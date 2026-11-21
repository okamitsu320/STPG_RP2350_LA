"""This tool's page in headless Firefox (host only; demo analyzer, no port opened).

Plans a capture, writes the accessory config, makes a mock capture and checks the result view and its waveform,
and adds an analyzer binding through the form. Skipped (not passed) when selenium, Firefox or geckodriver is
missing; ``STPG_BROWSER_FIREFOX`` and ``STPG_BROWSER_GECKODRIVER`` select the binaries.
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import threading
import time

import pytest

webdriver = pytest.importorskip("selenium.webdriver")
from selenium.common.exceptions import StaleElementReferenceException  # noqa: E402
from selenium.webdriver.common.by import By  # noqa: E402
from selenium.webdriver.common.keys import Keys  # noqa: E402
from selenium.webdriver.firefox.options import Options  # noqa: E402
from selenium.webdriver.firefox.service import Service  # noqa: E402
from selenium.webdriver.support.ui import Select  # noqa: E402

from stpg_capture.mock import PICO2_TURBO_LIMITS  # noqa: E402
from stpg_capture.ui.server import LaServer  # noqa: E402
from stpg_capture.ui.service import LaService  # noqa: E402
from stpg_runner import build_project, init_project  # noqa: E402
from stpg_runner.artifacts import read_json  # noqa: E402

from la_support import forbid_hardware  # noqa: E402
from test_stpg_capture import analyzer_config, counter_stil  # noqa: E402

pytestmark = pytest.mark.stpg_host_only
SNAP_FIREFOX = "/snap/firefox/current/usr/lib/firefox/firefox"


@pytest.fixture(scope="module")
def browser():
    firefox = os.environ.get("STPG_BROWSER_FIREFOX") or (SNAP_FIREFOX if Path(SNAP_FIREFOX).is_file() else None) \
        or shutil.which("firefox")
    driver_path = os.environ.get("STPG_BROWSER_GECKODRIVER") or shutil.which("geckodriver")
    if not (firefox and driver_path):
        pytest.skip("Firefox and geckodriver are required for the browser checks")
    options = Options()
    options.add_argument("-headless")
    options.binary_location = firefox
    driver = webdriver.Firefox(options=options, service=Service(driver_path))
    driver.set_window_size(1400, 1000)
    yield driver
    driver.quit()


@pytest.fixture(autouse=True)
def _no_hardware(monkeypatch):
    forbid_hardware(monkeypatch)


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    root = tmp_path_factory.mktemp("la-browser")
    (root / "p.stil").write_text(counter_stil(1600))
    return build_project(init_project(root / "p.stil", root / "project", wait_preset="none", completion="hold"))


@pytest.fixture
def page(browser, tmp_path):
    service = LaService(config_dir=tmp_path / "cfg", results_dir=tmp_path / "res", device_io=False, demo=True)
    server = LaServer(("127.0.0.1", 0), service, "tok")
    threading.Thread(target=server.serve_forever, daemon=True).start()
    browser.get(f"http://127.0.0.1:{server.server_address[1]}/?token=tok")
    browser.execute_script("window.__errors = []; window.addEventListener('error', (e) => window.__errors.push(String(e.message)));"
                           "window.addEventListener('unhandledrejection', (e) => window.__errors.push(String(e.reason)));")
    until(browser, lambda: "simulated" in browser.find_element(By.ID, "bindingsBody").text, "bindings")
    yield browser, service
    server.shutdown()
    server.server_close()


def until(driver, predicate, what, timeout=60.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            value = predicate()
        except StaleElementReferenceException:
            value = None
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}; status: {driver.find_element(By.ID, 'status').text!r}; "
                         f"script errors: {driver.execute_script('return window.__errors')}")


def fill(driver, element_id, value):
    box = driver.find_element(By.ID, element_id)
    box.clear()
    box.send_keys(str(value))


def test_plan_config_mock_and_waveform_on_the_page(page, built, tmp_path):
    driver, service = page
    assert "separate program from the STPG Runner" in driver.find_element(By.ID, "banner").text
    fill(driver, "bundlePath", built.bundle_dir)
    driver.find_element(By.ID, "bundleOpen").click()
    until(driver, lambda: built.manifest_sha256 in driver.find_element(By.ID, "bundleBody").text, "bundle")
    assert "null" not in driver.find_element(By.ID, "bundleBody").text
    Select(driver.find_element(By.ID, "planAnalyzer")).select_by_value("simulated")
    form = driver.find_element(By.ID, "planForm")
    form.find_element(By.NAME, "address").send_keys("300")
    driver.find_element(By.ID, "planRun").click()
    until(driver, lambda: "PLANNED" in driver.find_element(By.ID, "planBody").text, "plan")
    assert driver.find_element(By.ID, "runWrite").is_enabled() and driver.find_element(By.ID, "mockRun").is_enabled()
    driver.find_element(By.ID, "runWrite").click()  # no mode chosen yet: refused, nothing written
    until(driver, lambda: "choose the mode explicitly" in driver.find_element(By.ID, "status").text, "mode refusal")
    Select(driver.find_element(By.ID, "runMode")).select_by_value("simulated")
    fill(driver, "runFolder", tmp_path / "accessory")
    driver.find_element(By.ID, "runWrite").click()
    text = until(driver, lambda: driver.find_element(By.ID, "runBody").text, "config written")
    assert "external.json" in text and "External accessory" in text
    assert "open limit" in text and "arm/complete limit" in text and "stdin EOF grace" in text
    assert read_json(tmp_path / "accessory" / "external.json")["mode"] == "simulated"
    driver.find_element(By.ID, "mockRun").click()
    until(driver, lambda: driver.find_elements(By.ID, "waveCanvas"), "waveform")
    result = driver.find_element(By.ID, "captureBody").text
    assert "MOCK" in result and "timing adopted" in result and "not a measurement" in result
    height = driver.execute_script("return document.getElementById('waveCanvas').height")
    assert height > 100
    assert driver.execute_script("return window.__errors") == []


def test_a_new_user_is_guided_and_sees_the_settings_folder_and_the_wiring(page, tmp_path):
    driver, service = page
    guide = driver.find_element(By.ID, "laGuide")
    assert guide.get_attribute("open") and "It never runs the STPG" in guide.text
    for needle in ("press Identify", "plan a capture", "external.json", "use with the next run", "open the run's folder"):
        assert needle in guide.text, needle
    folder = driver.find_element(By.ID, "settingsFolder").text
    assert str(tmp_path / "cfg") in folder and "from --config-dir" in folder
    assert "The STPG Runner must show the same folder" in folder and "Nothing is moved or copied for you." in folder
    assert not (tmp_path / "cfg").exists()
    driver.find_element(By.XPATH, "//button[normalize-space()='Wiring']").click()
    wiring = until(driver, lambda: driver.find_element(By.ID, "identifyBody").text, "wiring table")
    assert "Wiring of simulated" in wiring and "GPIO23: no analyzer input" in wiring
    assert "analyzer GPIO0 wired to analyzer GPIO1" in wiring and "Connect the STPG and analyzer grounds." in wiring
    assert driver.execute_script("return window.__errors") == []


def test_scan_ports_fills_the_form_only_when_used(page, tmp_path):
    driver, service = page
    driver.find_element(By.XPATH, "//button[normalize-space()='Add binding…']").click()
    form = until(driver, lambda: driver.find_element(By.CSS_SELECTOR, "#bindingForm form"), "form")
    assert form.find_element(By.NAME, "port").get_attribute("value") == ""
    driver.find_element(By.ID, "scanPortsButton").click()
    text = until(driver, lambda: "SIMULATED-LA" in driver.find_element(By.ID, "scanPorts").text
                 and driver.find_element(By.ID, "scanPorts").text, "port list")
    assert "No real port scan" in text and "Nothing is selected automatically" in text  # --no-device-io here
    assert form.find_element(By.NAME, "port").get_attribute("value") == ""  # listing selects nothing
    driver.find_element(By.XPATH, "//div[@id='scanPorts']//button[normalize-space()='Use']").click()
    assert form.find_element(By.NAME, "port").get_attribute("value") == "SIMULATED-LA"
    assert form.find_element(By.NAME, "serial_number").get_attribute("value") == "SIMULATED-LA-0001"
    assert driver.execute_script("return window.__errors") == []


def test_reported_limits_can_be_adopted_into_the_binding(page, tmp_path, monkeypatch):
    driver, service = page
    other = {**PICO2_TURBO_LIMITS.to_dict(), "max_frequency": 150_000_000}
    service.save_analyzer({"name": "bench-la", "config": analyzer_config().to_dict()})
    service.device_io = True  # the injected report below replaces the port access
    monkeypatch.setattr(service, "identify", lambda request: {
        "status": "REJECT", "matches_binding": False, "reported": other, "analyzer": None, "simulated": False,
        "errors": ["analyzer reports other limits than the binding expects"], "result_file": "identify.json"})
    driver.refresh()
    until(driver, lambda: "bench-la" in driver.find_element(By.ID, "bindingsBody").text, "binding")
    row = driver.find_element(By.XPATH, "//tr[.//strong[normalize-space()='bench-la']]")
    row.find_element(By.XPATH, ".//button[normalize-space()='Identify']").click()
    until(driver, lambda: driver.find_elements(By.ID, "useReported"), "use reported")[0].click()
    form = until(driver, lambda: driver.find_element(By.CSS_SELECTOR, "#bindingForm form"), "form")
    assert f"as reported: {other['id_line']}, 150000000 Hz" in form.find_element(By.NAME, "limits").text
    driver.find_element(By.XPATH, "//div[@id='bindingForm']//button[normalize-space()='Save']").click()
    until(driver, lambda: read_json(tmp_path / "cfg" / "analyzers" / "bench-la.json")["limits"] == other, "saved")


def test_a_binding_is_added_through_the_form(page, tmp_path):
    driver, service = page
    driver.find_element(By.XPATH, "//button[normalize-space()='Add binding…']").click()
    form = until(driver, lambda: driver.find_element(By.CSS_SELECTOR, "#bindingForm form"), "form")
    for name, value in (("name", "bench-la"), ("port", "COM9"), ("pid", "0x000a"), ("serial_number", "PUBLIC-LA-9")):
        form.find_element(By.NAME, name).send_keys(value)
    driver.find_element(By.XPATH, "//div[@id='bindingForm']//button[normalize-space()='Save']").click()
    until(driver, lambda: "bench-la" in driver.find_element(By.ID, "bindingsBody").text, "binding listed")
    saved = read_json(tmp_path / "cfg" / "analyzers" / "bench-la.json")
    assert (saved["port"], saved["pid"], saved["wiring"]) == ("COM9", 10, "same-gpio")
    assert driver.execute_script("return window.__errors") == []


def test_the_picker_opens_a_typed_folder_and_names_a_new_folder_without_creating_it(page, tmp_path, monkeypatch):
    driver, service = page
    home, parent = tmp_path / "home", tmp_path / "out"
    home.mkdir()
    parent.mkdir()
    (parent / "Existing").mkdir()
    monkeypatch.setenv("HOME", str(home))  # Home shows this temporary folder, never the real one
    monkeypatch.setenv("USERPROFILE", str(home))
    is_open = lambda: driver.execute_script("return document.getElementById('pickDialog').open")  # noqa: E731
    here = lambda: driver.find_element(By.ID, "pickHere").text  # noqa: E731
    driver.execute_script("document.querySelector('[data-browse=runFolder]').click()")
    until(driver, is_open, "picker")
    assert driver.find_element(By.ID, "pickNewRow").is_displayed()  # a new output folder: parent plus a name
    box = driver.find_element(By.ID, "pickPath")
    box.clear()
    box.send_keys(str(parent))
    driver.find_element(By.ID, "pickGo").click()
    until(driver, lambda: here() == str(parent.resolve()), "the typed folder")
    roots = [b.text for b in driver.find_elements(By.CSS_SELECTOR, "#pickRoots button")]
    assert roots == (["/"] if os.name != "nt" else roots) and roots
    driver.find_element(By.ID, "pickHome").click()
    until(driver, lambda: here() == str(home.resolve()), "home")
    box.clear()
    box.send_keys(str(parent), Keys.ENTER)  # Enter in the path box opens it, it does not close the dialog
    until(driver, lambda: here() == str(parent.resolve()), "the typed folder again")
    name = driver.find_element(By.ID, "pickNewName")
    name.send_keys("existing")
    driver.find_element(By.ID, "pickOk").click()
    until(driver, lambda: "already exists" in driver.find_element(By.ID, "pickError").text, "refusal")
    assert is_open()  # still open: nothing was chosen
    name.clear()
    name.send_keys("run 1", Keys.ENTER)
    until(driver, lambda: not is_open(), "closed")
    assert driver.find_element(By.ID, "runFolder").get_attribute("value") == str(parent.resolve() / "run 1")
    assert sorted(p.name for p in parent.iterdir()) == ["Existing"]  # nothing created
    # An existing folder: a missing path is reported and no other folder is opened; Cancel changes nothing.
    driver.execute_script("document.querySelector('[data-browse=capturePath]').click()")
    until(driver, is_open, "picker for an existing folder")
    assert not driver.find_element(By.ID, "pickNewRow").is_displayed()
    until(driver, lambda: here() == str(home.resolve()), "empty input starts at home")
    box.clear()
    box.send_keys(str(tmp_path / "missing"))
    driver.find_element(By.ID, "pickGo").click()
    until(driver, lambda: "is not a folder" in driver.find_element(By.ID, "pickError").text, "missing folder")
    assert here() == str(home.resolve())
    driver.find_element(By.XPATH, "//dialog[@id='pickDialog']//button[@value='cancel']").click()
    until(driver, lambda: not is_open(), "cancelled")
    assert driver.find_element(By.ID, "capturePath").get_attribute("value") == ""
    assert driver.execute_script("return window.__errors") == []



def test_a_new_binding_walks_through_the_steps_and_chooses_nothing_for_the_user(page):
    driver, service = page
    driver.find_element(By.XPATH, "//button[normalize-space()='Add binding…']").click()
    form = until(driver, lambda: driver.find_element(By.CSS_SELECTOR, "#bindingForm form"), "form")
    steps = driver.find_element(By.ID, "bindingSteps").text
    assert "press Use on the analyzer's own row" in steps and "Nothing is selected for you" in steps
    assert "Save. Then press Identify" in steps
    limits = [o.text for o in form.find_elements(By.CSS_SELECTOR, "select[name=limits] option")]
    assert limits and all("MHz" in text and "blast" in text for text in limits), limits
    label = form.find_element(By.XPATH, ".//label[select[@name='limits']]").text
    assert "not told by its USB identity" in label
    advanced = form.find_element(By.CSS_SELECTOR, "details.advanced")
    assert advanced.get_attribute("open") is None  # optional, closed: no marker pin is chosen for the user
    assert form.find_element(By.NAME, "marker_gpio").get_attribute("value") == ""
    driver.find_element(By.ID, "scanPortsButton").click()
    until(driver, lambda: "SIMULATED-LA" in driver.find_element(By.ID, "scanPorts").text, "port list")
    assert form.find_element(By.NAME, "port").get_attribute("value") == ""  # a listing fills in nothing
    assert driver.execute_script("return window.__errors") == []
