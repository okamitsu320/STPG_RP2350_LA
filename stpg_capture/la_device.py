"""Serial adapter for the analyzer: one explicit port, exact identity, one open and one close.

This is the only analyzer client; it never opens the STPG port (identities and
endpoints must differ from the STPG binding, and the SDK lease is one lock per
physical USB identity whatever the role). Every byte written and every byte
consumed is kept as evidence: line-phase reads (identify, capture start) are
journalled in full, capture data stays in memory (bounded by the request) and
the coordinator stores it, complete or partial. A failed write records that the
number of bytes sent is unknown and is never resent. A capture that has not
produced data is cancelled with a single byte (the firmware stops on any input
while capturing); nothing is retried.
"""

from __future__ import annotations

import hashlib
import os
import threading
import time
from typing import Any, Callable

from stpg_runner.artifacts import RunnerError
from stpg_runner.device import DeviceLease, enumerate_ports, validate_port

from . import la_protocol as lp
from .config import AnalyzerConfig

IDENTIFY_TIMEOUT_S = 10.0
START_TIMEOUT_S = 10.0
CANCEL_DRAIN_S = 3.0
READER_JOIN_S = 2.0
LINE_MAX_BYTES = 4096  # identify/start replies are a few dozen bytes; more is a malformed reply
READ_EVENTS_MAX = 2000  # per-chunk capture read records kept in the journal (totals are always kept)
POLL_S = 0.05


class AnalyzerError(RunnerError):
    pass


class AnalyzerCancelled(KeyboardInterrupt):
    """Operator cancellation while waiting for the analyzer (handled like Ctrl-C by the SDK and coordinator)."""


def open_analyzer_serial(config: AnalyzerConfig):  # pragma: no cover - needs hardware
    """Open the analyzer CDC port. DTR is asserted (pico stdio only sends while it is set)."""

    import serial

    return serial.Serial(port=config.port, baudrate=115200, timeout=0.05, write_timeout=2.0)


def _endpoint(name: str) -> str:
    return os.path.realpath(name) if os.name != "nt" else name.casefold()


class AnalyzerSession:
    def __init__(self, config: AnalyzerConfig, *, stpg_identity: tuple[int, int, str] | None = None,
                 stpg_port: str | None = None,
                 stream_factory: Callable[[AnalyzerConfig], Any] | None = None,
                 port_provider: Callable[[], list[Any]] | None = None,
                 clock: Callable[[], float] = time.monotonic,
                 cancelled: Callable[[], bool] | None = None) -> None:
        self.config = config
        self.stpg_identity = stpg_identity
        self.stpg_port = stpg_port
        self.stream_factory = stream_factory
        self.port_provider = port_provider
        self.clock = clock
        self.cancelled = cancelled or (lambda: False)
        self.journal: list[dict[str, Any]] = []
        self.read_events_dropped = 0
        self.stream = None
        self.lease: DeviceLease | None = None
        self.buffer = bytearray()
        self.info: lp.AnalyzerInfo | None = None
        self.request: lp.CaptureRequest | None = None
        self._reader: threading.Thread | None = None
        self._data = bytearray()
        self._expected = 0
        self._done = threading.Event()
        self._stop = threading.Event()
        self._read_error: str | None = None
        self.request_sent = False
        self.start_answer: str | None = None
        self.armed = False
        self.cancel_sent = False
        self.closed = False
        self.write_attempts = 0
        self.writes_completed = 0
        self.unknown_sent = False
        self.close_attempts = 0
        self.close_successes = 0
        self.reader_stopped: bool | None = None
        self.lease_released: bool | None = None
        self.line_bytes = 0

    def _log(self, event: str, **fields: Any) -> None:
        self.journal.append({"event": event, "time_s": self.clock(), **fields})

    def evidence(self) -> dict[str, Any]:
        """Counters a result can state truthfully (no claim beyond what the session observed)."""

        return {"write_attempts": self.write_attempts, "writes_completed": self.writes_completed,
                "unknown_bytes_sent": self.unknown_sent, "request_sent": self.request_sent,
                "start_answer": self.start_answer, "armed": self.armed,
                "cancel_sent": self.cancel_sent, "close_attempts": self.close_attempts,
                "close_successes": self.close_successes, "reader_started": self._reader is not None,
                "reader_stopped": self.reader_stopped, "lease_released": self.lease_released,
                "line_bytes_consumed": self.line_bytes, "capture_bytes_received": len(self._data),
                "capture_bytes_expected": self._expected or None, "read_error": self._read_error,
                "read_events_dropped": self.read_events_dropped}

    # ---- lifecycle ---------------------------------------------------------
    def open(self) -> "AnalyzerSession":
        if self.closed or self.stream is not None or self.lease is not None:
            raise AnalyzerError("analyzer session can be opened only once")
        if self.cancelled():
            raise AnalyzerCancelled("cancelled before opening the analyzer")
        if self.stpg_identity is not None and self.config.identity == tuple(self.stpg_identity):
            raise AnalyzerError("analyzer binding names the STPG device itself; refusing a second owner of that port")
        if self.stpg_port is not None and _endpoint(self.config.port) == _endpoint(self.stpg_port):
            raise AnalyzerError("analyzer port is the STPG port; refusing a second owner of that port")
        lease = DeviceLease(self.config)  # one lock per VID/PID/serial, shared with STPG-role leases
        lease.__enter__()
        self.lease, self.lease_released = lease, False
        try:
            metadata = validate_port(self.config, (self.port_provider or enumerate_ports)())  # type: ignore[arg-type]
            self._log("port.validated", metadata=metadata, lease=lease.key)
            if self.cancelled():
                raise AnalyzerCancelled("cancelled before opening the analyzer")
            self._log("open.begin")
            self.stream = (self.stream_factory or open_analyzer_serial)(self.config)
            self._log("open.end")
        except BaseException:
            lease.__exit__(None, None, None)
            self.lease, self.lease_released = None, True
            raise
        return self

    def close(self) -> None:
        """Stop the reader, close once, then release the lease (kept if a reader may still use the port)."""

        if self.closed:
            return
        self.closed = True
        self._stop.set()
        alive = False
        if self._reader is not None:
            self._reader.join(timeout=READER_JOIN_S)
            alive = self._reader.is_alive()
            if alive:
                self._log("reader.join_timeout", seconds=READER_JOIN_S)
        failure: BaseException | None = None
        if self.stream is not None:
            self.close_attempts += 1
            self._log("close.begin")
            try:
                self.stream.close()
                self.close_successes += 1
            except BaseException as exc:  # noqa: BLE001 - recorded and reported, never retried
                failure = exc
                self._log("close.error", error=f"{type(exc).__name__}: {exc}")
            finally:
                self._log("close.end")
        if alive:
            self._reader.join(timeout=READER_JOIN_S)  # closing the port normally ends a blocked read
            alive = self._reader.is_alive()
        if self._reader is not None:
            self.reader_stopped = not alive
        if self.lease is not None and not alive:
            self.lease.__exit__(None, None, None)
            self.lease, self.lease_released = None, True
        if alive:
            self._log("reader.still_running")
            raise AnalyzerError("the analyzer reader thread did not stop after close; its port lease stays held "
                                "until this process exits")
        if failure is not None:
            raise AnalyzerError(f"analyzer close failed: {type(failure).__name__}: {failure}") from failure

    # ---- I/O helpers -----------------------------------------------------------
    def _write(self, data: bytes, label: str) -> None:
        self.write_attempts += 1
        self._log("write.begin", label=label, size=len(data), hex=data.hex())
        try:
            count = self.stream.write(data)
        except BaseException as exc:
            self.unknown_sent = True
            self._log("write.error", label=label, sent="unknown", error=f"{type(exc).__name__}: {exc}")
            raise AnalyzerError(f"write to the analyzer failed ({label}); bytes sent unknown, not resent: "
                                f"{type(exc).__name__}: {exc}") from exc
        self._log("write.end", label=label, sent=count)
        if count != len(data):
            raise AnalyzerError(f"short write to the analyzer ({count} of {len(data)} bytes); not resent")
        self.writes_completed += 1

    def _read_line_part(self) -> bytes:
        try:
            part = self.stream.read(256)
        except BaseException as exc:
            self._log("read.error", phase="line", error=f"{type(exc).__name__}: {exc}",
                      pending_hex=bytes(self.buffer).hex())
            raise AnalyzerError(f"analyzer read failed: {type(exc).__name__}: {exc}") from exc
        if part:
            if len(part) > 256:
                self._log("read.oversize", phase="line", requested=256, size=len(part), hex=bytes(part).hex())
                raise AnalyzerError(f"analyzer stream returned {len(part)} bytes for a 256-byte read")
            self.line_bytes += len(part)
            self._log("read", phase="line", size=len(part), hex=bytes(part).hex())
        return part

    def _line(self, deadline: float, what: str) -> str:
        while True:
            if self.cancelled():
                self._log("line.cancelled", pending_hex=bytes(self.buffer).hex())
                raise AnalyzerCancelled(f"cancelled while waiting for the analyzer {what} reply")
            if self.clock() >= deadline:
                self._log("line.timeout", pending_hex=bytes(self.buffer).hex())
                raise AnalyzerError(f"analyzer did not answer the {what} in time")
            newline = self.buffer.find(b"\n")
            if newline >= 0:
                if newline > LINE_MAX_BYTES:
                    self._log("line.oversize", pending_hex=bytes(self.buffer).hex())
                    raise AnalyzerError(f"analyzer {what} reply exceeds {LINE_MAX_BYTES} bytes")
                raw = bytes(self.buffer[:newline])
                del self.buffer[:newline + 1]
                text = raw.decode("ascii", errors="replace").rstrip("\r")
                self._log("line", text=text, hex=raw.hex())
                return text
            if len(self.buffer) > LINE_MAX_BYTES:
                self._log("line.oversize", pending_hex=bytes(self.buffer).hex())
                raise AnalyzerError(f"analyzer {what} reply exceeds {LINE_MAX_BYTES} bytes without a line end")
            part = self._read_line_part()
            if part:
                self.buffer.extend(part)

    # ---- protocol --------------------------------------------------------------
    def identify(self) -> lp.AnalyzerInfo:
        if self.cancelled():
            raise AnalyzerCancelled("cancelled before identifying the analyzer")
        self._write(lp.identify_frame(), "identify")
        deadline = self.clock() + IDENTIFY_TIMEOUT_S
        lines = [self._line(deadline, "identify") for _ in range(5)]
        try:
            info = lp.parse_identify(lines)
        except lp.ProtocolError as exc:
            self._log("identify.malformed", lines=lines, error=str(exc))
            raise
        self.info = info
        if info != self.config.limits:
            raise AnalyzerError(f"analyzer reports {info.to_dict()}, binding expects {self.config.limits.to_dict()}; "
                                "update the local binding explicitly if this is the intended analyzer")
        return info

    def arm(self, request: lp.CaptureRequest) -> None:
        if self.info is None:
            raise AnalyzerError("identify the analyzer before arming")
        if self.request_sent:
            raise AnalyzerError("this session already sent a capture request; one capture per session")
        if self.cancelled():
            raise AnalyzerCancelled("cancelled before arming the analyzer")
        self.request = request
        self.request_sent = True
        self._write(request.frame(), "capture_request")
        deadline = self.clock() + START_TIMEOUT_S
        answer = self._line(deadline, "capture start")
        self.start_answer = answer
        if answer != "CAPTURE_STARTED":
            raise AnalyzerError(f"analyzer refused the capture request: {answer!r}")
        self._expected = lp.expected_capture_bytes(request)
        self._data = bytearray(self.buffer)  # capture bytes that arrived with the start line
        self.buffer.clear()
        self.armed = True
        self._log("armed", expected=self._expected, carried=len(self._data))
        if len(self._data) > self._expected:
            self._read_error = f"carried capture has {len(self._data)} bytes, expected {self._expected}"
            self._log("read.oversize", phase="carried", size=len(self._data), expected=self._expected)
            self._done.set()
            raise AnalyzerError(self._read_error)
        self._reader = threading.Thread(target=self._read_capture, name="stpg-analyzer-read", daemon=True)
        self._reader.start()

    def _read_capture(self) -> None:
        read_events = 0
        try:
            while not self._stop.is_set() and len(self._data) < self._expected:
                want = min(65536, self._expected - len(self._data))
                part = self.stream.read(want)
                if not part:
                    continue
                if len(part) > want:
                    self._data.extend(part[:want])
                    self._read_error = f"stream returned {len(part)} bytes for a {want}-byte read"
                    self._log("read.oversize", phase="capture", requested=want, size=len(part),
                              extra_hex=bytes(part[want:want + LINE_MAX_BYTES]).hex())
                    break
                self._data.extend(part)
                if read_events < READ_EVENTS_MAX:
                    read_events += 1
                    self._log("read", phase="capture", size=len(part), total=len(self._data))
                else:
                    self.read_events_dropped += 1
        except BaseException as exc:  # noqa: BLE001 - reported to the coordinator
            self._read_error = f"{type(exc).__name__}: {exc}"
            self._log("read.error", phase="capture", error=self._read_error, received=len(self._data))
        finally:
            complete = len(self._data) >= self._expected
            self._log("reader.exit", received=len(self._data), complete=complete)
            if complete:
                self._log("capture.received", size=len(self._data),
                          sha256=hashlib.sha256(bytes(self._data)).hexdigest())
            self._done.set()

    def wait(self, timeout: float) -> bytes | None:
        """Capture bytes, or None if none arrived in time or the operator cancelled (the capture keeps running)."""

        if self._reader is None:
            raise AnalyzerError("analyzer is not armed")
        deadline = time.monotonic() + timeout  # a real wait, independent of an injected evidence clock
        while not self._done.is_set() and not self.cancelled() and time.monotonic() < deadline:
            self._done.wait(POLL_S)
        if self._read_error:
            raise AnalyzerError(f"analyzer read failed: {self._read_error}")
        if self._done.is_set() and len(self._data) >= self._expected:
            return bytes(self._data[:self._expected])
        return None

    def received(self) -> int:
        return len(self._data)

    def partial(self) -> bytes:
        """Every capture byte consumed so far (complete or not)."""

        return bytes(self._data)

    def cancel(self) -> bool:
        """Send the cancel byte once if a capture may be running; wait briefly for the firmware to stop.

        Nothing is sent when no request was sent, when the analyzer answered the
        request with a refusal, or when the capture already completed.
        """

        refused = not self.armed and self.start_answer is not None
        if self.cancel_sent or self.stream is None or not self.request_sent or refused or self._done.is_set():
            return False
        self.cancel_sent = True
        try:
            self._write(b"\xff", "cancel")
        finally:
            if self._reader is not None:
                self._done.wait(CANCEL_DRAIN_S)
            self._log("cancel.done", received=len(self._data), reader_done=self._done.is_set())
        return True
