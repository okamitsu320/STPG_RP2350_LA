"""Core and this tool as a user installs them: both wheels in one clean environment, away from the source trees.

Needs ``STPG_CORE_WHEEL`` and ``STPG_LA_WHEEL`` and, without network access, ``STPG_WHEELHOUSE``. Without the wheels
these tests are skipped, and a skip is **not** an installed-wheel pass. The accessory is the real isolated child
(``python -I -m stpg_capture.accessory``), the STPG and the analyzer are models; HOME is temporary.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.request
import zipfile

import pytest

_CORE, _LA = os.environ.get("STPG_CORE_WHEEL"), os.environ.get("STPG_LA_WHEEL")
# pip runs in a new folder; relative paths belong to the invoking terminal.
CORE = str(Path(_CORE).expanduser().resolve()) if _CORE else None
LA = str(Path(_LA).expanduser().resolve()) if _LA else None
pytestmark = [pytest.mark.stpg_host_only,
              pytest.mark.skipif(not (CORE and LA), reason="set STPG_CORE_WHEEL and STPG_LA_WHEEL; a skip is not a pass")]
BIN = "Scripts" if sys.platform == "win32" else "bin"
DRIVER = r"""
import json, sys
from pathlib import Path
from stpg_capture.config import CaptureRequestSpec
from stpg_capture.external_config import write_external_config
from stpg_capture.planner import plan_capture
from stpg_capture.timeline import load_bundle_timeline
from stpg_capture.ui.service import SIMULATED_ANALYZER, LaService
from stpg_runner import (accessory_exit, build_project, init_project, load_external_accessory,
                         run_with_external_accessory)
from stpgview.physical import patterns
from stpgview.runner.simulated import SIMULATED_DEVICE, SimulatedStpg, simulated_ports

root = Path(sys.argv[1])
patterns.main([str(root / "samples")])
project = init_project(root / "samples" / "gui_counter_long.stil", root / "project", wait_preset="none",
                       completion="hold", pin_map=root / "samples" / "gui_counter.pins.json", max_segment_vectors=1600)
built = build_project(project)
timeline = load_bundle_timeline(built.bundle_dir, manifest_sha256=built.manifest_sha256)
spec = CaptureRequestSpec.from_dict({"kind": "stpg_capture_request", "schema_version": 1, "address": {"vector": 300},
                                     "before_vectors": 8, "after_vectors": 24, "strategy": "auto"})
plan = plan_capture(timeline, spec, SIMULATED_ANALYZER)
written = write_external_config(root / "accessory", bundle_dir=built.bundle_dir, manifest_sha256=built.manifest_sha256,
                                analyzer_binding=SIMULATED_ANALYZER, capture_plan=plan, mode="simulated",
                                device=SIMULATED_DEVICE)
config = load_external_accessory(Path(written["config"]))
stpg = SimulatedStpg()
result = run_with_external_accessory(built.bundle_dir, manifest_sha256=built.manifest_sha256, device=SIMULATED_DEVICE,
                                     spec=config, output_dir=root / "run", confirm_run=True,
                                     stream_factory=lambda _: stpg, port_provider=simulated_ports)
page = LaService(config_dir=root / "settings", results_dir=root / "res", device_io=False, demo=True)
view = page.open_capture({"path": str(root / "run")})
wave = page.waveform({"capture_id": view["capture_id"]})
external = result["external"]
print("RESULT " + json.dumps({
    "segments": len(timeline.segments), "plan": plan["status"], "target_segment": plan["bundle"]["segment"],
    "command": list(config.command), "run": result["run"]["outcome"], "completed": result["run"]["completed_segments"],
    "runs": len(stpg.run_args), "external": external["outcome"], "adopted": external["adopted"],
    "methods": [r["method"] for r in external["requests"]], "returncode": external["returncode"],
    "exit": accessory_exit(result), "capture": view["result"]["outcome"], "mode": view["result"]["mode"],
    "lanes": len(wave["lanes"]), "aligned": wave["aligned"], "open_limit": config.open_timeout_s,
    "close_limit": config.close_timeout_s}))
"""


def environment(root: Path) -> dict:
    drop = ("PYTHONPATH", "STPG_CONFIG_DIR", "XDG_CONFIG_HOME", "APPDATA", "VIRTUAL_ENV")
    home = root / "home"
    home.mkdir(exist_ok=True)
    return {**{k: v for k, v in os.environ.items() if k not in drop}, "HOME": str(home), "USERPROFILE": str(home)}


@pytest.fixture(scope="module")
def installed(tmp_path_factory):
    root = tmp_path_factory.mktemp("core-la")
    subprocess.run([sys.executable, "-m", "venv", str(root / "env")], check=True, timeout=300)
    python = root / "env" / BIN / ("python.exe" if sys.platform == "win32" else "python")
    command = [str(python), "-m", "pip", "install", "--quiet", "--disable-pip-version-check"]
    if os.environ.get("STPG_WHEELHOUSE"):
        command += ["--no-index", "--find-links", str(Path(os.environ["STPG_WHEELHOUSE"]).expanduser().resolve())]
    subprocess.run(command + [CORE, LA], check=True, timeout=600, env=environment(root), cwd=root)
    return root, python


def test_both_come_from_the_wheels_and_this_wheel_carries_its_licence(installed):
    root, python = installed
    out = subprocess.run([str(python), "-c", "import stpg_capture, stpgview; print(stpg_capture.__file__, stpgview.__file__)"],
                         capture_output=True, text=True, timeout=60, cwd=root, env=environment(root))
    assert out.returncode == 0, out.stderr
    for path in out.stdout.split():
        assert Path(path).resolve().is_relative_to((root / "env").resolve()), path
    assert {"stpg-la", "stpg-capture", "stpg-view"} <= {p.stem for p in (root / "env" / BIN).iterdir()}
    with zipfile.ZipFile(LA) as wheel:
        names = wheel.namelist()
        assert any(n.endswith("licenses/LICENSE") for n in names) and any(n.endswith("licenses/NOTICE.md") for n in names)
        assert {"stpg_capture/ui/static/index.html", "stpg_capture/ui/static/la.js"} <= set(names)
        assert not [n for n in names if n.startswith(("stpgview", "stpg_runner", "tests/"))]


def test_a_plan_a_config_a_real_child_process_and_a_result_across_two_segments(installed, tmp_path):
    root, python = installed
    driver = tmp_path / "driver.py"
    driver.write_text(DRIVER, encoding="utf-8")
    out = subprocess.run([str(python), str(driver), str(tmp_path)], capture_output=True, text=True, timeout=600,
                         cwd=root, env=environment(root))
    assert out.returncode == 0, out.stdout[-2000:] + out.stderr[-2000:]
    r = json.loads(next(line for line in out.stdout.splitlines() if line.startswith("RESULT "))[7:])
    assert r["segments"] == 2 and r["plan"] == "PLANNED" and r["target_segment"] == 0
    assert r["command"] == [str(python), "-I", "-m", "stpg_capture.accessory"]  # the interpreter of this environment
    assert (r["run"], r["completed"], r["runs"]) == ("COMPLETED", 2, 2)  # segment 1 ran after the capture in segment 0
    assert (r["external"], r["adopted"], r["returncode"], r["exit"]) == ("COMPLETED", True, 0, 0)
    assert r["methods"] == ["open", "before_run", "after_terminal"]
    assert (r["capture"], r["mode"], r["aligned"]) == ("CAPTURED", "simulated", True) and r["lanes"] > 0
    assert r["open_limit"] >= 60 and r["close_limit"] == 5


def test_both_pages_start_and_the_runner_sees_the_tool_with_the_same_folder(installed, tmp_path):
    root, _ = installed
    environ = {**environment(root), "STPG_CONFIG_DIR": str(tmp_path / "shared settings")}
    pages = {}
    processes = []
    try:
        for name, extra in (("stpg-view", ["--no-device-run", "--results-dir", str(tmp_path / "r1")]),
                            ("stpg-la", ["--no-device-io", "--results-dir", str(tmp_path / "r2")])):
            process = subprocess.Popen([str(root / "env" / BIN / name), "--no-browser", *extra], stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT, text=True, cwd=root, env=environ)
            processes.append(process)
            end = time.monotonic() + 60
            while time.monotonic() < end and name not in pages:
                line = process.stdout.readline()
                assert line, f"{name} stopped"
                if "http://127.0.0.1" in line:
                    pages[name] = line[line.index("http://"):].strip()
        base, token = pages["stpg-view"].split("/?token=")
        state = json.loads(urllib.request.urlopen(urllib.request.Request(
            f"{base}/api/runner/state", headers={"X-STPG-Token": token}), timeout=20).read())
        assert state["accessory_tool"]["installed"] is True and state["accessory_tool"]["version"]
        la_base, la_token = pages["stpg-la"].split("/?token=")
        assert la_base != base and la_token != token  # its own port and token
        la = json.loads(urllib.request.urlopen(urllib.request.Request(
            f"{la_base}/api/state", headers={"X-LA-Token": la_token}), timeout=20).read())
        assert la["config"]["path"] == state["config"]["path"] and la["config"]["source"] == state["config"]["source"]
    finally:
        for process in processes:
            process.terminate()
            process.wait(10)
