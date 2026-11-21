"""Capture records shared by the accessory process (``accessory.py``) and the offline mock/import commands.

A capture during a run happens in the accessory process that the STPG core starts (external accessory contract
v1): the core owns the STPG and its executor, this tool owns only the analyzer. Arming at the RUN boundary removes
exposure to preflight, upload and earlier segments, but not to the state held before RUN or host scheduling
latency; the plan's uniqueness check and the alignment cover those. The STPG result (``stpg/run.json``) and the
capture (``accessory/capture/capture.json``) are separate records; neither is a DUT verdict.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from stpg_runner.artifacts import RunnerError, canonical_json, write_json
from stpg_runner.results import read_run_result, run_result_path

from . import la_protocol as lp
from .acceptance import timing_adoption
from .align import align
from .config import AnalyzerConfig
from .la_protocol import AnalyzerInfo
from .planner import validate_plan
from .timeline import BundleTimeline
from .trace import LaTrace, save_native
from .wiring import Wiring

RESULT_KIND = "stpg_capture_result"
FIRMWARE_POLL_S = 2.5  # the analyzer checks for completion once per ~2 s while capturing
MIN_LINK_RATE_BPS = 200_000


def plan_digest(plan: dict) -> str:
    return hashlib.sha256(canonical_json(plan)).hexdigest()


def _offline_analyzer(plan: dict) -> AnalyzerConfig:
    """The plan's own analyzer limits/wiring as a binding (no port: mock/import never open one)."""

    analyzer = plan.get("analyzer") if isinstance(plan, dict) else None
    if not isinstance(analyzer, dict):
        raise RunnerError("capture plan has no analyzer section")
    try:
        return AnalyzerConfig("OFFLINE", 1, 1, "OFFLINE", AnalyzerInfo.from_dict(analyzer.get("limits")),
                              Wiring.from_config(analyzer.get("wiring")), None)
    except (lp.ProtocolError, ValueError, TypeError, AttributeError) as exc:
        raise RunnerError(f"capture plan analyzer section is invalid: {exc}") from exc


def collect_timeout(plan: dict, request: lp.CaptureRequest) -> float:
    after_trigger = plan["samples"]["post"] * plan["sample_period_ps"] / 1e12
    transfer = lp.expected_capture_bytes(request) / MIN_LINK_RATE_BPS
    return FIRMWARE_POLL_S + after_trigger + transfer + 3.0


def capture_result(*, plan: dict, outcome: str, analyzer: AnalyzerConfig, bundle_dir: Path, request: lp.CaptureRequest | None,
                   journal: list, trace: LaTrace | None, alignment: dict | None, run_dir: Path | None,
                   run_summary: dict | None, errors: list[str], mode: str, samples: dict | None,
                   record: str = "FINAL", session: dict | None = None, hook: dict | None = None,
                   partial: dict | None = None, marker: dict | None = None) -> dict:
    run_identity = None
    if run_dir is not None and any((run_dir / name).is_file() for name in ("run.json", "run.json.gz")):
        record_path = run_result_path(run_dir)
        digest = hashlib.sha256(record_path.read_bytes()).hexdigest()
        read_run_result(record_path, expected_sha256=digest)
        run_identity = {"path": str(record_path), "sha256": digest}
    result = {
        "kind": RESULT_KIND, "schema_version": 1, "mode": mode, "outcome": outcome, "record": record,
        "result_persisted": False,
        "alignment_status": alignment["status"] if alignment else "NOT_ATTEMPTED",
        "dut_judgement": None, "electrical_validation": False, "physical_calibration": False,
        "plan_sha256": plan_digest(plan), "plan": plan,
        "bundle_manifest_sha256": plan["bundle"]["manifest_sha256"], "bundle_dir": str(bundle_dir),
        "analyzer": {"binding": analyzer.to_dict(), "request": request.to_dict() if request else None,
                     "request_hex": request.frame().hex() if request else None, "session": session},
        "arming": hook, "marker": marker,
        "trace": trace.summary() if trace else None, "samples": samples, "partial": partial, "alignment": alignment,
        "run": {"directory": str(run_dir) if run_dir else None, "result": run_identity, "summary": run_summary},
        "journal": journal, "errors": errors,
    }
    result["timing"] = None
    return result


def _finish(result: dict, path: Path) -> dict:
    """Persist the final record (atomic replace, no retry) and state truthfully whether it was persisted."""

    result["timing"] = timing_adoption({**result, "result_persisted": True})
    try:
        result["result_persisted"] = True
        write_json(path, result)
    except BaseException as exc:  # noqa: BLE001 - reported; the previous checkpoint stays on disk
        result["result_persisted"] = False
        result["errors"].append(f"final capture checkpoint: {type(exc).__name__}: {exc}")
        if result["outcome"] in ("CAPTURED", "MOCK", "IMPORTED"):
            result["outcome"] = "PERSIST_ERROR"
        result["timing"] = timing_adoption(result)
    return result


def offline_capture(timeline: BundleTimeline, plan: dict, trace: LaTrace, *, output_dir: Path, mode: str,
                    analyzer: AnalyzerConfig | None = None) -> dict[str, Any]:
    """Align an imported or synthesized trace and store it like a device capture (mode: import/mock)."""

    plan = validate_plan(timeline, plan, analyzer or _offline_analyzer(plan))
    output = Path(output_dir)
    output.mkdir(parents=True)
    alignment = align(timeline, plan, trace)
    samples_info = save_native(trace, output)
    result = {
        "kind": RESULT_KIND, "schema_version": 1, "mode": mode, "outcome": "MOCK" if mode == "mock" else "IMPORTED",
        "record": "FINAL", "result_persisted": False,
        "alignment_status": alignment["status"], "dut_judgement": None, "electrical_validation": False,
        "physical_calibration": False, "plan_sha256": plan_digest(plan), "plan": plan,
        "bundle_manifest_sha256": plan["bundle"]["manifest_sha256"], "bundle_dir": str(timeline.bundle_dir),
        "analyzer": {"binding": analyzer.to_dict() if analyzer else None, "request": None, "request_hex": None,
                     "session": None},
        "arming": None, "marker": None,
        "trace": trace.summary(), "samples": samples_info, "partial": None, "alignment": alignment,
        "run": {"directory": None, "result": None, "summary": None}, "journal": [], "errors": [],
        "timing": None,
    }
    return _finish(result, output / "capture.json")
