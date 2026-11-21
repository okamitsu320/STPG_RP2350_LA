"""Write the local config with which the STPG core starts this accessory for one run (files only, no device).

The core's Run (GUI External accessory card, or ``stpg-runner run --external-accessory``) reads one
``stpg_external_accessory`` JSON file: the program to start (this package's ``accessory`` module through the
current Python, isolated mode), the analyzer binding and the accessory plan it passes on, their SHA-256, the target
segment, the mode and a reply limit. This module writes such a folder:

    <folder>/external.json   the config to choose in the core
    <folder>/binding.json    a byte copy of the analyzer binding
    <folder>/plan.json       stpg_la_accessory_plan: the capture plan and, for a marker bundle, its derivation

Everything is checked offline first (the plan is derived again from the bundle and the binding, a marker bundle's
derivation is re-verified), so a config that the accessory would reject at ``open`` is not written. Nothing here
opens a port or starts a process. The folder must be new; a written folder is never changed (the core refuses a run
whose config changed after its confirmation).
"""

from __future__ import annotations

import gzip
import hashlib
import math
import os
import sys
import time
from pathlib import Path

from stpg_runner import DeviceConfig, ExternalAccessorySpec, load_external_accessory, prepare_external_accessory
from stpg_runner.artifacts import RunnerError, canonical_json, write_json

from .accessory import PLAN_KIND
from .config import AnalyzerConfig
from .coordinator import collect_timeout
from .marker import check_marker_bundle
from .planner import capture_request_from_plan, validate_plan
from .timeline import load_bundle_timeline

CONFIG_FILE = "external.json"
MAX_TIMEOUT_S = 20.0  # the core's arm/complete reply limit
MIN_TIMEOUT_S = 5.0
MAX_OPEN_TIMEOUT_S = 3600.0


def _strict_binding(data: bytes) -> dict:
    """The binding exactly as the accessory will read its byte copy (gzip allowed, strict JSON)."""

    from .accessory import AccessoryError, _strict_json

    try:
        return _strict_json(gzip.decompress(data) if data[:2] == b"\x1f\x8b" else data)
    except AccessoryError as exc:
        raise RunnerError(f"analyzer binding: {exc}") from exc


def accessory_command(python: str | None = None) -> list[str]:
    """This program as the core starts it: absolute interpreter, isolated mode (no cwd or PYTHON* lookup).

    The path is made absolute but symbolic links are kept: a virtual environment's ``python`` is a link whose
    target is the base interpreter, which would not see this environment's packages.
    """

    return [os.path.abspath(python or sys.executable), "-I", "-m", "stpg_capture.accessory"]


def reply_limit(plan: dict, analyzer: AnalyzerConfig) -> float:
    """Seconds the core waits for each reply: the bounded capture wait plus a margin, within 5..20 s.

    The after-terminal reply comes only after the samples arrived, so a capture whose own wait is longer than the
    core's limit cannot be used in a run; plan a shorter window or a higher link rate.
    """

    wait = collect_timeout(plan, capture_request_from_plan(plan, analyzer))
    if wait + 1.0 > MAX_TIMEOUT_S:
        raise RunnerError(f"this capture may need {wait:.1f} s to arrive, more than the core's "
                          f"{MAX_TIMEOUT_S:.0f} s reply limit; plan fewer samples or a shorter window")
    return round(min(MAX_TIMEOUT_S, max(MIN_TIMEOUT_S, wait + 2.0)), 1)


def open_limit(check_s: float) -> float:
    """Host-only validation allowance, not an analyzer/device timing measurement."""
    if (type(check_s) not in (int, float) or not 0 <= check_s < MAX_OPEN_TIMEOUT_S - 2
            or not math.isfinite(check_s)):
        raise RunnerError("offline validation exceeds the supported open allowance; use a smaller bundle")
    return min(MAX_OPEN_TIMEOUT_S, max(60.0, float(math.ceil(2 * check_s + 10))))


def write_external_config(directory: Path, *, bundle_dir: Path, manifest_sha256: str,
                          analyzer_binding: Path | AnalyzerConfig,
                          capture_plan: dict, marker_derivation: dict | None = None, mode: str = "device",
                          identifier: str = "stpg-la", device: DeviceConfig | None = None,
                          command: list[str] | None = None, open_timeout_s: float | None = None,
                          close_timeout_s: float = 5.0) -> dict:
    """Check and write a new accessory config folder; returns what the core will show and use."""

    if mode not in ("device", "simulated"):
        raise RunnerError("mode must be device or simulated")
    if isinstance(analyzer_binding, AnalyzerConfig):  # e.g. the demo's simulated analyzer: written as canonical JSON
        analyzer = AnalyzerConfig.from_dict(analyzer_binding.to_dict())
        binding_bytes = canonical_json(analyzer.to_dict()) + b"\n"
    else:
        binding_bytes = Path(analyzer_binding).read_bytes()
        analyzer = AnalyzerConfig.from_dict(_strict_binding(binding_bytes))
    started = time.monotonic()  # host-only measurement; repeated before READY under its own allowance
    timeline = load_bundle_timeline(Path(bundle_dir), manifest_sha256=manifest_sha256)
    plan = validate_plan(timeline, capture_plan, analyzer)
    marker = check_marker_bundle(timeline, marker_derivation, analyzer=analyzer)
    check_s = time.monotonic() - started
    timeout = reply_limit(plan, analyzer)
    opened = open_limit(check_s) if open_timeout_s is None else open_timeout_s
    folder = Path(directory).expanduser().resolve()
    bundle = Path(bundle_dir).resolve()
    if folder == bundle or bundle in folder.parents:
        raise RunnerError("the accessory config must be outside the immutable bundle")
    plan_bytes = canonical_json({"kind": PLAN_KIND, "schema_version": 1, "capture_plan": plan,
                                 "marker_derivation": marker_derivation}) + b"\n"
    argv = list(command or accessory_command())
    config = {"kind": "stpg_external_accessory", "schema_version": 1, "identifier": identifier, "command": argv,
              "executable_sha256": hashlib.sha256(Path(argv[0]).read_bytes()).hexdigest(),
              "binding_file": "binding.json", "binding_sha256": hashlib.sha256(binding_bytes).hexdigest(),
              "plan_file": "plan.json", "plan_sha256": hashlib.sha256(plan_bytes).hexdigest(),
              "segment": plan["bundle"]["segment"], "mode": mode, "timeout_s": timeout,
              "open_timeout_s": opened, "close_timeout_s": close_timeout_s}
    ExternalAccessorySpec.from_dict(config, base=folder)  # limits/shape before creating output
    folder.parent.mkdir(parents=True, exist_ok=True)
    folder.mkdir()  # new folder only
    (folder / "binding.json").write_bytes(binding_bytes)
    (folder / "plan.json").write_bytes(plan_bytes)
    path = folder / CONFIG_FILE
    write_json(path, config)
    spec = load_external_accessory(path)  # the core's own schema check
    checked = None
    if device is not None:  # the core's file-only preparation: USB identity apart from the STPG, segment in range
        checked = prepare_external_accessory(bundle, manifest_sha256=manifest_sha256, device=device,
                                             spec=spec).to_dict()
    return {"config": str(path), "identifier": identifier, "mode": mode, "segment": config["segment"],
            "timeout_s": timeout, "open_timeout_s": opened, "close_timeout_s": close_timeout_s,
            "check_s": round(check_s, 2), "trigger": plan["trigger"]["condition"], "marker": marker is not None,
            "analyzer": {"port": analyzer.port, "serial_number": analyzer.serial_number},
            "checked_by_core_sdk": checked is not None,
            "notes": [f"Choose {path} in the core's External accessory card (or pass it to stpg-runner run "
                      "--external-accessory). The core starts this program for that run only.",
                      "The program is the current Python in isolated mode; its hash does not attest the installed "
                      "packages: keep this environment unchanged, or write a new config after an update.",
                      f"Host open/validation allowance: {opened} s (offline check here {check_s:.1f} s; not device timing).",
                      f"Arm/complete reply limit: {timeout} s. Normal stdin-EOF exit grace: {close_timeout_s} s.",
                      "A slower host can still exceed the declared allowance; there is no retry or fallback.",
                      "A changed file in this folder makes the core refuse the run; write a new folder instead."]}
