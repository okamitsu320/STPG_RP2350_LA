"""This tool's manual stays true to the program: links work, no internal material, options and labels exist."""

from __future__ import annotations

from pathlib import Path
import re
import struct

import pytest

from stpg_capture import cli as capture_cli, ui
from stpg_capture.ui import server as la_server

pytestmark = pytest.mark.stpg_host_only
ROOT = Path(__file__).resolve().parents[1]
MANUAL = ROOT / "manual"
PAGES = sorted(MANUAL.glob("*.md"))
LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
INTERNAL = re.compile(r"\bdoc/|\bdocs/|companion/|recovery-safe|\bAI[12]\b|\bCodex\b|\bGLM\b|tools/reviews|"
                      r"stpg-capture run|run_with_capture|in-process|C:\\Users\\")
LABELS = ("Add binding…", "Scan ports", "Identify", "Wiring", "Use the reported values in this binding…", "Plan",
          "Write accessory config")


def test_every_page_is_reachable_and_links_resolve():
    index = (MANUAL / "README.md").read_text(encoding="utf-8")
    assert {p.name for p in PAGES} - {"README.md"} == {link for link in LINK.findall(index) if link.endswith(".md")}
    for page in [ROOT / "README.md", *PAGES]:
        for link in LINK.findall(page.read_text(encoding="utf-8")):
            assert link.startswith("https://") or (page.parent / link.split("#")[0]).is_file(), (page.name, link)


def test_attribution_firmware_and_support_have_distinct_destinations():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    notices = (ROOT / "NOTICE.md").read_text(encoding="utf-8")
    assert "Agustín Gimenez Bernad" in readme and "Agustín Gimenez Bernad" in notices
    assert "not affiliated with or endorsed by" in readme
    assert "https://github.com/gusmanb/logicanalyzer/releases/tag/v6.0.0.0" in LINK.findall(readme)
    assert "https://github.com/okamitsu320/STPG_RP2350_LA/issues/new/choose" in LINK.findall(readme)
    assert ("https://github.com/gusmanb/logicanalyzer/commit/4e88df784e4879c1f180456a72d71910dd15faf1"
            in LINK.findall(notices))
    assert "2026-11-21" in notices


@pytest.mark.parametrize("page", [ROOT / "README.md", *PAGES], ids=lambda p: p.name)
def test_no_internal_material_or_old_commands(page):
    assert INTERNAL.search(page.read_text(encoding="utf-8")) is None


def test_options_and_commands_named_in_the_manual_exist():
    known = {o for action in la_server.build_parser()._actions for o in action.option_strings}
    commands = capture_cli.build_parser()._subparsers._group_actions[0].choices
    for name, sub in commands.items():
        known |= {o for action in sub._actions for o in action.option_strings}
    text = "\n".join(page.read_text(encoding="utf-8") for page in PAGES if page.name != "testing.md")
    assert set(re.findall(r"(?<![\w-])--[a-z][a-z-]+", text)) <= known
    assert {"accessory-config", "marker"} <= set(commands) and "run" not in commands


def test_labels_named_in_the_manual_are_on_the_page():
    static = Path(ui.__file__).parent / "static"
    source = "\n".join(path.read_text(encoding="utf-8") for path in static.iterdir() if path.suffix in (".js", ".html"))
    text = "\n".join(page.read_text(encoding="utf-8") for page in PAGES)
    for label in LABELS:
        assert label in text and label in source, label


def test_the_limits_a_reader_must_know_are_stated():
    setup = (MANUAL / "setup.md").read_text(encoding="utf-8")
    assert "Analyzer GPIO0 and GPIO1" in setup and "23 of 24 pins" in setup and "common ground" in setup
    capture = (MANUAL / "capture.md").read_text(encoding="utf-8")
    assert "one run captures one segment" in capture and "no cancel byte is sent" in capture
    assert "not STPG compare results" in capture


def test_every_test_named_in_the_catalogue_exists():
    rows = [line for line in (MANUAL / "testing.md").read_text(encoding="utf-8").splitlines()
            if line.startswith("| ") and "::" in line]
    assert rows
    for row in rows:
        current = None
        for item in re.findall(r"`([^`]*::[^`]*)`", row):
            name, test = item.split("::")
            current = current if name == "…" else name
            assert f"def {test}(" in (Path(__file__).parent / current).read_text(encoding="utf-8"), item


@pytest.mark.parametrize("page", [ROOT / "README.md", MANUAL / "setup.md"])
def test_source_install_and_demo_commands_do_not_need_scripts_on_path(page):
    setup = page.read_text(encoding="utf-8")
    assert "macOS is untested" in setup
    windows = "\n".join(re.findall(r"```powershell\n(.*?)```", setup, re.S))
    posix = "\n".join(re.findall(r"```sh\n(.*?)```", setup, re.S))
    for commands, python in [(windows, ".\\stpg-env\\Scripts\\python.exe"),
                             (posix, "./stpg-env/bin/python")]:
        assert f"{python} -m pip install . ../STPG_RP2350_LA" in commands
        assert f"{python} -m stpg_capture.ui.server --demo --no-device-io" in commands
        assert "--config-dir ./settings-check --results-dir ./la-runs-check" in commands
        assert not re.search(r"<[^>]+>|Activate\.ps1|^stpg-", commands, re.M)
    for page in PAGES:
        for block in re.findall(r"```(?:sh|powershell)\n(.*?)```", page.read_text(encoding="utf-8"), re.S):
            assert not re.search(r"<[^>]+>", block), (page.name, block)


def test_real_demo_panels_are_linked_and_shipped():
    setup = (MANUAL / "setup.md").read_text(encoding="utf-8")
    assert "not a measurement" in setup and "simulated capture" in setup
    for name in ("analyzer-demo.png", "plan-simulated.png", "capture-simulated.png"):
        assert f"(images/{name})" in setup
        raw = (MANUAL / "images" / name).read_bytes()
        assert raw.startswith(b"\x89PNG\r\n\x1a\n")
        width, height = struct.unpack(">II", raw[16:24])
        assert width >= 600 and height >= 80
    assert "recursive-include manual *.md *.png" in (ROOT / "MANIFEST.in").read_text()
