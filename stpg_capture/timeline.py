"""Expected pin timeline of a verified runner bundle, per segment, in RUN-relative picoseconds.

The binaries are decoded with the viewer's firmware-equivalent decoder
(``stpgview.load_bin``); the bundle is verified with the runner SDK first.
Vector numbers are expanded zero-based vector IDs; waits lengthen a vector and
never create one. Segment k's times are relative to that segment's own RUN.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
from typing import Callable

from stpg_runner import verify_bundle
from stpg_runner.artifacts import RunnerError, confined_path, read_json
from stpg_runner.build import BUNDLE_NAME
from stpg_runner.progress import CancellationToken, ProgressEvent, Reporter
from stpgview.load_bin import load_bin
from stpgview.model import KIND_COMPARE, KIND_DRIVE, KIND_RELEASE, Trace

from .wiring import Wiring, stpg_gpio


@dataclass
class SegmentTimeline:
    index: int
    trace: Trace
    active_pins: int
    tick_hz: int
    global_start: int
    path: str  # bundle-relative
    sha256: str

    @property
    def vector_count(self) -> int:
        return self.trace.vector_count

    @property
    def global_end(self) -> int:
        return self.global_start + self.vector_count - 1

    def signal_channels(self, wiring: Wiring) -> list[dict]:
        """Per trace signal: name, logical bit, STPG GPIO and analyzer channel (or the reason there is none)."""

        rows = []
        for index, signal in enumerate(self.trace.signals):
            row = {"signal": index, "name": signal.name, "bit": signal.bit, "gpio": None, "channel": None,
                   "declared": signal.declared, "reason": ""}
            if signal.bit is None:
                row["reason"] = "no logical bit in the map"
            else:
                try:
                    row["gpio"] = stpg_gpio(signal.bit, self.active_pins)
                except ValueError as exc:
                    row["reason"] = str(exc)
                else:
                    row["channel"] = wiring.channel(row["gpio"])
                    if row["channel"] is None:
                        row["reason"] = f"GPIO{row['gpio']} is not wired to an analyzer channel"
            rows.append(row)
        return rows


@dataclass
class BundleTimeline:
    bundle_dir: Path
    manifest_sha256: str
    package_kind: str
    segments: list[SegmentTimeline]

    @property
    def total_vectors(self) -> int:
        return sum(segment.vector_count for segment in self.segments)

    def locate(self, global_vector: int) -> tuple[SegmentTimeline, int]:
        for segment in self.segments:
            if segment.global_start <= global_vector <= segment.global_end:
                return segment, global_vector - segment.global_start
        raise RunnerError(f"vector {global_vector} is outside the bundle (0..{self.total_vectors - 1})")

    def summary(self) -> dict:
        return {"bundle_dir": str(self.bundle_dir), "manifest_sha256": self.manifest_sha256,
                "package_kind": self.package_kind, "total_vectors": self.total_vectors,
                "segments": [{"index": s.index, "path": s.path, "sha256": s.sha256, "active_pins": s.active_pins,
                              "tick_hz": s.tick_hz, "global_vector_start": s.global_start,
                              "global_vector_end": s.global_end, "duration_ps": s.trace.total_ps,
                              "signals": [sig.name for sig in s.trace.signals]} for s in self.segments]}


def load_bundle_timeline(bundle_dir: Path, *, manifest_sha256: str,
                         on_progress: Callable[[ProgressEvent], None] | None = None,
                         cancel: CancellationToken | None = None) -> BundleTimeline:
    """Verify the bundle (offline) and decode each pattern binary. No device I/O."""
    progress = Reporter(on_progress, cancel)
    return progress.perform("capture_timeline", lambda: _load_bundle_timeline(
        bundle_dir, manifest_sha256=manifest_sha256, progress=progress))


def _load_bundle_timeline(bundle_dir: Path, *, manifest_sha256: str, progress: Reporter) -> BundleTimeline:
    def forward(event: ProgressEvent) -> None:
        progress.emit(event.stage, event.event, completed=event.completed, total=event.total, detail=event.detail)

    root = Path(bundle_dir).resolve()
    verified = verify_bundle(root, manifest_sha256=manifest_sha256,
                             on_progress=forward if progress.callback is not None else None,
                             cancel=progress.cancel, reuse=True)
    if verified["status"] != "PASS":
        raise RunnerError(f"bundle verification rejected: {verified['errors']}")
    manifest = read_json(root / BUNDLE_NAME, expected_sha256=manifest_sha256)
    patterns = manifest["patterns"]
    segments: list[SegmentTimeline] = []
    specs = None
    if manifest["package_kind"] == "segmented":
        from stil.segmented import load_manifest

        specs = load_manifest(confined_path(root, manifest["package_manifest"])).segments
        if len(specs) != len(patterns):
            raise RunnerError("segment manifest and bundle pattern list differ")
    next_start = 0
    for index, item in enumerate(patterns):
        progress.cancel.check()
        path = confined_path(root, item["path"])
        payload = path.read_bytes()
        digest = hashlib.sha256(payload).hexdigest()
        if digest != manifest["files"][item["path"]]["sha256"]:
            raise RunnerError("pattern changed after bundle verification")
        map_path = None
        if specs is not None and specs[index].map_path is not None:
            map_path = specs[index].map_path
        trace = load_bin(path, map_path=map_path)
        start = next_start
        if specs is not None:
            spec = specs[index]
            if spec.global_vector_start is not None:
                start = spec.global_vector_start
                if spec.global_vector_end is not None and spec.global_vector_end - start + 1 != trace.vector_count:
                    raise RunnerError(f"segment {index}: manifest covers vectors {start}..{spec.global_vector_end} "
                                      f"but the binary decodes to {trace.vector_count} vectors")
            if start != next_start:
                raise RunnerError(f"segment {index} starts at vector {start}, expected {next_start}")
        expected = item["expected"]
        segments.append(SegmentTimeline(index, trace, int(expected["active_pins"]), int(expected["tick_hz"]),
                                        start, item["path"], digest))
        next_start = start + trace.vector_count
        progress.emit("capture_decode", "progress", completed=index + 1, total=len(patterns))
    checked = verify_bundle(root, manifest_sha256=manifest_sha256, cancel=progress.cancel, reuse=True)
    if checked["status"] != "PASS":
        raise RunnerError(f"bundle changed during timeline decoding: {checked['errors']}")
    return BundleTimeline(root, manifest_sha256, manifest["package_kind"], segments)


# ---- channel-space state ------------------------------------------------------

@dataclass(frozen=True)
class ChannelMap:
    """Trace signal index -> analyzer channel index, for the signals that have one."""

    sig_to_ch: tuple[int | None, ...]

    @classmethod
    def build(cls, segment: SegmentTimeline, wiring: Wiring, signals: set[int] | None = None) -> "ChannelMap":
        rows = segment.signal_channels(wiring)
        return cls(tuple(row["channel"] if (signals is None or row["signal"] in signals) else None for row in rows))

    def project(self, mask: int) -> int:
        out = 0
        for sig, ch in enumerate(self.sig_to_ch):
            if ch is not None and mask >> sig & 1:
                out |= 1 << ch
        return out

    @property
    def channel_mask(self) -> int:
        return self.project((1 << len(self.sig_to_ch)) - 1)


def vector_steps(trace: Trace, index: int) -> list[tuple[int, int, int]]:
    """(absolute ps, drive value, drive enable) at the vector start and after each event time.

    Values/enables are signal-indexed bitmasks; compare strobes do not change them.
    """

    value = trace.start_values[index]
    enable = trace.start_enables[index]
    base = trace.starts[index]
    steps = [(base, value, enable)]
    events = sorted((e for e in trace.events(index) if e.kind in (KIND_DRIVE, KIND_RELEASE)), key=lambda e: e.t)
    i = 0
    while i < len(events):
        t = events[i].t
        while i < len(events) and events[i].t == t:
            event = events[i]
            bit = 1 << event.signal
            if event.kind == KIND_DRIVE:
                enable |= bit
                value = value | bit if event.value == "1" else value & ~bit
            else:
                enable &= ~bit
            i += 1
        if (value, enable) != steps[-1][1:]:
            steps.append((base + t, value, enable))
    return steps


def compare_strobes(trace: Trace, index: int) -> list[tuple[int, int, str]]:
    """(absolute ps, signal, H/L/X) compare strobes of one vector."""

    base = trace.starts[index]
    return [(base + e.t, e.signal, e.value) for e in trace.events(index) if e.kind == KIND_COMPARE]


def level_segments(trace: Trace, signal: int, first: int, last: int) -> list[tuple[int, int, str]]:
    """[start, end, '0'|'1'|'Z'] intervals of the tester drive state of one signal."""

    bit = 1 << signal
    out: list[tuple[int, int, str]] = []
    state = None
    since = trace.starts[first]
    for index in range(first, last + 1):
        for t, value, enable in vector_steps(trace, index):
            now = ("1" if value & bit else "0") if enable & bit else "Z"
            if now != state:
                if state is not None and t > since:
                    out.append((since, t, state))
                state, since = now, t
    end = trace.starts[last] + trace.periods[last]
    if state is not None and end > since:
        out.append((since, end, state))
    return out
