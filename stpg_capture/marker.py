"""Derived marker patterns: the same STIL plus one marker signal that is high in one vector.

The marker is driven on a spare STPG GPIO chosen per derivation: any STPG DUT
GPIO that the analyzer binding's wiring connects to an analyzer input and that
no signal of the pattern uses (:func:`marker_candidates`); the binding's
``marker_gpio`` is only the default. Whether the pin is spare and safe to
drive on the bench is the operator's declaration, not something the files can
show. Derivation creates a NEW portable project (new STIL, new pin map, same
settings); its bundle has a new hash and needs its own run confirmation.
Nothing about the source project changes.

After the derived bundle is built, :func:`verify_derivation` decodes both
bundles and requires every original signal to keep exactly the same
drive/release/compare timeline (vector starts, periods and waits included).
A derivation that changes the original timing is rejected, not "approximately"
accepted. Supported source form: every vector statement assigns the same single
signal group (the usual ``V { ALL = ...; }``) and the target vector is a
top-level statement that produces exactly one vector.

Provenance is bound to the immutable inventory, not to a directory layout:
:func:`check_marker_bundle` recognises a derived bundle by its own hashed
content (a ``STPG_MARKER`` signal or a pin-map role ``marker``), requires a
derivation record whose derived STIL/pin-map hashes equal the bundle's bound
inputs, re-verifies the hash-pinned source bundle and recomputes
:func:`verify_derivation`. A stored verification file is never trusted.
"""

from __future__ import annotations

import hashlib
from pathlib import Path, PurePosixPath
import re
from typing import Any

from stil.ast import LoopStmt, VectorStmt
from stil.parser import parse_stil_file
from stil.source_artifacts import read_source_text, write_source_text_artifact
from stpg_runner import load_project
from stpg_runner.artifacts import RunnerError, confined_path, identity, read_json, write_json
from stpg_runner.project import Project
from stpg_runner.source_storage import bundle_source_paths

from .address import Address, resolve
from .config import MARKER_SIGNAL, AnalyzerConfig
from .la_protocol import CHANNEL_GPIO
from .timeline import BundleTimeline, load_bundle_timeline
from .wiring import STPG_DUT_GPIO, stpg_bit_for_gpio

DERIVATION_KIND = "stpg_capture_marker_derivation"
DERIVATION_FILE = "marker.derivation.json"


class MarkerError(RunnerError):
    pass


def _skip(text: str, i: int) -> int:
    """Index after whitespace and comments starting at i."""

    while i < len(text):
        if text[i].isspace():
            i += 1
        elif text.startswith("//", i):
            end = text.find("\n", i)
            i = len(text) if end < 0 else end + 1
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = len(text) if end < 0 else end + 2
        else:
            break
    return i


def _block_end(text: str, open_brace: int) -> int:
    depth, i = 0, open_brace
    while i < len(text):
        ch = text[i]
        if ch in "'\"":
            end = text.find(ch, i + 1)
            i = len(text) if end < 0 else end + 1
            continue
        if text.startswith("//", i) or text.startswith("/*", i):
            i = _skip(text, i)
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    raise MarkerError("unbalanced braces in the STIL source")


def _line_offsets(text: str) -> list[int]:
    return [0] + [m.end() for m in re.finditer("\n", text)]


def _collect_vectors(statements, out: list, *, top: bool) -> None:
    for stmt in statements:
        if isinstance(stmt, VectorStmt):
            out.append((stmt, top))
        elif isinstance(stmt, LoopStmt):
            _collect_vectors(stmt.statements, out, top=False)


def _vector_data_end(text: str, offsets: list[int], stmt: VectorStmt) -> int:
    """Offset of the ';' that ends the data of one ``V { G = data; }`` statement."""

    loc = stmt.location
    start = offsets[loc.line - 1] + max(0, loc.column - 1)
    match = re.compile(r'(?:(?:"[^"]*"|[A-Za-z_][A-Za-z0-9_]*)\s*:\s*)?(?:V|Vector)\s*\{\s*'
                       + re.escape(stmt.target) + r"\s*=").match(text, start)
    if not match:
        raise MarkerError(f"cannot locate the vector statement at line {loc.line}")
    end = text.find(";", match.end())
    if end < 0:
        raise MarkerError(f"unterminated vector data at line {loc.line}")
    return end


def rewrite_stil(text: str, source: Path, *, target_line: int) -> tuple[str, dict[str, Any]]:
    """Add ``STPG_MARKER`` (high only in the vector statement at ``target_line``)."""

    document = parse_stil_file(source)
    if any(s.name == MARKER_SIGNAL for s in document.signals):
        raise MarkerError(f"the source already declares {MARKER_SIGNAL}")
    vectors: list[tuple[VectorStmt, bool]] = []
    for pattern in document.patterns:
        if pattern.statements:
            _collect_vectors(pattern.statements, vectors, top=True)
        else:
            raise MarkerError("pattern statements were not retained by the parser")
    for definition in (*document.macros, *document.procedures):
        _collect_vectors(definition.statements, vectors, top=False)
    targets = {stmt.target for stmt, _ in vectors}
    if len(targets) != 1:
        raise MarkerError(f"vector statements assign {sorted(targets)}; marker derivation needs one common group")
    group = targets.pop()
    groups = {g.name: g for g in document.signal_groups}
    if group not in groups:
        raise MarkerError(f"vectors assign {group!r}, which is not a SignalGroup")
    at_target = [(stmt, top) for stmt, top in vectors if stmt.location.line == target_line]
    if len(at_target) != 1 or not at_target[0][1]:
        raise MarkerError(f"line {target_line} must hold exactly one top-level vector statement "
                          "(not inside a Loop, Macro or Procedure)")
    offsets = _line_offsets(text)
    inline: list[tuple[int, str]] = []  # text inserted exactly at an offset
    before_close: list[tuple[int, str, str]] = []  # (offset of a closing brace, own-line text, inline text)
    for stmt, _ in vectors:
        char = "1" if stmt is at_target[0][0] else "0"
        inline.append((_vector_data_end(text, offsets, stmt), f" {char}"))
    # SignalGroups: append the marker to the group's quoted expression.
    gloc = groups[group].location
    gstart = offsets[gloc.line - 1] + max(0, gloc.column - 1)
    gmatch = re.compile(re.escape(group) + r"\s*=\s*'([^']*)'").search(text, gstart)
    if not gmatch:
        raise MarkerError(f"cannot locate the definition of group {group}")
    inline.append((gmatch.end(1), f" + {MARKER_SIGNAL}"))
    # Signals block: declare the marker as a tester-driven input.
    smatch = re.search(r"\bSignals\s*\{", text)
    if not smatch:
        raise MarkerError("no Signals block")
    declaration = f"{MARKER_SIGNAL} In;"
    before_close.append((_block_end(text, smatch.end() - 1), "  " + declaration, declaration))
    # Every WaveformTable: a signal-specific definition (it takes precedence over group definitions).
    waveform = f"{MARKER_SIGNAL} {{ 0: 0ns Drive(0); 1: 0ns Drive(1); }}"
    for table in document.waveform_tables:
        tstart = offsets[table.location.line - 1] + max(0, table.location.column - 1)
        tmatch = re.compile(r"WaveformTable\b[^{]*\{").search(text, tstart)
        if not tmatch:
            raise MarkerError(f"cannot locate WaveformTable {table.name}")
        tend = _block_end(text, tmatch.end() - 1)
        wmatch = re.compile(r"\bWaveforms\s*\{").search(text, tmatch.end(), tend)
        if not wmatch:
            raise MarkerError(f"WaveformTable {table.name} has no Waveforms block")
        line_start = text.rfind("\n", 0, wmatch.start()) + 1
        pad = re.match(r"[ \t]*", text[line_start:]).group(0) + "  "
        before_close.append((_block_end(text, wmatch.end() - 1), pad + waveform, waveform))
    edits: list[tuple[int, str]] = list(inline)
    for close, own_line, inline_text in before_close:
        line_start = text.rfind("\n", 0, close) + 1
        if text[line_start:close].strip() == "":
            edits.append((line_start, own_line + "\n"))  # the brace sits on its own line
        else:
            edits.append((close, " " + inline_text + " "))
    out = text
    for offset, insert in sorted(edits, key=lambda e: e[0], reverse=True):
        out = out[:offset] + insert + out[offset:]
    return out, {"group": group, "vector_statements": len(vectors)}


def marker_candidates(timeline: BundleTimeline, analyzer: AnalyzerConfig) -> list[dict[str, Any]]:
    """STPG DUT GPIOs a marker of this bundle could use: wired to the analyzer, driven by no signal.

    Necessary, not sufficient: only the operator knows whether a pin is spare
    and safe to drive on the bench (nothing but the analyzer input on it).
    """

    used = {sig.bit for segment in timeline.segments for sig in segment.trace.signals if sig.bit is not None}
    pins = max(segment.active_pins for segment in timeline.segments)
    out = []
    for gpio in STPG_DUT_GPIO:
        bit, channel = stpg_bit_for_gpio(gpio), analyzer.wiring.channel(gpio)
        if channel is not None and bit not in used:
            out.append({"gpio": gpio, "bit": bit, "analyzer_channel": channel + 1,
                        "analyzer_gpio": CHANNEL_GPIO[channel], "switches_to_24_pins": bit >= 16 and pins == 16})
    return out


def derive_marker_project(project_path: Path, *, bundle_dir: Path, manifest_sha256: str, vector: int,
                          analyzer: AnalyzerConfig, directory: Path,
                          marker_gpio: int | None = None) -> dict[str, Any]:
    """Create a new project whose STIL adds a marker pulse at ``vector`` (expanded, zero-based).

    ``marker_gpio`` chooses the pin (default: the analyzer binding's ``marker_gpio``).
    """

    gpio = analyzer.marker_gpio if marker_gpio is None else marker_gpio
    if gpio is None:
        raise MarkerError("choose a marker GPIO: none was given and the analyzer binding names no default "
                          "marker_gpio (a spare, safe, wired STPG GPIO)")
    if type(gpio) is not int or gpio not in STPG_DUT_GPIO:
        raise MarkerError("the marker GPIO must be an STPG DUT GPIO number (2..18, 20..23, 26..28)")
    if analyzer.wiring.channel(gpio) is None:
        raise MarkerError(f"GPIO{gpio} is not wired to an analyzer input (wiring {analyzer.wiring.label})")
    project_path = Path(project_path).resolve()
    project = load_project(project_path)
    settings = project.to_dict()
    inputs = project.input_paths(project_path.parent)
    timeline = load_bundle_timeline(bundle_dir, manifest_sha256=manifest_sha256)
    binding = read_json(Path(bundle_dir) / "runner.bundle.json")["binding"]
    if binding["project"] != settings or {k: v["sha256"] for k, v in binding["inputs"].items()} != {
            role: hashlib.sha256(path.read_bytes()).hexdigest() for role, path in inputs.items()}:
        raise MarkerError("the bundle was not built from this project's current settings/inputs")
    target = resolve(timeline, Address(vector=vector))
    if target.line is None:
        raise MarkerError("the target vector has no STIL line in the map")
    from .address import line_occurrences

    if len(line_occurrences(timeline, target.line)) != 1:
        raise MarkerError(f"line {target.line} produces several vectors (Loop/Macro); a marker there would "
                          "pulse at every repetition")
    bit = stpg_bit_for_gpio(gpio)
    segment = timeline.segments[target.segment]
    used = {s.bit: s.name for seg in timeline.segments for s in seg.trace.signals if s.bit is not None}
    if bit in used:
        raise MarkerError(f"GPIO{gpio} (logical bit {bit}) is used by signal {used[bit]}")
    old_pins = segment.active_pins
    new_pins = 24 if bit >= 16 or old_pins == 24 else 16
    source_text = read_source_text(inputs["stil"])
    derived_text, info = rewrite_stil(source_text, inputs["stil"], target_line=target.line)
    directory = Path(directory).resolve()
    directory.parent.mkdir(parents=True, exist_ok=True)
    directory.mkdir()  # new project only; never reused
    (directory / "inputs").mkdir()
    derived_stil = write_source_text_artifact(directory / "inputs" / "pattern.stil", derived_text)
    signals = {}
    if "pin_map" in inputs:
        pin_map = read_json(inputs["pin_map"])
        signals = dict(pin_map["signals"])
    else:
        directions = {"in": "input", "input": "input", "out": "output", "output": "output"}
        for sig in segment.trace.signals:
            signals[sig.name] = {"bit": sig.bit, "logical_pin": f"DUT{sig.bit}",
                                 "direction": directions.get((sig.declared or "").lower(), "inout"),
                                 "role": "signal"}
    # role "marker": the compiler refuses to share this bit with any other signal
    signals[MARKER_SIGNAL] = {"bit": bit, "logical_pin": f"DUT{bit}", "direction": "input", "role": "marker"}
    write_json(directory / "inputs" / "pins.json", {"active_pins": new_pins, "signals": signals})
    new_settings = dict(settings)
    new_settings["input"] = dict(settings["input"], stil=derived_stil.relative_to(directory).as_posix(), pin_map="inputs/pins.json")
    Project.from_dict(new_settings)  # same strict validation as any project
    project_file = directory / "project.stpg-project.json"
    write_json(project_file, new_settings)
    derivation = {
        "kind": DERIVATION_KIND, "schema_version": 1,
        "source": {"project": str(project_path), "bundle_dir": str(Path(bundle_dir).resolve()),
                   "manifest_sha256": manifest_sha256,
                   "stil_sha256": hashlib.sha256(source_text.encode()).hexdigest()},
        "derived": {"project": str(project_file),
                    "stil_sha256": identity(derived_stil)["sha256"],
                    "pin_map_sha256": identity(directory / "inputs" / "pins.json")["sha256"]},
        "target": target.to_dict(),
        "marker": {"signal": MARKER_SIGNAL, "gpio": gpio, "bit": bit,
                   "analyzer_channel": analyzer.wiring.channel(gpio) + 1},
        "active_pins": {"source": old_pins, "derived": new_pins},
        "rewrite": info,
        "notes": ["A new pattern: new STIL, pin map and bundle hash; it needs its own run confirmation.",
                  "verify_derivation must report EQUIVALENT_TIMING before the derived bundle is used."]
                 + (["active pins change 16 -> 24 (24-pin firmware mode): refill load differs from the source"]
                    if new_pins != old_pins else []),
    }
    write_json(directory / DERIVATION_FILE, derivation)
    return {"project_path": str(project_file), "derivation": derivation}


def verify_derivation(source: BundleTimeline, derived: BundleTimeline, derivation: dict) -> dict[str, Any]:
    """Every original signal keeps its timeline; the marker is high in exactly the target vector."""

    problems: list[str] = []
    if len(source.segments) != len(derived.segments):
        problems.append(f"segment count {len(source.segments)} -> {len(derived.segments)}")
    target = derivation["target"]["vector"]
    for a, b in zip(source.segments, derived.segments):
        ta, tb = a.trace, b.trace
        if ta.vector_count != tb.vector_count:
            problems.append(f"segment {a.index}: vectors {ta.vector_count} -> {tb.vector_count}")
            continue
        names_b = {s.name: i for i, s in enumerate(tb.signals)}
        marker = names_b.get(MARKER_SIGNAL)
        if marker is None:
            problems.append(f"segment {a.index}: no {MARKER_SIGNAL} in the derived binary")
            continue
        index_map = {i: names_b.get(s.name) for i, s in enumerate(ta.signals)}
        if None in index_map.values():
            problems.append("a source signal is missing from the derived binary")
            continue
        for v in range(ta.vector_count):
            if ta.starts[v] != tb.starts[v] or ta.periods[v] != tb.periods[v]:
                problems.append(f"vector {a.global_start + v}: timing {ta.starts[v]}+{ta.periods[v]} -> "
                                f"{tb.starts[v]}+{tb.periods[v]} ps")
                if len(problems) > 20:
                    break
                continue
            ev_a = sorted((index_map[e.signal], e.t, e.kind, e.value) for e in ta.events(v))
            ev_b = sorted((e.signal, e.t, e.kind, e.value) for e in tb.events(v) if e.signal != marker)
            if ev_a != ev_b:
                problems.append(f"vector {a.global_start + v}: events differ")
            level = (tb.start_values[v] >> marker) & 1 if (tb.start_enables[v] >> marker) & 1 else None
            after = [e.value for e in tb.events(v) if e.signal == marker and e.kind == "D"]
            high = (after[-1] == "1") if after else level == 1
            if high != (a.global_start + v == target):
                problems.append(f"vector {a.global_start + v}: marker is {'high' if high else 'low'}")
            if len(problems) > 20:
                break
    return {"kind": "stpg_capture_marker_verification", "schema_version": 1,
            "status": "EQUIVALENT_TIMING" if not problems else "REJECT", "problems": problems[:20],
            "source_manifest_sha256": source.manifest_sha256, "derived_manifest_sha256": derived.manifest_sha256,
            "output_events": {"source": _events(source), "derived": _events(derived)}}


def is_marker_bundle(bundle_dir: Path) -> bool:
    """A derived marker bundle, recognised from its own hashed input snapshots (never from its location).

    True when the bundle's STIL snapshot names the ``STPG_MARKER`` signal or its
    pin-map snapshot has a ``marker`` role. Call after the bundle verified.
    """

    root = Path(bundle_dir)
    inputs = bundle_source_paths(root)
    stil = read_source_text(inputs["stil"], errors="replace")
    if re.search(rf"(?<![A-Za-z0-9_]){MARKER_SIGNAL}(?![A-Za-z0-9_])", stil):
        return True
    if "pin_map" in inputs:
        pins = read_json(inputs["pin_map"])
        signals = pins.get("signals") if isinstance(pins, dict) else None
        if isinstance(signals, dict) and any(isinstance(v, dict) and v.get("role") == "marker"
                                             for v in signals.values()):
            return True
    return False


def _derivation_fields(derivation: Any) -> dict:
    if not isinstance(derivation, dict) or derivation.get("kind") != DERIVATION_KIND \
            or derivation.get("schema_version") != 1:
        raise MarkerError("not a marker derivation record (kind/schema_version)")
    try:
        source, derived, marker = derivation["source"], derivation["derived"], derivation["marker"]
        fields = {"source_dir": source["bundle_dir"], "source_sha": source["manifest_sha256"],
                  "stil": derived["stil_sha256"], "pins": derived["pin_map_sha256"],
                  "gpio": marker["gpio"], "bit": marker["bit"], "signal": marker["signal"],
                  "vector": derivation["target"]["vector"]}
    except (KeyError, TypeError) as exc:
        raise MarkerError(f"marker derivation record is incomplete ({exc}); derive the marker pattern again") from exc
    if (not all(isinstance(fields[k], str) for k in ("source_dir", "source_sha", "stil", "pins", "signal"))
            or type(fields["gpio"]) is not int or fields["gpio"] not in STPG_DUT_GPIO
            or type(fields["bit"]) is not int or stpg_bit_for_gpio(fields["gpio"]) != fields["bit"]
            or fields["signal"] != MARKER_SIGNAL or type(fields["vector"]) is not int):
        raise MarkerError("marker derivation record has inconsistent fields")
    return fields


def check_marker_bundle(timeline: BundleTimeline, derivation: dict | None, *,
                        analyzer: AnalyzerConfig | None = None) -> dict | None:
    """None for an ordinary bundle; for a derived marker bundle the re-verified provenance, or MarkerError.

    The record must belong to this bundle's hashed inputs, the source bundle
    must still verify against its pinned manifest hash, and the recomputed
    timing comparison must be EQUIVALENT_TIMING. With ``analyzer``, its wiring
    must connect the marker GPIO to an analyzer input; the binding's
    ``marker_gpio`` is only the default pin for new derivations, so a pattern
    derived on another free pin is accepted.
    """

    decoded = any(sig.name == MARKER_SIGNAL for segment in timeline.segments for sig in segment.trace.signals)
    if not decoded and not is_marker_bundle(timeline.bundle_dir):
        if derivation is not None:
            raise MarkerError("a marker derivation record was given for a bundle without a marker signal")
        return None
    if derivation is None:
        raise MarkerError("this bundle drives a marker signal (a derived pattern) but no derivation record was "
                          "found for its manifest hash; derive it with stpg-view or pass its marker.derivation.json")
    fields = _derivation_fields(derivation)
    root = Path(timeline.bundle_dir)
    inputs = read_json(root / "runner.bundle.json")["binding"]["inputs"]
    if fields["stil"] != inputs["stil"]["sha256"] or fields["pins"] != (inputs.get("pin_map") or {}).get("sha256"):
        raise MarkerError("the derivation record does not belong to this bundle (derived STIL/pin-map hashes differ)")
    pins = read_json(bundle_source_paths(root)["pin_map"])
    marker_pin = (pins.get("signals") or {}).get(MARKER_SIGNAL) if isinstance(pins, dict) else None
    if not isinstance(marker_pin, dict) or marker_pin.get("bit") != fields["bit"] or marker_pin.get("role") != "marker":
        raise MarkerError("the bundle's pin map does not drive the marker bit named by the derivation record")
    if analyzer is not None and analyzer.wiring.channel(fields["gpio"]) is None:
        raise MarkerError(f"this derived pattern drives its marker on GPIO{fields['gpio']}, which the analyzer "
                          f"binding's wiring ({analyzer.wiring.label}) connects to no analyzer input")
    try:
        source = load_bundle_timeline(Path(fields["source_dir"]), manifest_sha256=fields["source_sha"])
    except (RunnerError, OSError) as exc:
        raise MarkerError(f"the source bundle {fields['source_dir']} cannot be re-verified ({exc}); timing "
                          "equivalence cannot be shown, so a relocated source is not supported") from exc
    verification = verify_derivation(source, timeline, derivation)
    if verification["status"] != "EQUIVALENT_TIMING":
        raise MarkerError(f"the derived pattern does not keep the source timing: {verification['problems'][:3]}")
    return {"derivation": derivation, "verification": verification, "marker_gpio": fields["gpio"]}


def _events(timeline: BundleTimeline) -> int:
    patterns = read_json(Path(timeline.bundle_dir) / "runner.bundle.json")["patterns"]
    return sum(item["expected"]["events"] for item in patterns)


def relative(path: Path, root: Path) -> str:
    return PurePosixPath(Path(path).resolve().relative_to(Path(root).resolve())).as_posix()
