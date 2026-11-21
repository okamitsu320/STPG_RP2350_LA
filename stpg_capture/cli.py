"""``stpg-capture``: plan, mock, import and prepare cycle-addressed analyzer captures for STPG runs.

Offline commands (plan, mock, import, init-analyzer, marker, accessory-config)
never open a port. ``identify`` requires --output-dir NEW and
--confirm-identify, opens only the bound analyzer, and saves identify.json even
on an orderly failure.

A capture during a run is part of the STPG core's run: ``accessory-config``
writes a local config folder, and the core starts this tool as its external
accessory process (``stpg-runner run BUNDLE ... --external-accessory
FOLDER/external.json``, or the Runner GUI's External accessory card). The tool
then arms the analyzer right before the target segment's RUN and keeps its
capture under the run's ``accessory/capture/``. This tool never runs the STPG
itself.

Exit codes of mock/import (precedence from the top; none is a DUT PASS):
  130  cancelled
  2    rejected, error or incomplete, or the record not persisted
  4    capture kept but its timing not adopted (not ALIGNED, low confidence
       or no window coverage)
  0    capture adopted
plan returns 2 unless PLANNED; marker unless EQUIVALENT_TIMING. A core run with
the accessory uses the core's codes (stpg-runner).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from stpg_runner.artifacts import RunnerError, read_json, write_json
from stpg_runner.device import load_device_config

from . import la_protocol as lp
from .acceptance import EXIT_CANCELLED, EXIT_ERROR, capture_exit
from .config import ANALYZER_KIND, REQUEST_KIND, AnalyzerConfig, CaptureRequestSpec, load_analyzer_config
from .coordinator import offline_capture
from .mock import PICO2_LIMITS, PICO2_TURBO_LIMITS, synthesize
from .planner import plan_capture
from .timeline import load_bundle_timeline
from .trace import import_csv, import_lac
from .wiring import PRESETS as WIRING_PRESETS

PRESETS = {"pico2-v6-turbo": PICO2_TURBO_LIMITS, "pico2-v6": PICO2_LIMITS}


def _request_from_args(args: argparse.Namespace) -> CaptureRequestSpec:
    if args.request:
        if any(v is not None for v in (args.vector, args.line)):
            raise RunnerError("give either --request or --vector/--line, not both")
        return CaptureRequestSpec.from_dict(read_json(args.request))
    address = {"vector": args.vector} if args.vector is not None else {"line": args.line}
    if args.occurrence is not None:
        address["occurrence"] = args.occurrence
    return CaptureRequestSpec.from_dict({
        "kind": REQUEST_KIND, "schema_version": 1, "address": address,
        "before_vectors": args.before, "after_vectors": args.after, "strategy": args.strategy,
        "signals": args.signals.split(",") if args.signals else None,
        "sample_rate_hz": args.sample_rate, "pretrigger_policy": args.pretrigger,
    })


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="stpg-capture", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)

    init = commands.add_parser("init-analyzer", help="write a local analyzer binding (no device contact)")
    init.add_argument("output", type=Path)
    init.add_argument("--port", required=True)
    init.add_argument("--vid", type=lambda v: int(v, 0), required=True)
    init.add_argument("--pid", type=lambda v: int(v, 0), required=True)
    init.add_argument("--serial-number", required=True)
    init.add_argument("--limits", choices=tuple(PRESETS), required=True,
                      help="identify values of the analyzer firmware build (checked exactly when arming)")
    init.add_argument("--marker-gpio", type=int, help="default spare STPG GPIO for marker derivations (wired to the "
                      "analyzer, safe to drive); `marker --marker-gpio` can choose another free pin per pattern")
    init.add_argument("--wiring", choices=tuple(WIRING_PRESETS), default="same-gpio",
                      help="STPG -> analyzer GPIO wiring preset (same-gpio: current bench; "
                           "stpg20-23-to-la19-22: planned PCB)")

    def bundle_args(sub: argparse.ArgumentParser) -> None:
        sub.add_argument("bundle", type=Path)
        sub.add_argument("--manifest-sha256", required=True)

    plan = commands.add_parser("plan", help="plan a capture around a vector (offline)")
    bundle_args(plan)
    plan.add_argument("--analyzer", type=Path, required=True)
    plan.add_argument("--request", type=Path, help="capture request JSON (alternative to the flags below)")
    plan.add_argument("--vector", type=int, help="expanded zero-based vector ID")
    plan.add_argument("--line", type=int, help="STIL line of a vector statement")
    plan.add_argument("--occurrence", type=int, help="occurrence of --line inside Loops/Macros (0-based)")
    plan.add_argument("--before", type=int, default=16)
    plan.add_argument("--after", type=int, default=64)
    plan.add_argument("--signals", help="comma-separated signal names (default: every wired signal)")
    plan.add_argument("--strategy", choices=("auto", "pattern", "level", "marker"), default="auto")
    plan.add_argument("--sample-rate", type=int)
    plan.add_argument("--pretrigger", choices=("driver", "firmware"), default="driver")
    plan.add_argument("-o", "--output", type=Path, required=True)

    mock = commands.add_parser("mock", help="synthesize the expected waveform as a MOCK capture (offline)")
    bundle_args(mock)
    mock.add_argument("--plan", type=Path, required=True)
    mock.add_argument("--output-dir", type=Path, required=True)
    mock.add_argument("--offset-ps", type=int, default=0)
    mock.add_argument("--drift-ppm", type=float, default=0.0)

    imp = commands.add_parser("import", help="align an analyzer file (.lac or CLCapture .csv) with a plan")
    bundle_args(imp)
    imp.add_argument("--plan", type=Path, required=True)
    imp.add_argument("--trace", type=Path, required=True)
    imp.add_argument("--sample-rate", type=int, help="CSV only")
    imp.add_argument("--trigger-index", type=int, help="CSV only: sample index of the trigger (pre-trigger count)")
    imp.add_argument("--channels", help="CSV only: analyzer channel numbers (1..24) of the columns")
    imp.add_argument("--output-dir", type=Path, required=True)

    ident = commands.add_parser("identify", help="one explicitly confirmed Identify, preserving local evidence")
    ident.add_argument("--analyzer", type=Path, required=True)
    ident.add_argument("--output-dir", type=Path, required=True, help="new one-shot directory for identify.json")
    ident.add_argument("--confirm-identify", action="store_true")

    marker = commands.add_parser("marker", help="derive/build/verify a NEW marker project (offline)")
    bundle_args(marker)
    marker.add_argument("--project", type=Path, required=True)
    marker.add_argument("--analyzer", type=Path, required=True)
    marker.add_argument("--vector", type=int, required=True)
    marker.add_argument("--directory", type=Path, required=True, help="new derived project directory")
    marker.add_argument("--marker-gpio", type=int,
                        help="spare STPG GPIO for the marker (default: the analyzer binding's marker_gpio); "
                             "wired to the analyzer, unused by the pattern, safe to drive")

    config = commands.add_parser("accessory-config", help="write the local config with which the STPG core starts "
                                 "this tool for one run (offline)")
    bundle_args(config)
    config.add_argument("--analyzer", type=Path, required=True)
    config.add_argument("--plan", type=Path, required=True, help="capture plan JSON (from `plan`)")
    config.add_argument("--marker-derivation", type=Path,
                        help="marker.derivation.json of a derived marker bundle (re-verified against its source)")
    config.add_argument("--mode", choices=("device", "simulated"), required=True,
                        help="device: the bound analyzer; simulated: the analyzer model, with the core's demo STPG")
    config.add_argument("--device-config", type=Path,
                        help="the STPG binding: also run the core's file-only check (USB identity apart, segment)")
    config.add_argument("--identifier", default="stpg-la")
    config.add_argument("--directory", type=Path, required=True, help="new folder for external.json and its inputs")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "init-analyzer":
            config = AnalyzerConfig.from_dict({
                "kind": ANALYZER_KIND, "schema_version": 1, "port": args.port, "vid": args.vid, "pid": args.pid,
                "serial_number": args.serial_number, "limits": PRESETS[args.limits].to_dict(), "wiring": args.wiring,
                **({"marker_gpio": args.marker_gpio} if args.marker_gpio is not None else {})})
            if args.output.exists():
                raise RunnerError(f"{args.output} exists; not overwritten")
            write_json(args.output, config.to_dict())
            report = {"status": "WRITTEN", "path": str(args.output), "device_io_performed": False}
        elif args.command == "plan":
            timeline = load_bundle_timeline(args.bundle, manifest_sha256=args.manifest_sha256)
            report = plan_capture(timeline, _request_from_args(args), load_analyzer_config(args.analyzer))
            write_json(args.output, report)
        elif args.command == "mock":
            timeline = load_bundle_timeline(args.bundle, manifest_sha256=args.manifest_sha256)
            plan = read_json(args.plan)
            trace = synthesize(timeline, plan, offset_ps=args.offset_ps, drift_ppm=args.drift_ppm)
            report = _short(offline_capture(timeline, plan, trace, output_dir=args.output_dir, mode="mock"))
        elif args.command == "import":
            timeline = load_bundle_timeline(args.bundle, manifest_sha256=args.manifest_sha256)
            plan = read_json(args.plan)
            if args.trace.suffix.lower() == ".lac":
                trace = import_lac(args.trace)
            else:
                if args.sample_rate is None or args.trigger_index is None:
                    raise RunnerError("CSV import needs --sample-rate and --trigger-index (the CSV has neither)")
                channels = [int(c) for c in args.channels.split(",")] if args.channels else None
                trace = import_csv(args.trace, sample_rate_hz=args.sample_rate, trigger_index=args.trigger_index,
                                   channels=channels)
            report = _short(offline_capture(timeline, plan, trace, output_dir=args.output_dir, mode="import"))
        elif args.command == "identify":
            from .identify import identify_analyzer

            result = identify_analyzer(load_analyzer_config(args.analyzer), output_dir=args.output_dir,
                                       confirm_identify=args.confirm_identify)
            report = {key: result[key] for key in (
                "kind", "schema_version", "status", "analyzer", "reported", "matches_binding", "errors", "error",
                "result_file", "result_persisted", "capture_commands_written", "electrical_validation",
                "physical_calibration")}
        elif args.command == "marker":
            from .marker import derive_marker_project, verify_derivation, DERIVATION_FILE
            from stpg_runner import build_project
            derived = derive_marker_project(args.project, bundle_dir=args.bundle,
                                            manifest_sha256=args.manifest_sha256, vector=args.vector,
                                            analyzer=load_analyzer_config(args.analyzer), directory=args.directory,
                                            marker_gpio=args.marker_gpio)
            built = build_project(Path(derived["project_path"]))
            verification = verify_derivation(
                load_bundle_timeline(args.bundle, manifest_sha256=args.manifest_sha256),
                load_bundle_timeline(built.bundle_dir, manifest_sha256=built.manifest_sha256), derived["derivation"])
            report = {"status": verification["status"], "project": derived["project_path"],
                      "bundle_dir": str(built.bundle_dir), "manifest_sha256": built.manifest_sha256,
                      "derivation_file": str(args.directory.resolve() / DERIVATION_FILE),
                      "verification": verification, "device_io_performed": False}
        else:
            from .external_config import write_external_config

            report = write_external_config(
                args.directory, bundle_dir=args.bundle, manifest_sha256=args.manifest_sha256,
                analyzer_binding=args.analyzer, capture_plan=read_json(args.plan),
                marker_derivation=read_json(args.marker_derivation) if args.marker_derivation else None,
                mode=args.mode, identifier=args.identifier,
                device=load_device_config(args.device_config) if args.device_config else None)
            report.update(status="WRITTEN", device_io_performed=False)
    except (RunnerError, OSError, ValueError, lp.ProtocolError) as exc:
        report = {"status": "REJECT", "errors": [f"{type(exc).__name__}: {exc}"]}
    except KeyboardInterrupt:
        print(json.dumps({"status": "CANCELLED"}))
        return EXIT_CANCELLED
    code = _exit_code(args.command, report)
    if args.command in ("mock", "import") and report.get("status") != "REJECT":
        report["exit"] = {"code": code[0], "reason": code[1]}
    print(json.dumps(report, sort_keys=True, default=str))
    return code[0]


def _exit_code(command: str, report: dict) -> tuple[int, str]:
    if report.get("status") == "CANCELLED":
        return EXIT_CANCELLED, "cancelled"
    if report.get("status") == "REJECT":
        return EXIT_ERROR, "rejected"
    if command == "identify" and (report.get("status") != "IDENTIFIED"
            or report.get("matches_binding") is not True or report.get("result_persisted") is not True):
        return EXIT_ERROR, "Identify incomplete or not persisted"
    if command in ("mock", "import"):
        return capture_exit(None, report, device=False)
    if command == "plan" and report.get("status") != "PLANNED":
        return EXIT_ERROR, "not PLANNED"
    if command == "marker" and report.get("status") != "EQUIVALENT_TIMING":
        return EXIT_ERROR, "marker timing equivalence rejected"
    return 0, "done"


def _short(capture: dict) -> dict:
    """Capture result without the embedded plan and byte journal (both stay in capture.json)."""

    return {key: value for key, value in capture.items() if key not in ("plan", "journal")}


if __name__ == "__main__":
    raise SystemExit(main())
