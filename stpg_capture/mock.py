"""Synthesized captures and a byte-level analyzer model, for the mock UI, demos and host tests.

A synthesized trace is the *expected* waveform sampled like the analyzer would;
it proves nothing about hardware. Results built from it are labelled MOCK.
``ModelAnalyzer`` speaks the real wire format (frames, CAPTURE_REQUEST,
identify lines, binary capture) so the serial adapter is exercised byte for
byte, but it is not a timing or electrical model of the analyzer.
"""

from __future__ import annotations

import struct
import threading
from typing import Callable

from . import la_protocol as lp
from .timeline import BundleTimeline, compare_strobes, level_segments
from .trace import LaTrace

PICO2_TURBO_LIMITS = lp.AnalyzerInfo("LOGIC_ANALYZER_PICO_2_V6_0", 200_000_000, 400_000_000, 128 * 3 * 1024, 24)
PICO2_LIMITS = lp.AnalyzerInfo("LOGIC_ANALYZER_PICO_2_V6_0", 100_000_000, 200_000_000, 128 * 3 * 1024, 24)


def synthesize(timeline: BundleTimeline, plan: dict, *, offset_ps: int = 0, drift_ppm: float = 0.0,
               dut_follows_expect: bool = True) -> LaTrace:
    """Sample the expected levels at the planned rate around the planned trigger.

    Released (Z) stretches read the expected compare level of that vector when
    ``dut_follows_expect`` (a DUT that behaves as specified), otherwise 0.
    """

    segment = timeline.segments[plan["bundle"]["segment"]]
    trace = segment.trace
    p = plan["sample_period_ps"]
    pre, post = plan["samples"]["pre"], plan["samples"]["post"]
    tau = plan["trigger"]["time_ps"] + offset_ps
    count = pre + post
    slope = 1 + drift_ppm / 1e6
    times = [tau + slope * (k - pre) * p for k in range(count)]
    lo = trace.vector_at(max(0, int(min(times))))
    hi = trace.vector_at(max(0, int(max(times))))
    sig_index = {sig.name: i for i, sig in enumerate(trace.signals)}
    channels = plan["capture_channels"]
    names = {row["channel"]: row["signal"] for row in plan["channels"]}
    samples = [0] * count
    for bit, channel in enumerate(channels):
        sig = sig_index.get(names.get(channel, ""))
        if sig is None:
            continue
        segs = level_segments(trace, sig, lo, hi)
        expect = {}
        if dut_follows_expect:
            for index in range(lo, hi + 1):
                for t, s, value in compare_strobes(trace, index):
                    if s == sig and value in ("H", "L"):
                        expect[index] = 1 if value == "H" else 0
        j = 0
        for k, t in enumerate(times):
            while j + 1 < len(segs) and t >= segs[j][1]:
                j += 1
            state = segs[j][2] if segs and segs[j][0] <= t < segs[j][1] else "Z"
            if state == "Z":
                level = expect.get(trace.vector_at(max(0, int(t))), 0)
            else:
                level = int(state)
            if level:
                samples[k] |= 1 << bit
    return LaTrace(plan["sample_rate_hz"], tuple(channels), samples, pre,
                   {"kind": "mock", "offset_ps": offset_ps, "drift_ppm": drift_ppm,
                    "dut_follows_expect": dut_follows_expect}, period_ps=p)


def capture_bytes(request: lp.CaptureRequest, samples: list[int]) -> bytes:
    """The firmware's capture transmission for a single (non-burst) capture."""

    width = lp.BYTES_PER_SAMPLE[request.capture_mode]
    fmt = {1: "B", 2: "H", 4: "I"}[width]
    if len(samples) != request.total_samples:
        raise ValueError("sample count differs from the request")
    return struct.pack("<I", len(samples)) + struct.pack(f"<{len(samples)}{fmt}", *samples) + b"\x00"


class ModelAnalyzer:
    """Byte-stream analyzer model: identify, capture request, cancel byte, capture data.

    ``provider(request)`` returns the samples to send (already in request bit
    order). The capture is released by :meth:`fire` (e.g. when a simulated STPG
    receives RUN); ``auto_fire`` releases it immediately after CAPTURE_STARTED.
    """

    timeout = 2.0

    def __init__(self, info: lp.AnalyzerInfo = PICO2_TURBO_LIMITS, *,
                 provider: Callable[[lp.CaptureRequest], list[int]] | None = None, auto_fire: bool = False,
                 id_override: list[str] | None = None, reject_capture: bool = False,
                 truncate_capture: int | None = None) -> None:
        self.info = info
        self.provider = provider
        self.auto_fire = auto_fire
        self.id_override = id_override
        self.reject_capture = reject_capture
        self.truncate_capture = truncate_capture
        self.rx = bytearray()
        self.pending = bytearray()
        self.requests: list[lp.CaptureRequest] = []
        self.writes: list[bytes] = []
        self.capturing = False
        self.cancelled = False
        self.closed = 0
        self._lock = threading.Condition()

    # ---- host side (ByteStream) -------------------------------------------
    def write(self, data: bytes) -> int:
        with self._lock:
            self.writes.append(bytes(data))
            if self.capturing:
                self.capturing = False  # any byte cancels a running capture (firmware processCancel)
                self.cancelled = True
                return len(data)
            self.pending.extend(data)
            while True:
                start = self.pending.find(lp.FRAME_START)
                if start < 0:
                    self.pending.clear()
                    break
                end = self.pending.find(lp.FRAME_END, start + 2)
                if end < 0:
                    del self.pending[:start]
                    break
                message = lp.unframe(bytes(self.pending[start:end + 2]))
                del self.pending[:end + 2]
                self._handle(message)
            self._lock.notify_all()
        return len(data)

    def read(self, size: int) -> bytes:
        with self._lock:
            if not self.rx:
                self._lock.wait(timeout=min(self.timeout, 0.05))
            part = bytes(self.rx[:size])
            del self.rx[:size]
            return part

    def close(self) -> None:
        self.closed += 1

    # ---- device behaviour ----------------------------------------------------
    def _line(self, text: str) -> None:
        self.rx.extend(text.encode() + b"\r\n")

    def _handle(self, message: bytes) -> None:
        if message == bytes((lp.CMD_IDENTIFY,)):
            lines = self.id_override or [self.info.id_line, f"FREQ:{self.info.max_frequency}",
                                         f"BLASTFREQ:{self.info.blast_frequency}",
                                         f"BUFFER:{self.info.buffer_bytes}", f"CHANNELS:{self.info.channels}"]
            for line in lines:
                self._line(line)
            return
        if message[:1] == bytes((lp.CMD_CAPTURE,)) and len(message) == 1 + lp.REQUEST_SIZE:
            request = lp.CaptureRequest.unpack(message[1:])
            self.requests.append(request)
            if self.reject_capture or not self._firmware_accepts(request):
                self._line("CAPTURE_ERROR")
                return
            self._line("CAPTURE_STARTED")
            self.capturing = True
            if self.auto_fire:
                self._emit()
            return
        self._line("ERR_UNKNOWN_MSG")

    def _firmware_accepts(self, request: lp.CaptureRequest) -> bool:
        total = lp.buffer_samples(self.info, request.capture_mode)
        if request.total_samples > total or request.frequency > self.info.max_frequency:
            return False
        if request.trigger_type == lp.TRIGGER_COMPLEX:
            base, count = request.trigger_channel, request.inverted_or_count
            return 0 <= base <= 15 and 1 <= count <= 16 and base + count <= 16
        return request.trigger_type == lp.TRIGGER_EDGE and request.trigger_channel <= 24

    def _emit(self) -> None:
        request = self.requests[-1]
        samples = self.provider(request) if self.provider else [0] * request.total_samples
        data = capture_bytes(request, samples)
        if self.truncate_capture is not None:
            data = data[:self.truncate_capture]
        self.rx.extend(data)
        self.capturing = False

    def fire(self) -> None:
        with self._lock:
            if self.capturing:
                self._emit()
            self._lock.notify_all()
