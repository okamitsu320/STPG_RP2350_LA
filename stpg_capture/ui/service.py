"""What the logic-analyzer page does: bindings, Identify, planning, offline captures, marker patterns, the
accessory config for a core run, and the capture results (this tool's own process; the STPG core never loads it).

Analyzer bindings live in ``<config dir>/analyzers/`` (the folder the STPG Runner GUI lists read-only, by USB
identity) and marker derivation records in ``<config dir>/derivations/<derived manifest sha256>.json``. Everything
here is offline except Identify, which opens only the bound analyzer port after an explicit request.
"""

from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
import re
import secrets
import string
import threading
from typing import Any

from stpg_runner import load_device_config
from stpg_runner.artifacts import RunnerError, identity, read_json, write_json
from stpgview.config_location import OPTION, ConfigLocation, describe_config_dir, state_text

from ..acceptance import timing_adoption
from ..align import overlay_segments
from ..config import ANALYZER_KIND, AnalyzerConfig, CaptureRequestSpec
from ..coordinator import offline_capture
from ..external_config import write_external_config
from ..marker import (DERIVATION_FILE, MARKER_SIGNAL, check_marker_bundle, derive_marker_project, is_marker_bundle,
                      marker_candidates, verify_derivation)
from ..mock import PICO2_LIMITS, PICO2_TURBO_LIMITS, ModelAnalyzer, synthesize
from ..planner import PLAN_KIND, plan_capture, validate_plan
from ..timeline import level_segments, load_bundle_timeline
from ..trace import import_csv, import_lac, load_native
from ..wiring import PRESETS as WIRING_PRESETS, STPG_DUT_GPIO, Wiring

SIMULATED = "simulated"
LIMITS = {"pico2-v6-turbo": PICO2_TURBO_LIMITS, "pico2-v6": PICO2_LIMITS}
NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
# The demo analyzer: the same USB identity as the STPG Runner GUI's simulated "Reset LA" target.
SIMULATED_ANALYZER = AnalyzerConfig.from_dict({
    "kind": ANALYZER_KIND, "schema_version": 1, "port": "SIMULATED-LA", "vid": 0x2E8A, "pid": 0x000A,
    "serial_number": "SIMULATED-LA-0001", "limits": PICO2_TURBO_LIMITS.to_dict(), "wiring": "same-gpio"})


def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _path(value: Any, what: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise RunnerError(f"{what}: a path is required")
    return Path(value.strip()).expanduser().resolve()


class LaService:
    def __init__(self, *, config_dir: Path, results_dir: Path, device_io: bool = True, demo: bool = False,
                 config_location: ConfigLocation | None = None) -> None:
        self.config_dir = Path(config_dir)
        self.location = config_location or ConfigLocation(self.config_dir, OPTION)
        self.results_dir = Path(results_dir)
        self.device_io = device_io
        self.demo = demo
        self.lock = threading.Lock()
        self.device_lock = threading.Lock()  # one Identify at a time from this page
        self.plans: dict[str, dict] = {}  # plan_id -> {plan, analyzer name, bundle}
        self.captures: dict[str, dict] = {}

    # ---- state ---------------------------------------------------------------------------------------------
    def config_info(self) -> dict:
        """The settings folder of this start, why it was chosen and what is in it now (names only)."""

        info = describe_config_dir(self.config_dir)
        return {"path": str(self.config_dir), "source": self.location.source,
                "source_text": self.location.source_text, "notes": list(self.location.notes),
                "text": state_text(info), **info}

    def state(self) -> dict:
        return {"config_dir": str(self.config_dir), "config": self.config_info(),
                "results_dir": str(self.results_dir), "device_io": self.device_io, "demo": self.demo, "limits": {k: v.to_dict() for k, v in LIMITS.items()},
                "wiring_presets": sorted(WIRING_PRESETS), "stpg_gpio": list(STPG_DUT_GPIO),
                "marker_signal": MARKER_SIGNAL}

    # ---- analyzer bindings -----------------------------------------------------------------------------------
    def _folder(self) -> Path:
        return self.config_dir / "analyzers"

    def _binding_path(self, name: Any) -> Path:
        if not isinstance(name, str) or not NAME.match(name) or name == SIMULATED:
            raise RunnerError("a binding name is 1..64 letters, digits, '.', '_' or '-' (not 'simulated')")
        return self._folder() / f"{name}.json"

    def analyzer(self, name: str) -> AnalyzerConfig:
        if name == SIMULATED and self.demo:
            return SIMULATED_ANALYZER
        path = self._binding_path(name)
        if not path.is_file():
            raise RunnerError(f"no analyzer binding named {name!r}")
        return AnalyzerConfig.from_dict(read_json(path))

    def analyzers(self) -> dict:
        items = []
        if self.demo:
            items.append({"name": SIMULATED, "simulated": True, "config": SIMULATED_ANALYZER.to_dict(),
                          "wiring_rows": self.wiring(SIMULATED_ANALYZER.to_dict()["wiring"])["rows"]})
        folder = self._folder()
        try:  # an unreadable folder is reported, never shown as "no bindings"
            paths = sorted(p for p in folder.iterdir() if p.suffix == ".json") if folder.is_dir() else []
        except OSError as exc:
            raise RunnerError(f"cannot read the settings folder {folder}: {exc.strerror or exc}") from exc
        for path in paths:
            item: dict[str, Any] = {"name": path.stem, "path": str(path), "simulated": False}
            try:
                config = AnalyzerConfig.from_dict(read_json(path))
                item["config"] = config.to_dict()
                item["wiring_rows"] = self.wiring(item["config"]["wiring"])["rows"]
            except (RunnerError, ValueError, TypeError, KeyError) as exc:
                item["error"] = str(exc)
            items.append(item)
        return {"folder": str(folder), "items": items, "config": self.config_info()}

    def save_analyzer(self, request: dict) -> dict:
        path = self._binding_path(request.get("name"))
        config = AnalyzerConfig.from_dict({**(request.get("config") or {}), "kind": ANALYZER_KIND,
                                           "schema_version": 1})
        if path.exists() and request.get("replace") is not True:
            raise RunnerError(f"{path.name} exists; tick 'replace' to overwrite it")
        path.parent.mkdir(parents=True, exist_ok=True)
        write_json(path, config.to_dict())
        return {"path": str(path), "config": config.to_dict()}

    def delete_analyzer(self, request: dict) -> dict:
        path = self._binding_path(request.get("name"))
        if not path.is_file():
            raise RunnerError("no such binding")
        path.unlink()
        return {"deleted": str(path)}

    @staticmethod
    def wiring(value: Any) -> dict:
        try:
            rows = Wiring.from_config(value).rows()
        except (RunnerError, ValueError, TypeError) as exc:
            return {"error": str(exc), "rows": []}
        return {"rows": rows, "observable": sum(1 for r in rows if r.get("analyzer_gpio") is not None)}

    def scan_ports(self, request: dict | None = None) -> dict:
        """USB metadata of the serial ports, on request: no port is opened and nothing is selected automatically."""

        ports = []
        if self.demo:
            config = SIMULATED_ANALYZER
            ports.append({"device": config.port, "vid": config.vid, "pid": config.pid,
                          "serial_number": config.serial_number, "description": "simulated analyzer (demo)"})
        if not self.device_io:
            return {"ports": ports, "error": None, "real_scan_performed": False}
        error = None
        try:
            from serial.tools import list_ports

            for port in list_ports.comports(include_links=True):
                ports.append({"device": port.device, "vid": port.vid, "pid": port.pid,
                              "serial_number": port.serial_number, "description": port.description})
        except Exception as exc:  # noqa: BLE001 - shown to the user
            error = f"{type(exc).__name__}: {exc}"
        return {"ports": ports, "error": error, "real_scan_performed": True}

    def identify(self, request: dict) -> dict:
        """Open the bound analyzer, read its identity, close it; evidence in the results folder first."""

        name = request.get("name") or ""
        analyzer = self.analyzer(name)
        injected: dict[str, Any] = {}
        if name == SIMULATED and self.demo:
            model = ModelAnalyzer(analyzer.limits)
            port = type("Port", (), {"device": analyzer.port, "vid": analyzer.vid, "pid": analyzer.pid,
                                     "serial_number": analyzer.serial_number})()
            injected = {"stream_factory": lambda _config: model, "port_provider": lambda: [port]}
        elif not self.device_io:
            raise RunnerError("device access is disabled (--no-device-io): Identify opens the analyzer port")
        if not self.device_lock.acquire(blocking=False):
            raise RunnerError("another Identify of this page is running")
        try:
            from ..identify import identify_analyzer

            output = self.results_dir / f"{_stamp()}-analyzer-identify-{secrets.token_hex(4)}"
            report = identify_analyzer(analyzer, output_dir=output, confirm_identify=True, **injected)
        finally:
            self.device_lock.release()
        return {**report, "analyzer_name": name, "simulated": bool(injected)}

    # ---- bundles and plans --------------------------------------------------------------------------------------
    def _derivation_path(self, sha: str) -> Path:
        if not isinstance(sha, str) or not SHA256.match(sha):
            raise RunnerError("a manifest SHA-256 (64 lowercase hex) is required")
        return self.config_dir / "derivations" / f"{sha}.json"

    def derivation(self, sha: str) -> dict | None:
        path = self._derivation_path(sha)
        return read_json(path) if path.is_file() else None

    def open_bundle(self, request: dict) -> dict:
        bundle = _path(request.get("bundle"), "bundle folder")
        sha = request.get("manifest_sha256") or identity(bundle / "runner.bundle.json")["sha256"]
        timeline = load_bundle_timeline(bundle, manifest_sha256=sha)
        marker = None
        if is_marker_bundle(bundle) or any(s.name == MARKER_SIGNAL for seg in timeline.segments
                                             for s in seg.trace.signals):
            record = self.derivation(sha)
            try:
                checked = check_marker_bundle(timeline, record)
                marker = {"record": True, "status": checked["verification"]["status"],
                          "gpio": checked.get("marker_gpio")}
            except RunnerError as exc:
                marker = {"record": record is not None, "status": "REJECT", "error": str(exc)}
        return {"bundle_dir": str(bundle), "manifest_sha256": sha, "total_vectors": timeline.total_vectors,
                "segments": [{"index": s.index, "global_start": s.global_start, "global_end": s.global_end,
                              "vectors": s.vector_count,
                              "signals": [{"name": sig.name, "bit": sig.bit} for sig in s.trace.signals]}
                             for s in timeline.segments], "marker": marker}

    def _register_plan(self, plan: dict, name: str, bundle: Path, timeline) -> dict:
        plan_id = secrets.token_hex(6)
        with self.lock:
            self.plans[plan_id] = {"plan": plan, "analyzer": name, "bundle": str(bundle)}
        analyzer = self.analyzer(name)
        present = any(sig.name == MARKER_SIGNAL for seg in timeline.segments for sig in seg.trace.signals)
        candidates = [] if present else marker_candidates(timeline, analyzer)
        default = analyzer.marker_gpio if any(c["gpio"] == analyzer.marker_gpio for c in candidates) else None
        return {"plan_id": plan_id, "plan": plan, "analyzer": name, "has_marker_signal": present,
                "marker_candidates": candidates, "marker_default": default}

    def plan(self, request: dict) -> dict:
        bundle = _path(request.get("bundle"), "bundle folder")
        name = request.get("analyzer") or ""
        analyzer = self.analyzer(name)
        spec = CaptureRequestSpec.from_dict({"kind": "stpg_capture_request", "schema_version": 1,
                                             **(request.get("request") or {})})
        timeline = load_bundle_timeline(bundle, manifest_sha256=request.get("manifest_sha256") or "")
        return self._register_plan(plan_capture(timeline, spec, analyzer), name, bundle, timeline)

    def load_plan(self, request: dict) -> dict:
        bundle = _path(request.get("bundle"), "bundle folder")
        name = request.get("analyzer") or ""
        timeline = load_bundle_timeline(bundle, manifest_sha256=request.get("manifest_sha256") or "")
        plan = validate_plan(timeline, read_json(_path(request.get("path"), "plan file")), self.analyzer(name))
        return self._register_plan(plan, name, bundle, timeline)

    def _plan(self, plan_id: Any, *, planned: bool = True) -> dict:
        entry = self.plans.get(plan_id or "")
        if entry is None:
            raise RunnerError("unknown capture plan: plan again")
        if planned and entry["plan"]["status"] != "PLANNED":
            raise RunnerError("the capture plan is UNSUPPORTED; nothing to capture")
        return entry

    def save_plan(self, request: dict) -> dict:
        entry = self._plan(request.get("plan_id"), planned=False)
        path = _path(request.get("path"), "plan file")
        if path.suffix != ".json" or path.exists():
            raise RunnerError("save the plan as a new .json file")
        if any((parent / "runner.bundle.json").is_file() for parent in (path.parent, *path.parent.parents)):
            raise RunnerError("plans are local files; do not write them inside an immutable bundle")
        path.parent.mkdir(parents=True, exist_ok=True)
        write_json(path, entry["plan"])
        return {"path": str(path), **identity(path)}

    # ---- the accessory config for a core run ------------------------------------------------------------------
    def write_config(self, request: dict) -> dict:
        entry = self._plan(request.get("plan_id"))
        plan = entry["plan"]
        analyzer = self.analyzer(entry["analyzer"])
        mode = request.get("mode")
        if entry["analyzer"] == SIMULATED and mode != "simulated":
            raise RunnerError("the demo's simulated analyzer can only be used in simulated mode")
        bundle = Path(entry["bundle"])
        sha = plan["bundle"]["manifest_sha256"]
        derivation = self.derivation(sha) if is_marker_bundle(bundle) else None
        device = None
        if request.get("device_binding"):  # the core's own file-only check against an STPG binding
            device = load_device_config(_path(request["device_binding"], "STPG device binding"))
        default = (self.results_dir / "accessory-configs"
                   / f"{_stamp()}-{entry['analyzer']}-seg{plan['bundle']['segment']}-{secrets.token_hex(2)}")
        directory = Path(request.get("directory") or default).expanduser()
        binding = analyzer if entry["analyzer"] == SIMULATED else self._binding_path(entry["analyzer"])
        return write_external_config(directory, bundle_dir=bundle, manifest_sha256=sha, analyzer_binding=binding,
                                     capture_plan=plan, marker_derivation=derivation, mode=mode, device=device)

    # ---- offline captures and results ---------------------------------------------------------------------------
    def _capture_dir(self, mode: str) -> Path:
        return self.results_dir / f"{_stamp()}-capture-{mode}-{secrets.token_hex(2)}"

    def mock(self, request: dict) -> dict:
        entry = self._plan(request.get("plan_id"))
        plan = entry["plan"]
        timeline = load_bundle_timeline(Path(entry["bundle"]), manifest_sha256=plan["bundle"]["manifest_sha256"])
        trace = synthesize(timeline, plan, offset_ps=int(request.get("offset_ps") or 0))
        directory = self._capture_dir("mock")
        offline_capture(timeline, plan, trace, output_dir=directory, mode="mock")
        return self.open_capture({"path": str(directory)})

    def import_trace(self, request: dict) -> dict:
        entry = self._plan(request.get("plan_id"))
        plan = entry["plan"]
        timeline = load_bundle_timeline(Path(entry["bundle"]), manifest_sha256=plan["bundle"]["manifest_sha256"])
        path = _path(request.get("path"), "trace file")
        if path.suffix.lower() == ".lac":
            trace = import_lac(path)
        else:
            channels = request.get("channels")
            if isinstance(channels, str):
                channels = [int(x) for x in channels.split(",") if x.strip()]
            if not request.get("sample_rate_hz") or request.get("trigger_index") in (None, ""):
                raise RunnerError("a CSV has no sample rate or trigger position: give both")
            trace = import_csv(path, sample_rate_hz=int(request["sample_rate_hz"]),
                               trigger_index=int(request["trigger_index"]), channels=channels or None)
        directory = self._capture_dir("import")
        offline_capture(timeline, plan, trace, output_dir=directory, mode="import")
        return self.open_capture({"path": str(directory)})

    @staticmethod
    def _capture_file(path: Path) -> Path:
        """capture.json from itself, its folder, an accessory folder or a core run folder."""

        for candidate in (path, path / "capture.json", path / "capture" / "capture.json",
                          path / "accessory" / "capture" / "capture.json"):
            if candidate.is_file() and candidate.name == "capture.json":
                return candidate
        if (path / "accessory" / "rejected.json").is_file() or (path / "rejected.json").is_file():
            raise RunnerError("this run's accessory refused before capturing; see rejected.json in its "
                              "accessory folder")
        raise RunnerError(f"no capture.json in {path} (a run folder, its accessory/ folder or a capture folder)")

    def open_capture(self, request: dict) -> dict:
        capture_json = self._capture_file(_path(request.get("path"), "capture"))
        result = read_json(capture_json)
        if result.get("kind") != "stpg_capture_result":
            raise RunnerError("not a capture result (stpg_capture_result)")
        capture_id = secrets.token_hex(6)
        with self.lock:
            self.captures[capture_id] = {"directory": capture_json.parent, "result": result}
        short = {k: v for k, v in result.items() if k not in ("plan", "journal")}
        short["timing"] = timing_adoption(result)  # recomputed; a stored flag is not trusted
        plan = result.get("plan") or {}
        run = None
        folder = capture_json.parent
        from stpg_runner.results import read_run_result, run_result_path
        for root in (folder.parent.parent, folder.parent):
            if any((root / "stpg" / name).is_file() for name in ("run.json", "run.json.gz")):
                try:
                    run_path = run_result_path(root / "stpg")
                    report = read_run_result(run_path)
                    run = {"outcome": report.get("outcome"), "comparison": report.get("comparison"),
                           "path": str(run_path)}
                except RunnerError:
                    pass
                break
        return {"capture_id": capture_id, "directory": str(folder), "result": short, "run": run,
                "plan_summary": {"trigger": plan.get("trigger"), "channels": plan.get("channels"),
                                 "window": plan.get("window"),
                                 "sample_rate_hz": plan.get("sample_rate_hz"), "requested": plan.get("requested"),
                                 "segment": (plan.get("bundle") or {}).get("segment")},
                "waveform": bool(result.get("samples") and result.get("alignment"))}

    def waveform(self, request: dict, *, limit: int = 4000) -> dict:
        """Expected tester levels and the captured levels (in pattern time) of each planned channel."""

        entry = self.captures.get(request.get("capture_id") or "")
        if entry is None:
            raise RunnerError("unknown capture: open it again")
        result, directory = entry["result"], entry["directory"]
        plan, alignment = result["plan"], result.get("alignment") or {}
        if not result.get("samples"):
            raise RunnerError("this capture has no decoded samples")
        trace = load_native(directory, result["samples"], sample_rate_hz=result["trace"]["sample_rate_hz"],
                            channels=[c - 1 for c in result["trace"]["channels"]],
                            trigger_index=result["trace"]["trigger_index"], source=result["trace"]["source"],
                            period_ps=result["trace"].get("sample_period_ps"))
        timeline = load_bundle_timeline(Path(result["bundle_dir"]), manifest_sha256=result["bundle_manifest_sha256"])
        segment = timeline.segments[plan["bundle"]["segment"]]
        first = max(0, plan["window"]["first_vector"] - segment.global_start)
        last = min(segment.vector_count - 1, plan["window"]["last_vector"] - segment.global_start)
        names = {sig.name: i for i, sig in enumerate(segment.trace.signals)}
        lanes = []
        for row in plan["channels"]:
            index = names.get(row["signal"])
            expected = level_segments(segment.trace, index, first, last) if index is not None else []
            captured = overlay_segments(trace, plan, alignment, channel=row["channel"]) \
                if alignment.get("offset_ps") is not None else []
            if len(captured) > limit:
                stride = len(captured) / limit
                captured = [captured[int(k * stride)] for k in range(limit)]
            lanes.append({"signal": row["signal"], "channel": row["channel"],
                          "expected": [list(s) for s in expected], "captured": [list(s) for s in captured]})
        start = segment.trace.starts[first]
        end = segment.trace.starts[last] + segment.trace.periods[last]
        return {"start_ps": start, "end_ps": end, "lanes": lanes, "adopted": timing_adoption(result)["adopted"],
                "aligned": alignment.get("status") == "ALIGNED", "mode": result.get("mode")}

    def results(self) -> dict:
        items = []
        if self.results_dir.is_dir():
            for entry in sorted(self.results_dir.iterdir(), key=lambda p: p.name, reverse=True)[:100]:
                for name in ("capture.json", "identify.json"):
                    if not (entry / name).is_file():
                        continue
                    try:
                        data = read_json(entry / name)
                    except RunnerError as exc:
                        items.append({"path": str(entry), "name": entry.name, "error": str(exc)})
                        break
                    items.append({"path": str(entry), "name": entry.name, "kind": name.split(".")[0],
                                  "outcome": data.get("outcome") or data.get("status"),
                                  "mode": data.get("mode"), "alignment": data.get("alignment_status")})
                    break
        return {"root": str(self.results_dir), "items": items}

    # ---- marker patterns ------------------------------------------------------------------------------------
    def derive_marker(self, request: dict) -> dict:
        """Derive a marker project, build it with the SDK, verify the timing equivalence, keep the record.

        ``marker_gpio`` picks any free wired pin (default: the binding's ``marker_gpio``); another pin needs
        ``spare_confirmed``: the operator's statement that it is safe to drive.
        """

        from stpg_runner import build_project

        project = _path(request.get("project"), "project file")
        bundle = _path(request.get("bundle"), "bundle folder")
        sha = request.get("manifest_sha256") or ""
        vector = int(request["vector"])
        name = request.get("analyzer") or ""
        analyzer = self.analyzer(name)
        gpio = request.get("marker_gpio")
        if gpio is not None and (type(gpio) is not int or gpio not in STPG_DUT_GPIO):
            raise RunnerError("marker_gpio must be an STPG DUT GPIO number (2..18, 20..23, 26..28)")
        if gpio is not None and gpio != analyzer.marker_gpio and request.get("spare_confirmed") is not True:
            raise RunnerError(f"confirm that GPIO{gpio} is spare on this bench (nothing but the analyzer input on it) "
                              "and safe for the STPG to drive")
        default = project.parent.parent / f"{project.parent.name}-marker-v{vector}-{secrets.token_hex(2)}"
        directory = Path(request.get("directory") or default).expanduser()
        derived = derive_marker_project(project, bundle_dir=bundle, manifest_sha256=sha, vector=vector,
                                        analyzer=analyzer, directory=directory, marker_gpio=gpio)
        built = build_project(Path(derived["project_path"]))
        verification = verify_derivation(load_bundle_timeline(bundle, manifest_sha256=sha),
                                          load_bundle_timeline(built.bundle_dir, manifest_sha256=built.manifest_sha256),
                                          derived["derivation"])
        write_json(Path(derived["project_path"]).parent / "marker.verification.json", verification)
        # Registry by the derived manifest hash: a relocated bundle finds its record by content identity. A run never
        # trusts the stored verdict: the accessory re-verifies against the source bundle before READY.
        path = self._derivation_path(built.manifest_sha256)
        path.parent.mkdir(parents=True, exist_ok=True)
        write_json(path, derived["derivation"])
        return {"project_path": derived["project_path"], "bundle_dir": str(built.bundle_dir),
                "manifest_sha256": built.manifest_sha256, "verification": verification,
                "derivation_file": str(Path(derived["project_path"]).parent / DERIVATION_FILE),
                "marker_gpio": derived["derivation"]["marker"]["gpio"]}

    # ---- folder listing for the path picker --------------------------------------------------------------------
    @staticmethod
    def listing(request: dict) -> dict:
        """One folder for the path picker. An empty path opens the home folder; a path that is not a folder (or
        a drive that is not there) is an error, never silently replaced by another folder."""

        raw = request.get("path")
        folder = (Path(raw).expanduser() if raw else Path.home()).resolve()
        if folder.is_file():
            folder = folder.parent
        if not folder.is_dir():
            raise RunnerError(f"{folder} is not a folder (or does not exist)")
        entries = []
        try:
            for entry in sorted(folder.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))[:500]:
                if not entry.name.startswith("."):
                    entries.append({"name": entry.name, "dir": entry.is_dir(), "path": str(entry)})
        except PermissionError as exc:
            raise RunnerError(f"cannot list {folder}: {exc}") from exc
        return {"path": str(folder), "parent": str(folder.parent), "entries": entries, "home": str(Path.home()),
                "roots": path_roots()}

    @staticmethod
    def new_folder(request: dict) -> dict:
        """The path a new output folder will get: an existing parent folder and a new name. Nothing is created
        or reserved here; writing still refuses a folder that exists by then."""

        parent, name = request.get("parent"), request.get("name")
        if not isinstance(name, str) or not name.strip():
            raise RunnerError("type a name for the new folder")
        problem = folder_name_problem(name)
        if problem:
            raise RunnerError(problem)
        if not isinstance(parent, str) or not parent.strip() or not Path(parent).expanduser().is_absolute():
            raise RunnerError("choose the folder that will hold the new folder first")
        folder = Path(parent).expanduser().resolve()
        if not folder.is_dir():
            raise RunnerError(f"{folder} is not a folder (or does not exist)")
        try:
            taken = {entry.name.casefold() for entry in folder.iterdir()}
        except PermissionError as exc:
            raise RunnerError(f"cannot list {folder}: {exc}") from exc
        if name.casefold() in taken:
            raise RunnerError(f"{name} already exists in {folder} (names that differ only in upper and lower case "
                              "count as the same); choose a new name")
        return {"path": str(folder / name), "parent": str(folder), "name": name, "created": False}


_RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL", *(f"COM{n}" for n in range(1, 10)), *(f"LPT{n}" for n in range(1, 10))}
_BAD_NAME_CHARACTERS = set('<>:"/\\|?*') | {chr(code) for code in range(32)}


def folder_name_problem(name: str) -> str | None:
    """Why ``name`` cannot be one new folder name on every system this tool runs on, or None."""

    if name in (".", "..") or any(ch in _BAD_NAME_CHARACTERS for ch in name):
        return f"{name!r} is not a single folder name (no / \\ : * ? \" < > | or control characters)"
    if name != name.strip() or name.endswith("."):
        return f"{name!r}: a folder name cannot start or end with a space, or end with a dot"
    if name.split(".")[0].upper() in _RESERVED_NAMES:
        return f"{name!r} is a reserved device name on Windows"
    return None


def path_roots(system: str | None = None, drives=None) -> list[str]:
    """Where a path can start: on Windows every logical drive that exists now (A to Z), elsewhere "/".

    No network location is searched; a UNC path typed into the picker is opened with Go."""

    if (system or os.name) != "nt":
        return ["/"]
    if drives is None:
        listdrives = getattr(os, "listdrives", None)  # Python 3.12 and later
        try:
            drives = list(listdrives()) if listdrives else [f"{letter}:\\" for letter in string.ascii_uppercase
                                                             if os.path.exists(f"{letter}:\\")]
        except OSError:
            drives = [f"{letter}:\\" for letter in string.ascii_uppercase if os.path.exists(f"{letter}:\\")]
    return sorted({str(drive) for drive in drives}, key=str.upper)


def dumps(value: Any) -> bytes:
    return json.dumps(value, default=str).encode("utf-8")


__all__ = ["LaService", "PLAN_KIND", "SIMULATED", "SIMULATED_ANALYZER", "dumps"]
