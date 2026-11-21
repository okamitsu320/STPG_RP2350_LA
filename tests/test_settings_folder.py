"""stpg-la and stpg-view choose the same settings folder for the same inputs (real commands, temporary folders).

Host only. Both commands are started with no device option; analyzer Identify stays off. No real user settings are
read: HOME and the variables are set for each start.
"""

from __future__ import annotations

import hashlib
import inspect
import os
from pathlib import Path
import re
import subprocess
import sys
import time

import pytest

from stpg_capture.ui import server as la_server, service as la_service
from stpg_capture.ui.service import LaService
from stpgview.config_location import ENV_NAME, OPTION, resolve_config_dir
from stpgview.runner.store import BindingStore

from test_stpg_capture import analyzer_config

pytestmark = pytest.mark.stpg_host_only
HOME_KEY = "USERPROFILE" if sys.platform == "win32" else "HOME"
CORE = [sys.executable, "-m", "stpgview", "--no-browser", "--no-device-run"]
LA = [sys.executable, "-m", "stpg_capture.ui.server", "--no-browser", "--no-device-io"]
LINE = re.compile(r"^stpg-(?:view|la): settings folder: (.*) \((from --config-dir|from STPG_CONFIG_DIR|"
                  r"the per-user default); (.*)\)$")


def environment(home: Path, **extra) -> dict:
    drop = (ENV_NAME, "XDG_CONFIG_HOME", "APPDATA")
    return {**{k: v for k, v in os.environ.items() if k not in drop}, HOME_KEY: str(home), **extra}


def start(argv: list[str], environ: dict, cwd: Path) -> tuple[list[str], int]:
    """A tool's output until it serves or stops, and its exit code (None-like -1 when it was stopped by this test)."""

    process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=environ, cwd=cwd)
    lines, end, code = [], time.monotonic() + 30, -1
    try:
        while time.monotonic() < end:
            line = process.stdout.readline()
            if not line:
                code = process.wait(10)
                break
            lines.append(line.rstrip("\n"))
            if "Press Ctrl+C" in line:
                break
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(10)
    return lines, code


def folder_of(lines: list[str]) -> tuple[str, str, str]:
    found = [m.groups() for m in map(LINE.match, lines) if m]
    assert len(found) == 1, lines
    return found[0]


@pytest.fixture
def home(tmp_path):
    folder = tmp_path / "home"
    folder.mkdir()
    return folder


CASES = {
    "option": (lambda t: ["--config-dir", str(t / "chosen")], {}),
    "option with spaces": (lambda t: ["--config-dir", str(t / "my settings")], {}),
    "option over variable": (lambda t: ["--config-dir", str(t / "chosen")], lambda t: {ENV_NAME: str(t / "other")}),
    "variable": (lambda t: [], lambda t: {ENV_NAME: str(t / "from env")}),
    "variable with home": (lambda t: [], {ENV_NAME: "~/from home"}),
    "relative option": (lambda t: ["--config-dir", "rel/cfg"], {}),
    "relative variable": (lambda t: [], {ENV_NAME: "rel/cfg"}),
    "empty variable": (lambda t: [], {ENV_NAME: ""}),
    "blank variable": (lambda t: [], {ENV_NAME: "   "}),
    "default": (lambda t: [], {}),
}


@pytest.mark.parametrize("name", sorted(CASES))
def test_both_commands_report_the_same_folder_source_and_state(tmp_path, home, name):
    args, variables = CASES[name]
    extra = variables(tmp_path) if callable(variables) else variables
    environ = environment(home, **extra)
    core, _ = start(CORE + ["--results-dir", str(tmp_path / "r")] + args(tmp_path), environ, tmp_path)
    la, _ = start(LA + ["--results-dir", str(tmp_path / "r")] + args(tmp_path), environ, tmp_path)
    assert folder_of(core) == folder_of(la)
    assert [line.split(": ", 1)[1] for line in core if ": note:" in line] == \
        [line.split(": ", 1)[1] for line in la if ": note:" in line]
    assert list(home.iterdir()) == []  # starting created nothing, wherever the folder is


@pytest.mark.parametrize("args,variables,message", [
    (["--config-dir", ""], {}, "--config-dir is blank"),
    (["--config-dir", "   "], {ENV_NAME: "/tmp/not-used"}, "--config-dir is blank"),
    ([], {ENV_NAME: '"quoted value"'}, "wrapped in quotes"),
    ([], {ENV_NAME: "~no-such-user-for-this-test/x"}, "cannot expand"),
])
def test_both_commands_stop_the_same_way_on_an_unusable_value(tmp_path, home, args, variables, message):
    for argv, name in ((CORE, "stpg-view"), (LA, "stpg-la")):
        lines, code = start(argv + args, environment(home, **variables), tmp_path)
        assert code == 2 and any(line.startswith(f"{name}: ") and message in line for line in lines), lines
        assert not any("settings folder:" in line for line in lines)
    assert list(home.iterdir()) == []


def test_the_original_mismatch_cannot_return(tmp_path, monkeypatch):
    # Both tools used to agree on the default but not on STPG_CONFIG_DIR. One function serves both now.
    assert "default_config_dir" not in vars(la_service) and "XDG_CONFIG_HOME" not in inspect.getsource(la_service)
    assert la_server.resolve_config_dir is resolve_config_dir
    monkeypatch.setenv(ENV_NAME, str(tmp_path / "shared"))
    assert resolve_config_dir().path == (tmp_path / "shared").resolve()


def test_a_binding_written_by_this_tool_is_listed_by_the_runner_in_the_same_folder(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV_NAME, str(tmp_path / "shared folder"))
    location = resolve_config_dir()
    service = LaService(config_dir=location.path, config_location=location, results_dir=tmp_path / "r", device_io=False)
    assert service.state()["config"]["state"] == "new" and service.state()["config"]["source"] == ENV_NAME
    service.save_analyzer({"name": "bench", "config": analyzer_config().to_dict()})
    assert service.state()["config"]["counts"]["analyzers"] == 1
    listed = BindingStore(resolve_config_dir().path).list("analyzers")
    assert [(i["name"], i["config"]["serial_number"]) for i in listed] == [("bench", "PUBLIC-LA-0001")]


def test_page_state_reports_the_folder_and_an_old_file_is_explained_not_rewritten(tmp_path):
    root = tmp_path / "settings"
    (root / "analyzers").mkdir(parents=True)
    old = root / "analyzers" / "old.json"
    old.write_text('{"kind": "stpg_external_device", "schema_version": 1, "port": "COM7", "vid": 1, "pid": 2, '
                   '"serial_number": "X"}')
    digest = hashlib.sha256(old.read_bytes()).hexdigest()
    service = LaService(config_dir=root, results_dir=tmp_path / "r", device_io=False)
    assert service.state()["config"]["source"] == OPTION and service.state()["config"]["text"] == "1 analyzers"
    item = service.analyzers()["items"][0]
    assert item["name"] == "old" and item.get("error") and "config" not in item
    with pytest.raises(Exception, match="exists"):
        service.save_analyzer({"name": "old", "config": analyzer_config().to_dict()})
    assert hashlib.sha256(old.read_bytes()).hexdigest() == digest


def test_results_default_to_a_per_user_folder_not_the_start_folder():
    source = inspect.getsource(la_server.main)
    assert '"stpg-work" / "la"' in source and "Path.cwd()" not in source
