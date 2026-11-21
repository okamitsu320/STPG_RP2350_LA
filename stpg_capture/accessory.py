"""The logic analyzer as an external accessory process of the STPG core (stdio contract v1).

The core's ``stpg-runner run --external-accessory CONFIG`` (or the GUI's External
accessory card) starts this program once per run and sends at most three
JSON-line requests on stdin, in this order:

1. ``open``: validate the binding and the plan against the bundle (the whole
   capture plan is derived again; any difference rejects), check a marker
   bundle's derivation, take the per-user USB lease, open and identify only the
   bound analyzer, then reply READY;
2. ``before_run`` (immediately before the target segment's RUN): send the
   capture request and require CAPTURE_STARTED, then reply ARMED;
3. ``after_terminal`` (only after a valid DONE/HOLD of that segment): wait a
   bounded time for the samples, decode, align, persist ``capture/capture.json``,
   close the analyzer normally and reply COMPLETE with the neutral result.

Detailed evidence stays in this program's folder (``accessory/capture/``): the
core keeps only the neutral result. On any failure the reply is ERROR (no
further request follows) and the analyzer session is closed once. As the
contract requires, no cancel byte, reset or recovery is sent: an analyzer that
was armed may stay armed until its own timeout or a Reset LA. Nothing here
talks to the STPG or to a GPIO controller.

Modes: ``device`` opens the bound serial port; ``simulated`` replays the plan's
expected levels through the byte-level analyzer model, never a real port, and
says so in every result.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import math
from pathlib import Path
import queue
import sys
import threading
from types import SimpleNamespace
from typing import Any, BinaryIO, Callable

from stpg_runner.artifacts import RunnerError, canonical_json, write_json

from . import la_protocol as lp
from .align import align
from .config import AnalyzerConfig
from .coordinator import capture_result, collect_timeout
from .acceptance import timing_adoption
from .la_device import AnalyzerError, AnalyzerSession
from .mock import ModelAnalyzer, synthesize
from .planner import capture_request_from_plan, validate_plan
from .timeline import load_bundle_timeline
from .trace import LaTrace, save_native

PROTOCOL = "stpg_external_accessory_v1"
PLAN_KIND = "stpg_la_accessory_plan"
RESULT_KIND = "stpg_external_accessory_result"
RESULT_FILE = "result.json"
CAPTURE_DIR = "capture"
MAX_LINE = 32 * 1024
LIFECYCLE = (("open", "READY"), ("before_run", "ARMED"), ("after_terminal", "COMPLETE"))
FIELDS = {
    "open": {"bundle_dir", "manifest_sha256", "binding_file", "binding_sha256", "plan_file", "plan_sha256",
             "output_dir", "segment", "mode"},
    "before_run": {"segment", "pattern_sha256"},
    "after_terminal": {"segment"},
}
IDENTITY = {"protocol", "nonce", "seq", "method"}


class AccessoryError(Exception):
    """A request this accessory rejects (reply ERROR, close the analyzer once)."""


def _strict_json(raw: bytes) -> dict:
    def pairs(items):
        keys = [key for key, _ in items]
        if len(keys) != len(set(keys)):
            raise AccessoryError("duplicate key in request")
        return dict(items)

    def constant(name):
        raise AccessoryError(f"non-finite number {name} in request")

    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs, parse_constant=constant)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AccessoryError(f"request is not UTF-8 JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise AccessoryError("request is not a JSON object")
    return value


def _input(path: str, sha256: str) -> dict:
    """An input snapshot the host named: exact stored-byte hash, then JSON (gzip allowed)."""

    data = Path(path).read_bytes()
    if hashlib.sha256(data).hexdigest() != sha256:
        raise AccessoryError(f"input hash differs from the request: {path}")
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    value = _strict_json(data)
    return value


def load_accessory_plan(value: dict) -> tuple[dict, dict | None]:
    """The plan file of this accessory: the capture plan plus an optional marker derivation record."""

    if (not isinstance(value, dict) or value.get("kind") != PLAN_KIND or type(value.get("schema_version")) is not int
            or value["schema_version"] != 1 or set(value) != {"kind", "schema_version", "capture_plan",
                                                               "marker_derivation"}):
        raise AccessoryError(f"plan file is not a {PLAN_KIND} v1 document")
    plan, derivation = value["capture_plan"], value["marker_derivation"]
    if not isinstance(plan, dict) or not (derivation is None or isinstance(derivation, dict)):
        raise AccessoryError("plan file: capture_plan must be an object and marker_derivation an object or null")
    return plan, derivation


class Capture:
    """One analyzer capture driven by the three requests (state between them)."""

    def __init__(self, *, stream_factory: Callable | None = None, port_provider: Callable | None = None,
                 host_gone: Callable[[], bool] = lambda: False) -> None:
        self.stream_factory, self.port_provider = stream_factory, port_provider
        self.host_gone = host_gone  # the host closed stdin (its cancel or failure): stop waiting, nothing is sent
        self.session: AnalyzerSession | None = None
        self.model: ModelAnalyzer | None = None
        self.root: Path | None = None
        self.capture_dir: Path | None = None
        self.timeline = self.plan = self.request = self.analyzer = None
        self.mode: str | None = None
        self.marker: dict | None = None
        self.errors: list[str] = []
        self.outcome = "PENDING"
        self.trace = self.alignment = self.samples_info = self.partial_info = None
        self.hook: dict[str, Any] = {"target_segment": None, "before_run_segments": [], "armed_segment": None,
                                     "target_terminal": None, "error": None}
        self.closed = False

    # ---- requests -----------------------------------------------------------------------------------------
    def open(self, request: dict) -> None:
        mode = request["mode"]
        if mode not in ("device", "simulated"):
            raise AccessoryError(f"unsupported mode {mode!r}")
        self.mode = mode
        self.root = Path(request["output_dir"])
        if not self.root.is_dir():
            raise AccessoryError("the host's accessory output folder does not exist")
        binding = _input(request["binding_file"], request["binding_sha256"])
        try:
            self.analyzer = AnalyzerConfig.from_dict(binding)
        except (RunnerError, ValueError, TypeError, KeyError, lp.ProtocolError) as exc:
            raise AccessoryError(f"analyzer binding rejected: {exc}") from exc
        plan, derivation = load_accessory_plan(_input(request["plan_file"], request["plan_sha256"]))
        self.timeline = load_bundle_timeline(Path(request["bundle_dir"]), manifest_sha256=request["manifest_sha256"])
        self.plan = validate_plan(self.timeline, plan, self.analyzer)  # derived again from the bundle: no trust
        target = self.plan["bundle"]["segment"]
        if request["segment"] != target:
            raise AccessoryError(f"the host arms segment {request['segment']}, the capture plan targets {target}")
        self.hook["target_segment"] = target
        from .marker import check_marker_bundle

        marker = check_marker_bundle(self.timeline, derivation, analyzer=self.analyzer)
        self.marker = {"derivation": marker["derivation"], "verification": marker["verification"]} if marker else None
        self.request = capture_request_from_plan(self.plan, self.analyzer)
        self.capture_dir = self.root / CAPTURE_DIR
        self.capture_dir.mkdir()  # exclusive: one capture per invocation
        if mode == "simulated":
            samples = synthesize(self.timeline, self.plan).samples
            self.model = ModelAnalyzer(self.analyzer.limits, provider=lambda _request: samples)
            config = self.analyzer
            factory = lambda _config: self.model  # noqa: E731
            ports = lambda: [SimpleNamespace(device=config.port, vid=config.vid, pid=config.pid,  # noqa: E731
                                             serial_number=config.serial_number)]
        else:
            factory, ports = self.stream_factory, self.port_provider
        self.session = AnalyzerSession(self.analyzer, stream_factory=factory, port_provider=ports,
                                       cancelled=self.host_gone)
        self.checkpoint()
        self.session.open()  # per-user USB identity lease first, then the bound port only
        self.session.identify()  # exact limits of the binding, or reject before READY
        self.checkpoint()

    def before_run(self, request: dict) -> None:
        self.hook["before_run_segments"].append(request["segment"])
        if request["segment"] != self.hook["target_segment"]:
            raise AccessoryError(f"armed for segment {self.hook['target_segment']}, asked at {request['segment']}")
        if request["pattern_sha256"] != self.plan["bundle"]["segment_sha256"]:
            raise AccessoryError("segment identity at RUN differs from the capture plan; analyzer not armed")
        try:
            self.session.arm(self.request)
        except BaseException as exc:
            self.hook["error"] = f"{type(exc).__name__}: {exc}"
            raise
        self.hook["armed_segment"] = request["segment"]
        self.checkpoint()

    def after_terminal(self, request: dict) -> tuple[str, str]:
        if request["segment"] != self.hook["target_segment"]:
            raise AccessoryError(f"collecting segment {self.hook['target_segment']}, asked at {request['segment']}")
        self.hook["target_terminal"] = "DONE/HOLD (reported by the host)"
        if self.model is not None:
            self.model.fire()  # simulated: the target RUN happened, release the synthesized samples
        data = self.session.wait(collect_timeout(self.plan, self.request))
        if data is None and self.host_gone():
            self.outcome = "CANCELLED"
            raise AccessoryError("the host closed the request channel while the capture was awaited")
        if data is None:
            self.outcome = "NO_DATA"
            raise AccessoryError("no complete capture within the bounded wait: trigger condition not seen "
                                 "(or analyzer busy)")
        (self.capture_dir / "capture.raw.bin").write_bytes(data)  # evidence before decoding
        raw = {"file": "capture.raw.bin", "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
        samples = lp.parse_capture(data, self.request)
        self.trace = LaTrace(self.request.frequency, tuple(self.request.channels), samples, self.plan["samples"]["pre"],
                             {"kind": "analyzer", "sha256": raw["sha256"], "size": len(data)},
                             period_ps=self.plan["sample_period_ps"])
        self.samples_info = save_native(self.trace, self.capture_dir)
        self.samples_info["raw"] = raw
        self.alignment = align(self.timeline, self.plan, self.trace)
        self.outcome = "CAPTURED"
        self.close()  # a normal close before COMPLETE
        if not self.closed:
            self.outcome = "CLEANUP_ERROR"
            raise AccessoryError("samples were received, but closing the analyzer failed: kept, not adopted")
        capture = self.persist()
        if capture["result_persisted"] is not True:
            self.outcome = "PERSIST_ERROR"
            raise AccessoryError("the capture record could not be persisted")
        neutral = {"kind": RESULT_KIND, "schema_version": 1, "mode": self.mode, "outcome": "COMPLETED",
                   "adopted": capture["timing"]["adopted"] is True, "errors": []}
        payload = canonical_json(neutral) + b"\n"
        (self.root / RESULT_FILE).write_bytes(payload)
        return RESULT_FILE, hashlib.sha256(payload).hexdigest()

    # ---- evidence and the once-close -----------------------------------------------------------------------
    def record(self, record: str) -> dict:
        return capture_result(plan=self.plan, outcome=self.outcome, analyzer=self.analyzer,
                              bundle_dir=self.timeline.bundle_dir, request=self.request,
                              journal=self.session.journal if self.session else [], trace=self.trace,
                              alignment=self.alignment, run_dir=None, run_summary=None, errors=self.errors,
                              mode=self.mode, samples=self.samples_info, record=record,
                              session=self.session.evidence() if self.session else None, hook=self.hook,
                              partial=self.partial_info, marker=self.marker)

    def checkpoint(self) -> None:
        if self.capture_dir is not None and self.plan is not None:
            write_json(self.capture_dir / "capture.json", self.record("CHECKPOINT"))

    def persist(self) -> dict:
        result = self.record("FINAL")
        result["accessory"] = {"protocol": PROTOCOL, "cancel_byte_sent": False,
                               "note": "the run itself is the core's evidence (stpg/run.json.gz beside this folder; legacy run.json is readable)"}
        result["timing"] = timing_adoption({**result, "result_persisted": True})
        try:
            result["result_persisted"] = True
            write_json(self.capture_dir / "capture.json", result)
        except BaseException as exc:  # noqa: BLE001 - reported; the previous checkpoint stays on disk
            result["result_persisted"] = False
            result["errors"].append(f"final capture record: {type(exc).__name__}: {exc}")
            result["timing"] = timing_adoption(result)
        return result

    def close(self) -> None:
        """Close the analyzer session once (reader stopped, lease released). Never a cancel byte."""

        if self.session is None or self.session.close_attempts:
            return
        try:
            self.session.close()
            self.closed = True
        except BaseException as exc:  # noqa: BLE001 - recorded, never retried
            self.errors.append(f"analyzer close: {type(exc).__name__}: {exc}")

    def abort(self, reason: str | None, *, analyzer_fault: bool = False) -> None:
        """After a failure or the host's EOF: close once, keep partial bytes, write the final record.

        Outcomes: ANALYZER_ERROR (the analyzer or its bytes failed), REJECT (any other failure here), NO_RUN (the
        host stopped before arming), HOST_STOPPED (the host stopped after ARMED without asking to collect: the
        target RUN may or may not have been sent).
        """

        if reason:
            self.errors.append(reason)
        if self.outcome in ("PENDING", "CAPTURED"):
            armed = self.hook["armed_segment"] is not None
            self.outcome = ("ANALYZER_ERROR" if reason and analyzer_fault else "REJECT" if reason else
                            "HOST_STOPPED" if armed else "NO_RUN")
        if self.session is not None and self.session.armed and self.outcome != "CAPTURED":
            self.errors.append("no cancel byte was sent (accessory contract v1): the analyzer may still be armed "
                               "until its own timeout; use Reset LA if it must be stopped now")
        self.close()
        if self.session is not None and self.session.received():
            try:
                data = self.session.partial()
                (self.capture_dir / "capture.partial.bin").write_bytes(data)
                self.partial_info = {"file": "capture.partial.bin", "size": len(data),
                                     "sha256": hashlib.sha256(data).hexdigest(),
                                     "expected": self.session.evidence()["capture_bytes_expected"]}
            except OSError as exc:
                self.errors.append(f"partial capture evidence: {type(exc).__name__}: {exc}")
        if self.capture_dir is not None and self.plan is not None:
            self.persist()
        elif reason and self.root is not None and self.root.is_dir():
            try:  # rejected before the capture folder existed (inputs, plan or bundle): say why, locally
                write_json(self.root / "rejected.json", {"kind": "stpg_la_accessory_rejection", "schema_version": 1,
                                                         "mode": self.mode, "errors": self.errors,
                                                         "analyzer_opened": False})
            except OSError:
                pass


def _reply(out: BinaryIO, request: dict, status: str, result: tuple[str, str] | None = None) -> None:
    reply = {"protocol": PROTOCOL, "nonce": request["nonce"], "seq": request["seq"], "method": request["method"],
             "status": status, "result_file": result[0] if result else None,
             "result_sha256": result[1] if result else None}
    out.write(json.dumps(reply, separators=(",", ":")).encode() + b"\n")
    out.flush()


def _check(request: dict, index: int, nonce: str | None) -> None:
    method, _ = LIFECYCLE[index]
    if request.get("protocol") != PROTOCOL:
        raise AccessoryError("unsupported protocol")
    if type(request.get("seq")) is not int or request["seq"] != index + 1 or request.get("method") != method:
        raise AccessoryError(f"request {index + 1} must be {method}")
    if not isinstance(request.get("nonce"), str) or not request["nonce"] or (nonce and request["nonce"] != nonce):
        raise AccessoryError("request nonce rejected")
    if set(request) != IDENTITY | FIELDS[method]:
        raise AccessoryError(f"{method}: unexpected or missing fields")
    if type(request.get("segment")) is not int:
        raise AccessoryError(f"{method}: segment must be an integer")
    for key in ("manifest_sha256", "binding_sha256", "plan_sha256", "pattern_sha256"):
        if key in request and not (isinstance(request[key], str) and len(request[key]) == 64):
            raise AccessoryError(f"{method}: {key} must be a SHA-256")
    for value in request.values():
        if isinstance(value, float) and not math.isfinite(value):
            raise AccessoryError("non-finite number in request")


def serve(stdin: BinaryIO, stdout: BinaryIO, stderr: BinaryIO | None = None, *, stream_factory=None,
          port_provider=None) -> int:
    """Answer one host's requests. 0: completed or the host stopped early; 1: this accessory rejected.

    A reader thread takes the request lines, so the host's EOF (its cancel or failure) is seen while a capture is
    awaited: the wait ends, the analyzer is closed once and nothing else is sent.
    """

    lines: queue.Queue = queue.Queue()
    eof = threading.Event()

    def pump() -> None:
        try:
            while True:
                raw = stdin.readline(MAX_LINE + 1)
                lines.put(raw)
                if not raw.endswith(b"\n"):  # EOF, or a partial/oversized line: nothing after it is read
                    return
        except BaseException:  # noqa: BLE001 - an unreadable stdin is the end of the requests
            lines.put(b"")
        finally:
            eof.set()

    threading.Thread(target=pump, name="accessory-stdin", daemon=True).start()
    capture = Capture(stream_factory=stream_factory, port_provider=port_provider, host_gone=eof.is_set)
    nonce = None
    index = 0

    def note(text: str) -> None:
        if stderr is not None:
            stderr.write(f"stpg-capture accessory: {text}\n".encode("utf-8", "replace")[:2000])
            stderr.flush()

    while True:
        raw = lines.get()
        if not raw:
            # The host closed stdin: after COMPLETE (normal) or because the run stopped earlier.
            if index < len(LIFECYCLE):
                capture.abort(None)
                if index:
                    note(f"host ended the run after {LIFECYCLE[index - 1][1]}; analyzer closed once")
            return 0
        request = None
        try:
            if not raw.endswith(b"\n"):
                raise AccessoryError("partial or oversized request line")
            if index >= len(LIFECYCLE):
                raise AccessoryError("request after COMPLETE")
            request = _strict_json(raw[:-2] if raw.endswith(b"\r\n") else raw[:-1])
            _check(request, index, nonce)
            nonce = request["nonce"]
            method, status = LIFECYCLE[index]
            result = getattr(capture, method)(request)
            _reply(stdout, request, status, result if method == "after_terminal" else None)
            index += 1
        except BaseException as exc:  # noqa: BLE001 - every failure is one ERROR reply and the once-close
            text = f"{type(exc).__name__}: {exc}"
            capture.abort(text, analyzer_fault=isinstance(exc, (AnalyzerError, lp.ProtocolError)))
            note(text)
            if request is not None and isinstance(request.get("nonce"), str) and type(request.get("seq")) is int \
                    and isinstance(request.get("method"), str) and not eof.is_set():
                try:
                    _reply(stdout, request, "ERROR")
                except OSError:
                    pass
            eof.wait(60)  # no further request is answered; the host closes stdin after an ERROR
            # the host's own stop (its EOF ended the wait) is not a failure of this program
            return 0 if capture.outcome == "CANCELLED" else 1


def main(argv: list[str] | None = None) -> int:
    if argv:
        print("usage: python -I -m stpg_capture.accessory   (started by the STPG core; reads requests on stdin)",
              file=sys.stderr)
        return 2
    return serve(sys.stdin.buffer, sys.stdout.buffer, sys.stderr.buffer)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
