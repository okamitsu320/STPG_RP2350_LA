"""Align an analyzer trace with the expected timeline and report what was actually captured.

Only tester-driven transitions are trusted reference points. The search is
deliberately narrow (trigger latency, one sample and crystal drift around the
planned trigger time): repetitive patterns would align at many offsets, so a
trace that does not match near the plan is reported UNALIGNED instead of being
fitted somewhere else. Levels seen at compare strobes are reported as analyzer
observations only; they are not a DUT judgement.
"""

from __future__ import annotations

from bisect import bisect_left
from collections import Counter
from typing import Any

from .timeline import BundleTimeline, compare_strobes, level_segments
from .trace import LaTrace

MAX_DRIFT_PPM = 200
MIN_MATCHED_EDGES = 2
MATCH_RATIO = 0.9
LEVEL_RATIO = 0.98


def _expected_edges(segs: list[tuple[int, int, str]]) -> list[tuple[int, int]]:
    """(time, new level) where a driven level changes to the other driven level."""

    out = []
    for prev, cur in zip(segs, segs[1:]):
        if prev[2] in "01" and cur[2] in "01" and prev[2] != cur[2] and prev[1] == cur[0]:
            out.append((cur[0], int(cur[2])))
    return out


def align(timeline: BundleTimeline, plan: dict, trace: LaTrace) -> dict[str, Any]:
    segment = timeline.segments[plan["bundle"]["segment"]]
    vectors = segment.trace
    p = trace.sample_period_ps
    tau = plan["trigger"]["time_ps"] if plan.get("trigger") else plan["window"]["start_ps"]
    ti = trace.trigger_index
    count = len(trace.samples)
    names = {row["channel"]: row["signal"] for row in plan["channels"]}
    sig_index = {sig.name: i for i, sig in enumerate(vectors.signals)}
    window_ps = count * p
    search = max(4 * p, 100_000) + window_ps * MAX_DRIFT_PPM // 1_000_000

    def nominal(k: float) -> float:
        return tau + (k - ti) * p

    lo_t = max(0, int(nominal(0) - search))
    hi_t = min(vectors.total_ps - 1, int(nominal(count - 1) + search))
    first, last = vectors.vector_at(lo_t), vectors.vector_at(hi_t)
    per_signal = []
    for bit, channel in enumerate(trace.channels):
        name = names.get(channel)
        if name is None or name not in sig_index:
            continue
        segs = level_segments(vectors, sig_index[name], first, last)
        per_signal.append({"bit": bit, "channel": channel, "name": name, "signal": sig_index[name], "segs": segs,
                           "expected": _expected_edges(segs), "captured": trace.edges(bit)})

    # 1) offset from captured edges close to the trigger (drift is negligible there)
    result: dict[str, Any] = {"kind": "stpg_capture_alignment", "schema_version": 1, "search_ps": search,
                              "sample_period_ps": p, "notes": []}
    expected_total = sum(1 for item in per_signal for t, _ in item["expected"]
                         if nominal(0) <= t <= nominal(count - 1))
    near_window = max(200 * p, 2_000_000)  # +-2 us or 200 samples around the trigger
    near_search = max(4 * p, 100_000) + near_window * MAX_DRIFT_PPM // 1_000_000
    result["search_ps"] = near_search
    offsets: Counter[int] = Counter()
    for item in per_signal:
        exp_times = [t for t, _ in item["expected"]]
        exp_levels = [level for _, level in item["expected"]]
        for k, level in item["captured"]:
            if abs((k - ti) * p) > near_window:
                continue
            t = nominal(k)
            j = bisect_left(exp_times, t - near_search)
            while j < len(exp_times) and exp_times[j] <= t + near_search:
                if exp_levels[j] == level:
                    offsets[round((exp_times[j] - t) / p)] += 1
                j += 1
    if not offsets:
        result.update(status="NOT_ALIGNABLE" if expected_total == 0 else "UNALIGNED", confidence="low",
                      offset_ps=None, drift_ppm=None, rms_ps=None,
                      edges={"expected_in_capture": expected_total, "captured": sum(len(i["captured"]) for i in per_signal),
                             "matched_expected": 0, "paired": 0})
        result["notes"].append("no captured tester-driven transition near the planned trigger position"
                               if expected_total else "no tester-driven transition inside the captured range")
        result["coverage"] = "UNKNOWN"
        _levels_only(result, per_signal, trace, lambda k: nominal(k), vectors, first, last)
        return result
    best_bin, best = offsets.most_common(1)[0]
    near = [b for b, n in offsets.items() if abs(b - best_bin) > 2 and n >= 0.9 * best]

    # 2) pair edges and fit offset + drift, widening from the trigger outwards
    intercept, slope = float(best_bin * p), 1.0
    pairs: list[tuple[float, int]] = []
    matched_expected: set[tuple[int, int]] = set()
    reach = near_window
    while True:
        pairs, matched_expected = _pair(per_signal, ti, p, tau, intercept, slope, reach)
        if len(pairs) >= 2:
            xs = [x for x, _ in pairs]
            ys = [y - tau for _, y in pairs]
            mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
            sxx = sum((x - mx) ** 2 for x in xs)
            fitted = 1.0 if sxx == 0 else sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
            if abs(fitted - 1) * 1e6 <= MAX_DRIFT_PPM:
                slope = fitted
            intercept = my - slope * mx
        if reach >= window_ps:
            break
        reach *= 4
    rms = (sum((tau + intercept + slope * x - y) ** 2 for x, y in pairs) / len(pairs)) ** 0.5 if pairs else None

    def mapped(k: float) -> float:
        return tau + intercept + slope * (k - ti) * p

    start_t, end_t = mapped(0), mapped(count - 1)
    captured_tester = 0
    for item in per_signal:
        for k, _ in item["captured"]:
            t = mapped(k)
            if _state_at(item["segs"], t - 2 * p) in "01" and _state_at(item["segs"], t + 2 * p) in "01":
                captured_tester += 1
    expected_in = [(item["signal"], j) for item in per_signal for j, (t, _) in enumerate(item["expected"])
                   if start_t + p <= t <= end_t - p]
    matched_in = sum(1 for key in expected_in if key in matched_expected)
    exp_ratio = matched_in / len(expected_in) if expected_in else 0.0
    cap_ratio = min(1.0, len(pairs) / captured_tester) if captured_tester else 0.0
    # Every signal must agree on its own: a periodic clock can match at a wrong offset while
    # slower signals (address bits) disagree, so pooled ratios alone are not enough.
    disagreeing = []
    for item in per_signal:
        own = [j for j, (t, _) in enumerate(item["expected"]) if start_t + p <= t <= end_t - p]
        hit = sum(1 for j in own if (item["signal"], j) in matched_expected)
        if own and hit < MATCH_RATIO * len(own):
            disagreeing.append(f"{item['name']}: {hit}/{len(own)} transitions")
    status = "ALIGNED" if (len(pairs) >= 1 and exp_ratio >= MATCH_RATIO and cap_ratio >= MATCH_RATIO
                           and not near and not disagreeing) else "UNALIGNED"
    if near:
        result["notes"].append(f"ambiguous: offsets {sorted(near)[:4]} samples fit almost as well as {best_bin}")
    if not expected_in:
        status = "NOT_ALIGNABLE"
        result["notes"].append("no tester-driven transition inside the captured range")
    span = (max(x for x, _ in pairs) - min(x for x, _ in pairs)) if len(pairs) >= 2 else 0
    result.update(status=status, offset_ps=round(intercept), drift_ppm=round((slope - 1) * 1e6, 2),
                  drift_resolution_ppm=round(p / span * 1e6, 2) if span else None,
                  rms_ps=None if rms is None else round(rms),
                  edges={"expected_in_capture": len(expected_in), "matched_expected": matched_in,
                         "captured_on_tester_levels": captured_tester, "paired": len(pairs)})
    first_v = vectors.vector_at(max(0, int(start_t)))
    if vectors.starts[first_v] < start_t:
        first_v += 1
    last_v = vectors.vector_at(max(0, int(end_t)))
    if vectors.starts[last_v] + vectors.periods[last_v] > end_t:
        last_v -= 1
    base = segment.global_start
    result["actual"] = {"start_ps": round(start_t), "end_ps": round(end_t),
                        "first_vector": base + first_v if first_v <= last_v else None,
                        "last_vector": base + last_v if first_v <= last_v else None}
    want_first, want_last = plan["window"]["first_vector"], plan["window"]["last_vector"]
    if status != "ALIGNED" or first_v > last_v:
        result["coverage"] = "UNKNOWN"
    elif base + first_v <= want_first and base + last_v >= want_last:
        result["coverage"] = "FULL"
    elif base + last_v < want_first or base + first_v > want_last:
        result["coverage"] = "NONE"
    else:
        result["coverage"] = "PARTIAL"
    _levels_only(result, per_signal, trace, mapped, vectors, first, last)
    for level in result["levels"]:
        if level["total"] and level["agree"] < LEVEL_RATIO * level["total"]:
            disagreeing.append(f"{level['signal']}: levels agree {level['agree']}/{level['total']}")
    if disagreeing:
        result["status"] = "UNALIGNED" if result["status"] == "ALIGNED" else result["status"]
        result["notes"].append("signals that do not match the expected tester levels/transitions: "
                               + "; ".join(disagreeing[:8]))
        if result["status"] == "UNALIGNED":
            result["coverage"] = "UNKNOWN"
    result["confidence"] = ("normal" if result["status"] == "ALIGNED" and matched_in >= MIN_MATCHED_EDGES + 1
                            else "low")
    if result["status"] == "ALIGNED" and result["confidence"] == "low":
        result["notes"].append(f"only {matched_in} tester transition(s) inside the capture; the position is "
                               "consistent but weakly constrained")
    return result


def _pair(per_signal: list[dict], ti: int, p: int, tau: float, intercept: float, slope: float,
          reach: float) -> tuple[list[tuple[float, int]], set[tuple[int, int]]]:
    """Pair each captured edge within ``reach`` of the trigger with the nearest same-direction expected edge."""

    pairs: list[tuple[float, int]] = []
    matched: set[tuple[int, int]] = set()
    for item in per_signal:
        exp = item["expected"]
        exp_times = [t for t, _ in exp]
        for k, level in item["captured"]:
            x = (k - ti) * p
            if abs(x) > reach:
                continue
            t = tau + intercept + slope * x
            j = bisect_left(exp_times, t - 2 * p)
            pick = None
            while j < len(exp) and exp_times[j] <= t + 2 * p:
                if exp[j][1] == level and (pick is None or abs(exp_times[j] - t) < abs(exp_times[pick] - t)):
                    pick = j
                j += 1
            if pick is not None:
                pairs.append((x, exp_times[pick]))
                matched.add((item["signal"], pick))
    return pairs, matched


def _state_at(segs: list[tuple[int, int, str]], t: float) -> str:
    """Expected tester state at time t ('0', '1', 'Z', or '?' outside the decoded range)."""

    lo, hi = 0, len(segs) - 1
    while lo <= hi:
        mid = (lo + hi) // 2
        a, b, state = segs[mid]
        if t < a:
            hi = mid - 1
        elif t >= b:
            lo = mid + 1
        else:
            return state
    return "?"


def _levels_only(result: dict, per_signal: list[dict], trace: LaTrace, mapped, vectors, first: int, last: int) -> None:
    """Level agreement inside tester-driven stretches and analyzer levels at compare strobes."""

    p = trace.sample_period_ps
    count = len(trace.samples)
    levels = []
    for item in per_signal:
        agree = total = 0
        for a, b, state in item["segs"]:
            if state not in "01" or b - a < 4 * p:
                continue
            span = _sample_range(mapped, count, a + 2 * p, b - 2 * p, p)
            step = max(1, len(span) // 2000)
            for k in span[::step]:
                total += 1
                agree += trace.level(item["bit"], k) == int(state)
        levels.append({"signal": item["name"], "channel": item["channel"] + 1, "agree": agree, "total": total})
    strobes = {item["signal"]: {"signal": item["name"], "channel": item["channel"] + 1, "H": [0, 0], "L": [0, 0]}
               for item in per_signal}
    bits = {item["signal"]: item["bit"] for item in per_signal}
    t_lo, t_hi = mapped(0), mapped(count - 1)
    for index in range(first, last + 1):
        for t, sig, value in compare_strobes(vectors, index):
            if sig not in bits or value not in ("H", "L") or not t_lo <= t <= t_hi:
                continue
            k = _sample_at(mapped, count, t, p)
            if k is None:
                continue
            seen = trace.level(bits[sig], k)
            entry = strobes[sig][value]
            entry[1] += 1
            entry[0] += seen == (1 if value == "H" else 0)
    result["levels"] = levels
    result["strobe_observations"] = [{"signal": s["signal"], "channel": s["channel"],
                                      "H_seen_high": s["H"][0], "H_total": s["H"][1],
                                      "L_seen_low": s["L"][0], "L_total": s["L"][1]}
                                     for s in strobes.values() if s["H"][1] or s["L"][1]]
    result["strobe_note"] = ("analyzer level at each expected compare strobe (analyzer threshold and timing, "
                             "not the STPG comparison and not a DUT result)")


def _sample_at(mapped, count: int, t: float, p: int) -> int | None:
    t0 = mapped(0)
    k = round((t - t0) / ((mapped(count - 1) - t0) / max(1, count - 1))) if count > 1 else 0
    return k if 0 <= k < count else None


def _sample_range(mapped, count: int, a: float, b: float, p: int) -> range:
    if count < 2:
        return range(0)
    t0 = mapped(0)
    step = (mapped(count - 1) - t0) / (count - 1)
    k0 = max(0, int((a - t0) / step) + 1)
    k1 = min(count - 1, int((b - t0) / step))
    return range(k0, k1 + 1) if k1 >= k0 else range(0)


def overlay_segments(trace: LaTrace, plan: dict, alignment: dict, *, channel: int) -> list[tuple[int, int, int]]:
    """[start_ps, end_ps, level] of one analyzer channel in the segment's pattern time."""

    bit = trace.bit_of(channel)
    if bit is None or alignment.get("offset_ps") is None:
        return []
    p = trace.sample_period_ps
    tau = plan["trigger"]["time_ps"]
    slope = 1 + (alignment.get("drift_ppm") or 0) / 1e6
    offset = alignment["offset_ps"]
    ti = trace.trigger_index

    def mapped(k: float) -> int:
        return round(tau + offset + slope * (k - ti) * p)

    out = []
    count = len(trace.samples)
    start = 0
    level = trace.level(bit, 0) if count else 0
    for k, new in trace.edges(bit):
        out.append((mapped(start), mapped(k), level))
        start, level = k, new
    if count:
        out.append((mapped(start), mapped(count), level))
    return out
