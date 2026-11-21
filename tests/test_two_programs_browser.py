"""From nothing to a capture, with both real programs in a real browser (host only).

The steps of the manual: build in ``stpg-view``, plan and write ``external.json`` in ``stpg-la``, Check and Run in the
Runner (the accessory is the real ``python -I`` child), open the run's folder in ``stpg-la``. Needs the installed core
and LA modules, selenium, Firefox and geckodriver; otherwise it is skipped,
which is NOT RUN. HOME is temporary; both start with their no-device option, so no port is listed or opened.
"""

from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
import sys
import time

import pytest

pytest.importorskip("selenium.webdriver")
from selenium.common.exceptions import NoSuchElementException, StaleElementReferenceException  # noqa: E402
from selenium.webdriver.common.by import By  # noqa: E402
from selenium.webdriver.common.keys import Keys  # noqa: E402
from selenium.webdriver.support.ui import Select  # noqa: E402

from stpg_runner.artifacts import read_json  # noqa: E402

from test_la_browser import browser  # noqa: E402,F401 - module fixture

pytestmark = pytest.mark.stpg_host_only
HOME_KEY = "USERPROFILE" if sys.platform == "win32" else "HOME"


def command(module: str) -> list[str]:
    package = module.split(".", 1)[0]
    probe = subprocess.run([sys.executable, "-I", "-c", "import importlib.util; "
                            f"print(importlib.util.find_spec({package!r}) is not None)"],
                           capture_output=True, text=True, timeout=30)
    if probe.returncode == 0 and probe.stdout.strip() == "False":
        pytest.skip("install both matching packages into this Python before the isolated module-command test")
    assert probe.returncode == 0, probe.stderr
    return [sys.executable, "-I", "-m", module]


def manual_shot(driver, name: str, selector: str) -> None:
    folder = os.environ.get("STPG_LA_MANUAL_SHOTS")
    if folder:
        path = Path(folder)
        path.mkdir(parents=True, exist_ok=True)
        size = driver.get_window_size()
        driver.set_window_size(1280, 1600)
        time.sleep(0.2)  # let the actual waveform canvas resize
        target = driver.find_element(By.CSS_SELECTOR, selector)
        driver.execute_script("arguments[0].scrollIntoView({block:'center'})", target)
        try:
            target.screenshot(str(path / name))
        finally:
            driver.set_window_size(size["width"], size["height"])


def until(predicate, what: str, timeout: float = 60.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            value = predicate()
        except (NoSuchElementException, StaleElementReferenceException):
            value = None
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


def start(argv: list[str], environ: dict, cwd: Path, running: list) -> tuple[str, str]:
    process = subprocess.Popen(argv, cwd=cwd, env=environ, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    running.append(process)
    lines = []
    for line in process.stdout:
        lines.append(line)
        if "Press Ctrl+C" in line:
            break
    folder = next(line.split("settings folder: ", 1)[1].strip() for line in lines if "settings folder:" in line)
    return re.search(r"http://\S+token=\S+", "".join(lines)).group(0), folder


def test_a_capture_from_nothing_with_both_programs(browser, tmp_path):  # noqa: F811
    view, la, examples = command("stpgview"), command("stpg_capture.ui.server"), command("stpgview.physical.patterns")
    home = tmp_path / "home"
    home.mkdir()
    environ = {k: v for k, v in os.environ.items()
               if k not in ("PYTHONPATH", "STPG_CONFIG_DIR", "XDG_CONFIG_HOME", "APPDATA")}
    environ[HOME_KEY] = str(home)
    environ["PATH"] = ""  # the documented module commands do not need console scripts on PATH
    subprocess.run([*examples, "samples"], cwd=tmp_path, env=environ, capture_output=True, check=True, timeout=30)
    driver, running = browser, []
    text = lambda element: driver.find_element(By.ID, element).text  # noqa: E731
    css = lambda selector: driver.find_element(By.CSS_SELECTOR, selector)  # noqa: E731
    is_open = lambda dialog: driver.execute_script(  # noqa: E731
        "return document.getElementById(arguments[0]).open", dialog)
    try:
        view_url, view_folder = start([*view, "--demo-device", "--no-device-run", "--no-browser",
                                       "--config-dir", "./settings-check", "--results-dir", "./runs-check"], environ, tmp_path,
                                      running)
        driver.get(view_url)
        until(lambda: driver.find_element(By.ID, "openBtn").is_displayed(), "the Viewer")
        driver.find_element(By.ID, "openBtn").click()
        driver.find_element(By.ID, "openPaths").send_keys(str(tmp_path / "samples" / "gui_counter.stil"))
        driver.find_element(By.ID, "openSubmit").click()
        until(lambda: driver.find_element(By.ID, "runnerBtn").is_enabled(), "the open pattern")
        driver.find_element(By.ID, "runnerBtn").click()
        until(lambda: is_open("formDialog"), "the New project dialog")
        body = driver.find_element(By.ID, "formDialogBody")
        Select(body.find_element(By.NAME, "wait_preset")).select_by_value("none")
        Select(body.find_element(By.NAME, "completion")).select_by_value("hold")
        driver.find_element(By.ID, "formDialogOk").click()
        until(lambda: not is_open("formDialog"), "the project")
        driver.find_element(By.ID, "rpBuild").click()
        until(lambda: "manifest" in text("rpBuildBody").lower(), "the build", 120)
        assert "not found in this Python environment" not in text("rpExternalBody")  # step 1: the tool is installed
        bundle = next(tmp_path.rglob("runner.bundle.json")).parent
        assert str(bundle) in driver.find_element(By.TAG_NAME, "body").text  # the folder step 5 needs is on the page
        runner = driver.current_window_handle

        # steps 2-3: its own program and page, the same settings folder
        la_url, la_folder = start([*la, "--demo", "--no-device-io", "--no-browser",
                                   "--config-dir", "./settings-check", "--results-dir", "./la-runs-check"], environ, tmp_path, running)
        assert la_folder == view_folder
        driver.switch_to.new_window("tab")
        driver.get(la_url)
        until(lambda: "simulated" in text("bindingsBody"), "the analyzers")
        manual_shot(driver, "analyzer-demo.png", "#bindingsBody")

        # steps 5-6: open the same bundle, plan, write the config
        driver.find_element(By.ID, "bundlePath").send_keys(str(bundle))
        driver.find_element(By.ID, "bundleOpen").click()
        until(lambda: len(text("bundleBody")) > 40, "the bundle")
        Select(driver.find_element(By.ID, "planAnalyzer")).select_by_value("simulated")
        driver.find_element(By.ID, "planForm").find_element(By.NAME, "address").send_keys("50")
        driver.find_element(By.ID, "planRun").click()
        until(lambda: "PLANNED" in text("planBody"), "the plan")
        assert "ch0(A0)=" in text("planBody") and "ch1(A0)=" not in text("planBody")
        manual_shot(driver, "plan-simulated.png", "#cardPlan")
        Select(driver.find_element(By.ID, "runMode")).select_by_value("simulated")
        config = tmp_path / "accessory" / "external.json"
        driver.find_element(By.ID, "runFolder").send_keys(str(config.parent))
        driver.find_element(By.ID, "runWrite").click()
        until(lambda: "external.json" in text("runBody"), "the config")
        assert config.is_file()
        assert "ch0(A0)=" in text("runBody") and "ch1(A0)=" not in text("runBody")

        # step 7: Check, tick, Run in the Runner
        driver.switch_to.window(runner)

        def typed():
            box = css("#rpExternalBody input[name=external_config]")
            box.clear()
            box.send_keys(str(config), Keys.TAB)
            return css("#rpExternalBody input[name=external_config]").get_attribute("value") == str(config)

        until(typed, "the config path")
        Select(css("#rpRunBody select[name=device]")).select_by_value("simulated")
        until(lambda: driver.find_element(By.ID, "rpExternalCheck").is_enabled(), "Check")
        driver.find_element(By.ID, "rpExternalCheck").click()
        until(lambda: "checked" in text("rpExternalBody").lower(), "the check")

        def ticked():
            box = css("#rpExternalBody input[name=external_use]")
            if not box.is_selected():
                box.click()
            return css("#rpExternalBody input[name=external_use]").is_selected()

        until(ticked, "use with the next run")
        until(lambda: driver.find_element(By.ID, "rpRun").is_enabled(), "Run…")
        driver.find_element(By.ID, "rpRun").click()
        until(lambda: is_open("confirmRunDialog"), "the confirmation")
        for box in driver.find_elements(By.CSS_SELECTOR, "#confirmRunAcks input[type=checkbox]"):
            box.click()
        driver.find_element(By.ID, "confirmRunStart").click()
        until(lambda: "Overall" in text("rpRunBody"), "the result", 120)
        assert "COMPLETED" in text("rpRunBody") and "external accessory" in text("rpRunBody").lower()
        run = next(tmp_path.rglob("accessory/session.json")).parent.parent
        capture = read_json(run / "accessory" / "capture" / "capture.json")
        assert capture["mode"] == "simulated"
        assert "ch1(A0)=" in capture["plan"]["trigger"]["condition"]  # historical saved field
        assert read_json(run / "accessory" / "result.json")["mode"] == "simulated"

        # step 8: open the run's folder on the analyzer's page
        driver.switch_to.new_window("tab")
        driver.get(la_url)
        until(lambda: "simulated" in text("bindingsBody"), "the analyzers")
        driver.find_element(By.ID, "capturePath").send_keys(str(run))
        driver.find_element(By.ID, "captureOpen").click()
        until(lambda: driver.find_elements(By.ID, "waveCanvas"), "the waveform")
        assert "COMPLETED" in text("captureBody")
        manual_shot(driver, "capture-simulated.png", "#captureBody")
    finally:
        for process in running:
            process.terminate()
            process.wait(10)
        while len(driver.window_handles) > 1:
            driver.switch_to.window(driver.window_handles[-1])
            driver.close()
        driver.switch_to.window(driver.window_handles[0])
