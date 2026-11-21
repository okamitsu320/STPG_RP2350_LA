"""Offline capture planning: sample rate, buffer split and a trigger that first fires at a known place.

The analyzer cannot count events, so a trigger condition is only usable when it
cannot occur anywhere before the intended moment. Every candidate is checked
against the whole expected pin timeline from the start of the invocation:

* only tester-driven levels are known; a channel that is released (Z) or
  driven by the DUT can have any level ("wildcard");
* while several channels change in one step, any mix of old and new levels
  can be sampled (pin skew), so all intermediate states count as occurring;
* the analyzer is armed immediately before the target segment's RUN (runner
  SDK ``before_run`` hook), so it observes only the state held between arming
  and RUN and then the target segment itself. Before segment 0's RUN the
  levels are unknown (reported as an assumption); before a later segment the
  HOLD levels of the previous segment are known for tester-driven channels.
  The post-capture alignment detects an early trigger instead of trusting it.

Strategies: ``pattern`` (firmware complex trigger: 1..16 contiguous channels
within ch0..ch15), ``level`` (firmware "edge" trigger, which is a level
condition on one channel) and ``marker`` (level trigger on a marker signal that
exists only in a derived pattern). Nothing here talks to a device.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any

from stpg_runner.artifacts import RunnerError, canonical_json

from . import la_protocol as lp
from .address import Address, resolve, window
from .config import AnalyzerConfig, CaptureRequestSpec, MARKER_SIGNAL
from .timeline import BundleTimeline, ChannelMap, SegmentTimeline, vector_steps

MARGIN_SAMPLES = 8
MIN_PATTERN_STEADY_PS = 20_000  # complex trigger loop needs the state for a few analyzer clocks
PLAN_KIND = "stpg_capture_plan"


class PlanError(RunnerError):
    pass


@dataclass(frozen=True)
class Trigger:
    strategy: str  # pattern | level | marker
    trigger_type: int
    base: int
    count: int
    value: int
    time_ps: int  # segment-RUN-relative moment the condition first holds
    vector: int  # local vector containing that moment
    inputs_one: bool  # has a '1' on a declared tester-to-DUT input channel (robust against pre-RUN levels)
    late_driven: list[int]  # channels not driven from the start of the segment


def _popcount(value: int) -> int:
    return bin(value).count("1")


class _Prefix:
    """Distinct (wildcard, value) states that occur before a time, with their first time.

    Times are (segment, ps) tuples. Keys live in analyzer-channel space.
    """

    def __init__(self) -> None:
        self.first: dict[tuple[int, int], tuple[int, int]] = {}

    def add(self, when: tuple[int, int], value: int, wild: int) -> None:
        key = (wild, value & ~wild)
        if key not in self.first:
            self.first[key] = when

    def groups(self, mask: int) -> dict[int, dict[int, tuple[int, int]]]:
        out: dict[int, dict[int, tuple[int, int]]] = {}
        for (wild, value), when in self.first.items():
            w = wild & mask
            a = value & mask & ~w
            bucket = out.setdefault(w, {})
            if a not in bucket or when < bucket[a]:
                bucket[a] = when
        return out


def _scan_prefix(timeline: BundleTimeline, segment_index: int, last_local: int,
                 maps: dict[int, ChannelMap]) -> tuple[_Prefix, list[float]]:
    """Collect the states the armed analyzer can see before ``last_local`` of the target segment.

    The analyzer is armed right before the target segment's RUN, so the scan
    covers the pre-RUN state (the previous segment's HOLD levels, or unknown
    levels before segment 0) and the target segment. A vector's start state
    equals the previous vector's end state, so identical (start state, rate,
    characters) vectors produce identical states; only the first one is
    expanded. Returns the prefix and the first-drive time of each channel.
    """

    prefix = _Prefix()
    first_drive: list[float] = [math.inf] * 25
    full = (1 << 25) - 1
    hold: tuple[int, int] | None = None  # previous segment's final state (driven HOLD levels)
    if segment_index:
        previous = timeline.segments[segment_index - 1]
        _, value, enable = vector_steps(previous.trace, previous.vector_count - 1)[-1]
        hold = (maps[previous.index].project(value), maps[previous.index].project(enable))
    segment = timeline.segments[segment_index]
    cmap = maps[segment.index]
    trace = segment.trace
    if hold is not None:
        prefix.add((segment.index, -1), hold[0], ~hold[1] & full)  # held between arming and RUN
    seen: set[tuple] = set()
    for index in range(last_local + 1):
        key = (trace.start_values[index], trace.start_enables[index], trace.rate_ids[index], trace.chars[index])
        if key in seen and not (index == 0 and hold is not None):
            continue
        seen.add(key)
        steps = vector_steps(trace, index)
        run_start = index == 0
        if run_start and hold is not None:
            prev = hold  # PRESERVE_GPIO_STATE: the segment starts from the HOLD levels
        elif run_start:
            prev = None  # segment 0: levels before RUN are an explicit assumption
        else:
            prev = (cmap.project(steps[0][1]), cmap.project(steps[0][2]))
            prefix.add((segment.index, steps[0][0]), prev[0], ~prev[1] & full)
        for t, value, enable in (steps if run_start else steps[1:]):
            v, e = cmap.project(value), cmap.project(enable)
            when = (segment.index, t)
            for ch in range(25):
                if e >> ch & 1 and first_drive[ch] == math.inf:
                    first_drive[ch] = t
            if prev is None:
                if e:
                    prev = (v, e)
                    prefix.add(when, v, ~e & full)
                continue
            pv, pe = prev
            if (pv, pe) != (v, e):
                changed = (pv ^ v) | (pe ^ e)
                if pe == 0 and segment.index == 0 and run_start and t == trace.starts[0]:
                    wild = ~e & full  # first drive at RUN start: covered by the pre-RUN assumption
                else:
                    wild = (changed | ~pe | ~e) & full
                prefix.add(when, pv, wild)
                prefix.add(when, v, ~e & full)
            prev = (v, e)
    return prefix, first_drive


def _transitions(segment: SegmentTimeline, cmap: ChannelMap, first: int, last: int) -> list[tuple[int, int, int, int]]:
    """(time, vector, value, enable) after each change in [first, last] (channel space)."""

    out = []
    prev = None
    trace = segment.trace
    for index in range(first, last + 1):
        for t, value, enable in vector_steps(trace, index):
            v, e = cmap.project(value), cmap.project(enable)
            if prev != (v, e):
                out.append((t, index, v, e))
                prev = (v, e)
    return out


def _search(segment: SegmentTimeline, cmap: ChannelMap, prefix: _Prefix, first_drive: list[float], *,
            strategy: str, masks: list[tuple[int, int]], tau_lo: int, tau_hi: int, min_steady: int,
            inputs_mask: int, levels: tuple[int, ...], target: int, run_start: float) -> list[Trigger]:
    """Per mask: the valid moment closest to ``target`` (window start) and, separately, a RUN-start one."""

    trace = segment.trace
    lo = trace.vector_at(max(0, tau_lo))
    hi = trace.vector_at(max(0, tau_hi))
    changes = _transitions(segment, cmap, max(0, lo - 1), min(segment.vector_count - 1, hi + 1))
    times = [c[0] for c in changes]
    order = sorted(range(len(changes)), key=lambda k: (abs(times[k] - target), times[k]))
    found: list[Trigger] = []
    for base, count in masks:
        mask = ((1 << count) - 1) << base
        groups = prefix.groups(mask)
        picked_late = picked_start = False
        for k in order:
            if picked_late and picked_start:
                break
            t, vector, value, enable = changes[k]
            at_start = t <= run_start
            if (picked_start if at_start else picked_late):
                continue
            if t < tau_lo or t > tau_hi or enable & mask != mask:
                continue
            if k and (changes[k - 1][2] & mask, changes[k - 1][3] & mask) == (value & mask, mask):
                continue  # the masked state did not change here
            condition = value & mask
            if levels and condition >> base not in levels:
                continue
            when_now = (segment.index, t)
            if any(bucket.get(condition & ~wild, when_now) < when_now for wild, bucket in groups.items()):
                continue  # could already have matched earlier (or while switching)
            steady_until = trace.total_ps
            for later in changes[k + 1:]:
                if (later[2] & mask, later[3] & mask) != (condition, mask):
                    steady_until = later[0]
                    break
            if steady_until - t < min_steady:
                continue
            late = [ch for ch in range(base, base + count) if first_drive[ch] > trace.starts[0]]
            found.append(Trigger(strategy, lp.TRIGGER_COMPLEX if strategy == "pattern" else lp.TRIGGER_EDGE,
                                 base, count, condition >> base, t, vector, bool(condition & inputs_mask), late))
            if at_start:
                picked_start = True
            else:
                picked_late = True
    return found


def _contiguous_masks(usable: set[int], limit: int, max_bits: int) -> list[tuple[int, int]]:
    masks = []
    for base in range(limit):
        for count in range(1, max_bits + 1):
            channels = range(base, base + count)
            if base + count > limit or any(ch not in usable for ch in channels):
                break
            masks.append((base, count))
    masks.sort(key=lambda m: -m[1])
    return masks


def plan_capture(timeline: BundleTimeline, request: CaptureRequestSpec, analyzer: AnalyzerConfig) -> dict[str, Any]:
    """Return a schema-1 capture plan; ``status`` is PLANNED or UNSUPPORTED (never a PASS)."""

    info = analyzer.limits
    resolved = resolve(timeline, request.address)
    segment = timeline.segments[resolved.segment]
    trace = segment.trace
    first, last, notes = window(segment, resolved.local_vector, request.before_vectors, request.after_vectors)
    rows = segment.signal_channels(analyzer.wiring)
    by_name = {row["name"]: row for row in rows}
    if request.signals is None:
        selected = [row for row in rows if row["channel"] is not None and row["name"] != MARKER_SIGNAL]
    else:
        missing = [name for name in request.signals if name not in by_name]
        if missing:
            raise PlanError(f"signals not in this pattern: {missing}")
        selected = [by_name[name] for name in request.signals]
    unmapped = [{"name": row["name"], "reason": row["reason"]} for row in selected if row["channel"] is None]
    mapped = [row for row in selected if row["channel"] is not None]
    marker_row = by_name.get(MARKER_SIGNAL)
    if marker_row is not None and marker_row["channel"] is not None and marker_row not in mapped:
        mapped.append(marker_row)
    t0 = trace.starts[first]
    t1 = trace.starts[last] + trace.periods[last]
    plan: dict[str, Any] = {
        "kind": PLAN_KIND, "schema_version": 1, "status": "UNSUPPORTED", "device_io_performed": False,
        "bundle": {"manifest_sha256": timeline.manifest_sha256, "package_kind": timeline.package_kind,
                   "segment": segment.index, "segment_path": segment.path, "segment_sha256": segment.sha256,
                   "active_pins": segment.active_pins, "tick_hz": segment.tick_hz},
        "request": request.to_dict(),
        "target": resolved.to_dict(),
        "requested": {"first_vector": segment.global_start + max(0, resolved.local_vector - request.before_vectors),
                      "last_vector": segment.global_start + resolved.local_vector + request.after_vectors},
        "window": {"first_vector": segment.global_start + first, "last_vector": segment.global_start + last,
                   "first_local": first, "last_local": last, "start_ps": t0, "end_ps": t1},
        "analyzer": {"limits": info.to_dict(), "wiring": analyzer.wiring.to_config()},
        "channels": [], "unmapped": unmapped, "trigger": None, "samples": None, "predicted": None,
        "notes": notes, "reasons": [],
        "assumptions": [
            "The analyzer is armed immediately before the target segment's RUN (after its upload). Levels held "
            "before that RUN that the pattern does not drive are unknown: if the bench already shows the trigger "
            "condition while the analyzer is armed, it triggers early. The alignment step checks this; an "
            "unaligned capture is never adopted.",
            "Times are the pattern's nominal STPG ticks; the analyzer clock is independent (crystal drift, "
            "analyzer resolution of one sample, trigger latency up to ~25 ns for pattern triggers).",
        ],
    }
    if resolved.line is not None:
        plan["target"]["source_line"] = resolved.line
    if not mapped:
        plan["reasons"].append("none of the selected signals is wired to an analyzer channel")
        return plan
    channels = sorted({row["channel"] for row in mapped})
    mode = lp.capture_mode(channels)
    total = lp.buffer_samples(info, mode)
    pre_limit = lp.pretrigger_limit(info, mode, request.pretrigger_policy)
    plan["channels"] = [{"signal": row["name"], "bit": row["bit"], "gpio": row["gpio"], "channel": row["channel"],
                         "channel_label": row["channel"] + 1, "declared": row["declared"]}
                        for row in sorted(mapped, key=lambda r: r["channel"])]
    plan["analyzer"].update({"capture_mode": mode, "bytes_per_sample": lp.BYTES_PER_SAMPLE[mode],
                             "buffer_samples": total, "pretrigger_limit": pre_limit})
    base_ps = 10**12 // info.max_frequency
    if 10**12 % info.max_frequency:
        raise PlanError("analyzer max frequency must divide 1 THz for exact sample times")
    span = t1 - t0
    shortest = min(trace.periods[i] for i in range(first, last + 1))
    if request.sample_rate_hz is not None:
        if info.max_frequency % request.sample_rate_hz:
            raise PlanError(f"sample rate {request.sample_rate_hz} Hz is not max/n of {info.max_frequency} Hz; "
                            "fractional dividers add jitter")
        dividers = [info.max_frequency // request.sample_rate_hz]
    else:
        n_fit = max(1, math.ceil(span / (base_ps * max(1, total - 2 * MARGIN_SAMPLES - 8))))
        n_cap = max(n_fit, shortest // (2 * base_ps))
        dividers = sorted({n for n in (n_fit, n_fit * 2, n_fit * 4, n_fit * 8, n_fit * 16) if n <= max(n_cap, n_fit)})
    dividers = [n for n in dividers if info.max_frequency // n >= info.min_frequency]
    if not dividers:
        plan["reasons"].append("the window does not fit the analyzer buffer at any supported sample rate")
        return plan
    maps = {s.index: ChannelMap.build(s, analyzer.wiring) for s in timeline.segments[:segment.index + 1]}
    cmap = maps[segment.index]
    driven_any = 0
    inputs_mask = 0
    for row in rows:
        if row["channel"] is None:
            continue
        if (row["declared"] or "").lower() in ("in", "input"):
            inputs_mask |= 1 << row["channel"]
    # Channels the pattern drives at some time inside the window are trigger candidates.
    for index in range(first, last + 1):
        for _, _, enable in vector_steps(trace, index):
            driven_any |= cmap.project(enable)
    mode_limit = {lp.MODE_8: 8, lp.MODE_16: 16, lp.MODE_24: 24}[mode]
    strategies = ["marker", "pattern", "level"] if request.strategy == "auto" else [request.strategy]
    marker_channel = marker_row["channel"] if marker_row else None
    if "marker" in strategies and marker_channel is None:
        if request.strategy == "marker":
            plan["reasons"].append(f"no {MARKER_SIGNAL} signal wired to the analyzer in this bundle: derive a marker "
                                   "pattern first (new STIL, new bundle hash, separate approval)")
            return plan
        strategies.remove("marker")
    prefix = None
    tried: list[str] = []
    for n in dividers:
        p = base_ps * n
        frequency = info.max_frequency // n
        if math.ceil(span / p) + 2 * MARGIN_SAMPLES + 2 > total:
            tried.append(f"{frequency} Hz: window needs {math.ceil(span / p)} samples > buffer {total}")
            continue
        offset_max = lp.trigger_offset(info, frequency, lp.TRIGGER_COMPLEX)
        tau_lo = t1 - (total - 2 * MARGIN_SAMPLES - offset_max - 2) * p
        tau_hi = t0 + (pre_limit - MARGIN_SAMPLES - 1) * p
        if tau_hi < tau_lo:
            tried.append(f"{frequency} Hz: no trigger position fits pre-trigger limit {pre_limit}")
            continue
        if prefix is None or prefix[1] < tau_hi:
            last_local = trace.vector_at(max(0, tau_hi))
            scanned, first_drive = _scan_prefix(timeline, segment.index, last_local, maps)
            prefix = (scanned, trace.starts[last_local] + trace.periods[last_local], first_drive)
        candidates: list[Trigger] = []
        for strategy in strategies:
            if strategy == "marker":
                masks = [(marker_channel, 1)]
                levels: tuple[int, ...] = (1,)
                min_steady = 2 * p
            elif strategy == "pattern":
                usable = {ch for ch in range(min(16, mode_limit)) if driven_any >> ch & 1 and ch != marker_channel}
                masks = _contiguous_masks(usable, min(16, mode_limit), 16)
                levels = ()
                min_steady = max(MIN_PATTERN_STEADY_PS, p)
            else:
                usable = {ch for ch in range(mode_limit) if driven_any >> ch & 1 and ch != marker_channel}
                masks = [(ch, 1) for ch in sorted(usable)]
                levels = ()
                min_steady = 2 * p
            found = _search(segment, cmap, prefix[0], prefix[2], strategy=strategy, masks=masks, tau_lo=tau_lo,
                            tau_hi=tau_hi, min_steady=min_steady, inputs_mask=inputs_mask, levels=levels,
                            target=t0, run_start=min((t for t in prefix[2] if t != math.inf), default=0))
            candidates.extend(found)
            if candidates and strategy == "marker":
                break
        chosen = None
        run_start = prefix[2]
        first_drive_time = min((t for t in run_start if t != math.inf), default=0)

        def rank(t: Trigger) -> tuple:
            # Prefer conditions a bench is unlikely to show before RUN: not the first driven state,
            # not all-zero (pulled-down idle lines), with a '1' on a tester-to-DUT input.
            return (t.strategy != "marker", t.time_ps <= first_drive_time, t.value == 0, not t.inputs_one,
                    bool(t.late_driven), -t.count, abs(t.time_ps - t0))

        for trig in sorted(candidates, key=rank):
            offset = lp.trigger_offset(info, frequency, trig.trigger_type)
            pre = max(2, math.ceil((trig.time_ps - t0) / p) + MARGIN_SAMPLES)
            post = max(2 + offset, math.ceil((t1 - trig.time_ps) / p) + MARGIN_SAMPLES)
            if pre > pre_limit or pre + post > total:
                continue
            capture = sorted(set(channels) | set(range(trig.base, trig.base + trig.count)))
            try:
                lp.build_request(info, channels=capture, frequency=frequency, pre_samples=pre, post_samples=post,
                                 trigger_type=trig.trigger_type, trigger_channel=trig.base, trigger_count=trig.count,
                                 trigger_value=trig.value, inverted=trig.trigger_type == lp.TRIGGER_EDGE and trig.value == 0,
                                 pretrigger_policy=request.pretrigger_policy)
            except lp.ProtocolError as exc:
                tried.append(f"{frequency} Hz {trig.strategy}: {exc}")
                continue
            chosen = (trig, pre, post, offset, capture)
            break
        if chosen is None:
            tried.append(f"{frequency} Hz: no trigger condition first occurs within the reachable range "
                         f"[{tau_lo} ps, {tau_hi} ps]")
            continue
        trig, pre, post, offset, capture = chosen
        by_channel = {row["channel"]: row for row in rows if row["channel"] is not None}
        selected_channels = set(channels)
        plan["channels"] = [{"signal": by_channel[ch]["name"], "bit": by_channel[ch]["bit"],
                             "gpio": by_channel[ch]["gpio"], "channel": ch, "channel_label": ch + 1,
                             "declared": by_channel[ch]["declared"],
                             "role": "selected" if ch in selected_channels else "trigger"} for ch in capture]
        start = trig.time_ps - pre * p
        end = trig.time_ps + post * p
        names = {row["channel"]: row["name"] for row in rows if row["channel"] is not None}
        bits = [(trig.value >> i) & 1 for i in range(trig.count)]
        condition = ", ".join(f"ch{trig.base + i + 1}({names.get(trig.base + i, '?')})={b}" for i, b in enumerate(bits))
        plan.update({
            "status": "PLANNED",
            "sample_rate_hz": frequency, "sample_period_ps": p, "divider": n,
            "trigger": {"strategy": trig.strategy,
                        "type": "pattern" if trig.trigger_type == lp.TRIGGER_COMPLEX else "level",
                        "base_channel": trig.base, "count": trig.count, "value": trig.value,
                        "inverted": trig.trigger_type == lp.TRIGGER_EDGE and trig.value == 0,
                        "condition": condition, "time_ps": trig.time_ps,
                        "vector": segment.global_start + trig.vector, "local_vector": trig.vector,
                        "robust_pre_run": trig.inputs_one and trig.value != 0 and trig.time_ps > first_drive_time,
                        "at_run_start": trig.time_ps <= first_drive_time,
                        "late_driven_channels": [c + 1 for c in trig.late_driven],
                        "latency_offset_samples": offset},
            "samples": {"pre": pre, "post": post, "total": pre + post},
            "predicted": {"start_ps": start, "end_ps": end,
                          "first_vector": segment.global_start + trace.vector_at(max(0, start)),
                          "last_vector": segment.global_start + trace.vector_at(min(trace.total_ps - 1, end))},
            "resolution": {"sample_period_ps": p, "samples_per_shortest_vector": shortest / p},
            "capture_channels": capture,
        })
        if trig.time_ps <= first_drive_time:
            plan["assumptions"].append("The trigger is the first state the pattern drives: it fires early if the "
                                       "pins already show it before RUN (no later unique state was found).")
        if not trig.inputs_one:
            plan["assumptions"].append("The trigger value has no '1' on a declared DUT-input channel; a bench that "
                                       "idles at this value before RUN would trigger early.")
        if trig.late_driven:
            plan["assumptions"].append("Trigger channels not driven from the start of the segment are assumed not "
                                       "to show the trigger level before the pattern drives them.")
        distance = first - trig.vector
        if trig.strategy != "marker" and distance > max(64, 2 * (last - first + 1)):
            plan["notes"].append(f"the nearest unique trigger state is {distance} vectors before the window, so the "
                                 "capture is long and the sample rate may be reduced; a marker pattern gives an "
                                 "exact trigger at the window")
        if segment.index:
            plan["assumptions"].append(f"The analyzer is armed right before segment {segment.index}'s RUN; the "
                                       "previous segment's HOLD levels (shown while armed) were included in the "
                                       "uniqueness check, released channels as unknown.")
        break
    if plan["status"] != "PLANNED":
        plan["reasons"].extend(tried or ["no sample rate was tried"])
        if "marker" not in strategies:
            plan["reasons"].append("a marker pattern (derived STIL with a marker pulse) gives an exact trigger")
    return plan


def _differences(a: Any, b: Any, path: str = "") -> list[str]:
    if isinstance(a, dict) and isinstance(b, dict):
        out = []
        for key in sorted(set(a) | set(b), key=str):
            where = f"{path}.{key}" if path else str(key)
            if key not in a or key not in b:
                out.append(where)
            else:
                out.extend(_differences(a[key], b[key], where))
        return out
    if isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
        return [d for i, (x, y) in enumerate(zip(a, b)) for d in _differences(x, y, f"{path}[{i}]")]
    return [] if canonical_json(a) == canonical_json(b) else [path or "<root>"]


def validate_plan(timeline: BundleTimeline, plan: Any, analyzer: AnalyzerConfig) -> dict[str, Any]:
    """Re-derive a plan from its own request, the verified bundle and the frozen analyzer binding.

    The supplied plan must equal that canonical derivation exactly (types, every
    nested field, no unknown keys): trigger, window, channels, sample counts,
    rate and limits are never taken from the file on trust. Returns the
    canonical plan; raises before any device I/O otherwise.
    """

    if not isinstance(plan, dict):
        raise PlanError("capture plan must be a JSON object")
    if plan.get("kind") != PLAN_KIND or type(plan.get("schema_version")) is not int or plan["schema_version"] != 1:
        raise PlanError("unsupported capture plan kind/schema_version")
    if plan.get("status") != "PLANNED":
        raise PlanError("only a PLANNED capture plan can arm an analyzer")
    request = plan.get("request")
    if not isinstance(request, dict):
        raise PlanError("capture plan has no request")
    canonical = plan_capture(timeline, CaptureRequestSpec.from_dict(request), analyzer)
    if canonical_json(canonical) != canonical_json(plan):
        fields = _differences(plan, canonical)
        shown = ", ".join(fields[:8]) + (f" (+{len(fields) - 8} more)" if len(fields) > 8 else "")
        raise PlanError("capture plan differs from its re-derivation for this bundle and analyzer binding at: "
                        f"{shown or 'value types'}; plan the capture again")
    return canonical


def capture_request_from_plan(plan: dict, analyzer: AnalyzerConfig) -> lp.CaptureRequest:
    if plan.get("kind") != PLAN_KIND or plan.get("status") != "PLANNED":
        raise PlanError("only a PLANNED capture plan can arm an analyzer")
    trig = plan["trigger"]
    trigger_type = lp.TRIGGER_COMPLEX if trig["type"] == "pattern" else lp.TRIGGER_EDGE
    return lp.build_request(analyzer.limits, channels=plan["capture_channels"], frequency=plan["sample_rate_hz"],
                            pre_samples=plan["samples"]["pre"], post_samples=plan["samples"]["post"],
                            trigger_type=trigger_type, trigger_channel=trig["base_channel"],
                            trigger_count=trig["count"], trigger_value=trig["value"], inverted=trig["inverted"],
                            pretrigger_policy=plan["request"]["pretrigger_policy"])


def address_of(plan: dict) -> Address:
    return Address.from_dict(plan["request"]["address"])
