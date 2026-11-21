"""One explicit analyzer Identify with local prefix/lifecycle evidence, never a capture."""
from __future__ import annotations

from pathlib import Path
import time

from stpg_runner import CancellationToken
from stpg_runner.artifacts import RunnerError, write_json
from stpg_runner.execution import RunCancelled

from .config import AnalyzerConfig
from . import la_device


def identify_analyzer(config: AnalyzerConfig, *, output_dir: Path, confirm_identify: bool = False,
                      stream_factory=None, port_provider=None, cancel: CancellationToken | None = None,
                      clock=time.monotonic) -> dict:
    """Reserve evidence, open/identify/close once, return the consumed prefix on failure.

    No arm, capture, cancel byte, STPG/GPIO command, retry or protocol fallback.
    USB open/close effects are unmeasured; this is not electrically read-only or
    proof of installed firmware identity. Initial reservation failure prevents
    contact; final-write failure returns evidence in memory, not a saved verdict.
    """
    if confirm_identify is not True:
        raise RunnerError("explicit confirm_identify=True is required; no analyzer opened")
    if cancel is not None and not isinstance(cancel, CancellationToken):
        raise RunnerError("cancel must be a CancellationToken")
    config = AnalyzerConfig.from_dict(config.to_dict())
    output = Path(output_dir).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.mkdir()  # exclusive, including after failed evidence/identify
    options = {"clock": clock, "cancelled": lambda: cancel is not None and cancel.cancelled}
    if stream_factory is not None:
        options["stream_factory"] = stream_factory
    if port_provider is not None:
        options["port_provider"] = port_provider
    session = la_device.AnalyzerSession(config, **options)
    report = {"kind": "stpg_analyzer_identify_result", "schema_version": 1, "status": "PENDING",
              "config": config.to_dict(), "analyzer": None, "reported": None, "matches_binding": None,
              "errors": [], "error": None, "result_persisted": False,
              "result_file": str(output / "identify.json"), "started_s": clock(),
              "capture_commands_written": 0, "electrical_validation": False, "physical_calibration": False}
    write_json(output / "identify.json", report)  # no metadata/open if reservation fails
    try:
        session.open()
        info = session.identify()
        report["analyzer"] = info.to_dict()
        if session.buffer:
            raise la_device.AnalyzerError("unexpected consumed bytes after the analyzer identify reply")
        if cancel is not None and cancel.cancelled:
            raise la_device.AnalyzerCancelled("cancelled after the analyzer identify reply")
        report["status"] = "IDENTIFIED"
    except (la_device.AnalyzerCancelled, RunCancelled, KeyboardInterrupt) as exc:
        report["status"] = "CANCELLED"
        report["errors"].append(f"{type(exc).__name__}: {exc}")
    except BaseException as exc:
        # The byte adapter journals read/write interruptions before wrapping
        # them in AnalyzerError. Retain the cancellation and its raw prefix.
        report["status"] = "CANCELLED" if isinstance(exc.__cause__, (KeyboardInterrupt, RunCancelled)) else "REJECT"
        report["errors"].append(f"{type(exc).__name__}: {exc}")
    finally:
        try:
            session.close()
        except BaseException as exc:
            report["status"] = "REJECT"
            report["errors"].append(f"close: {type(exc).__name__}: {exc}")
    if session.info is not None:
        report["reported"] = session.info.to_dict()
        report["matches_binding"] = session.info == config.limits
    report["transport"] = {"counters": session.evidence(), "journal": list(session.journal),
                           "pending_hex": bytes(session.buffer).hex()}
    report["completed_s"] = clock()  # before final filesystem checkpoint, not a physical timestamp
    report["error"] = "; ".join(report["errors"]) or None
    try:
        report["result_persisted"] = True
        write_json(output / "identify.json", report)
    except BaseException as exc:
        report["status"], report["result_persisted"] = "REJECT", False
        report["errors"].append(f"checkpoint: {type(exc).__name__}: {exc}")
        report["error"] = "; ".join(report["errors"])
    return report
