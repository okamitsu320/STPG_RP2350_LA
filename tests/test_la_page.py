"""This tool's own page server: request guards, the demo flow and a core run with the written config (host only).

The end-to-end case is the operator's path: plan on this page, write the accessory config, run the bundle with the
STPG core (here its SDK with the simulated STPG; the Runner GUI calls the same function) and open the run folder
here. The accessory is the real child process (``python -I -m stpg_capture.accessory``).
"""

from __future__ import annotations

import http.client
import json
from pathlib import Path
import threading

import pytest

from stpg_capture.ui.server import LaServer, ROUTES
from stpg_capture.ui.service import LaService
from stpg_runner import build_project, init_project, load_external_accessory, run_with_external_accessory
from stpg_runner.artifacts import read_json
from stpgview.runner.simulated import SIMULATED_DEVICE, SimulatedStpg, simulated_ports

from la_support import forbid_hardware
from test_stpg_capture import analyzer_config, counter_stil

pytestmark = pytest.mark.stpg_host_only


@pytest.fixture(autouse=True)
def _no_hardware(monkeypatch):
    forbid_hardware(monkeypatch)


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    root = tmp_path_factory.mktemp("page-counter")
    (root / "p.stil").write_text(counter_stil(1600))
    return build_project(init_project(root / "p.stil", root / "project", wait_preset="none", completion="hold"))


@pytest.fixture
def page(tmp_path):
    service = LaService(config_dir=tmp_path / "cfg", results_dir=tmp_path / "res", device_io=False, demo=True)
    server = LaServer(("127.0.0.1", 0), service, "tok")
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()
    server.server_close()


def call(server, method, path, body=None, headers=None):
    port = server.server_address[1]
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=120)
    sent = {"Host": f"127.0.0.1:{port}", "X-LA-Token": "tok", "Content-Type": "application/json", **(headers or {})}
    connection.request(method, path, json.dumps(body) if body is not None else None,
                       {k: v for k, v in sent.items() if v is not None})
    response = connection.getresponse()
    data = response.read()
    if response.getheader("Content-Type", "").startswith("application/json"):
        return response.status, json.loads(data)
    return response.status, data


def test_the_page_is_served_and_every_api_call_is_guarded(page):
    status, html = call(page, "GET", "/")
    assert status == 200 and b"STPG logic analyzer" in html and b"/static/la.js" in html
    assert call(page, "GET", "/static/la.js")[0] == 200 and call(page, "GET", "/static/../server.py")[0] == 404
    assert call(page, "GET", "/api/state", headers={"X-LA-Token": "wrong"})[0] == 401
    assert call(page, "GET", "/api/state", headers={"X-LA-Token": None})[0] == 401
    assert call(page, "GET", "/api/state?token=tok", headers={"X-LA-Token": None})[0] == 200  # reading only
    assert call(page, "POST", "/api/ls", {}, headers={"X-LA-Token": None, "Origin": None})[0] == 401
    assert call(page, "GET", "/api/state", headers={"Host": "attacker.example:80"})[0] == 403
    assert call(page, "POST", "/api/plan", {}, headers={"Origin": "http://attacker.example"})[0] == 403
    assert call(page, "POST", "/api/plan", {}, headers={"Sec-Fetch-Site": "cross-site"})[0] == 403
    assert call(page, "GET", "/api/plan")[0] == 405 and call(page, "POST", "/api/state", {})[0] == 405
    assert call(page, "POST", "/api/run", {})[0] == 404  # this tool never runs the STPG
    assert not {"run", "stpg_run", "upload"} & set(ROUTES)
    status, state = call(page, "GET", "/api/state")
    assert status == 200 and state["demo"] is True and state["device_io"] is False
    # its own origin and token: the STPG Runner's header or token opens nothing here
    assert call(page, "GET", "/api/state", headers={"X-LA-Token": None, "X-STPG-Token": "tok"})[0] == 401
    from stpgview.server import ViewerServer

    static = Path(__import__("stpgview").__file__).parent / "static"
    core = "\n".join(f.read_text(encoding="utf-8") for f in static.iterdir() if f.suffix in (".js", ".html"))
    assert "X-LA-Token" not in core and "/static/la.js" not in core  # it may name the command, never load the page
    assert ViewerServer is not LaServer


def test_bindings_are_written_where_the_stpg_runner_lists_them(page, tmp_path):
    config = analyzer_config(marker_gpio=18).to_dict()
    assert call(page, "POST", "/api/analyzer/save", {"name": "bench-la", "config": config})[0] == 200
    status, error = call(page, "POST", "/api/analyzer/save", {"name": "bench-la", "config": config})
    assert status == 400 and "replace" in error["error"]
    assert call(page, "POST", "/api/analyzer/save", {"name": "simulated", "config": config})[0] == 400
    assert call(page, "POST", "/api/analyzer/save", {"name": "../x", "config": config})[0] == 400
    saved = tmp_path / "cfg" / "analyzers" / "bench-la.json"
    assert read_json(saved) == analyzer_config(marker_gpio=18).to_dict()
    from stpgview.runner.store import BindingStore  # the STPG Runner's own read-only view of the same folder

    listed = BindingStore(tmp_path / "cfg").list("analyzers")
    assert [(i["name"], i["config"]["serial_number"]) for i in listed] == [("bench-la", config["serial_number"])]
    assert set(listed[0]["config"]) == {"kind", "schema_version", "port", "vid", "pid", "serial_number"}
    status, rows = call(page, "GET", "/api/analyzers")
    assert [i["name"] for i in rows["items"]] == ["simulated", "bench-la"] and rows["items"][1]["wiring_rows"]
    status, error = call(page, "POST", "/api/analyzer/identify", {"name": "bench-la"})
    assert status == 400 and "--no-device-io" in error["error"]
    assert call(page, "POST", "/api/analyzer/delete", {"name": "bench-la"})[0] == 200 and not saved.exists()


def test_plan_on_the_page_run_with_the_core_and_open_the_run_here(page, built, tmp_path):
    status, bundle = call(page, "POST", "/api/bundle", {"bundle": str(built.bundle_dir)})
    assert status == 200 and bundle["manifest_sha256"] == built.manifest_sha256 and bundle["marker"] is None
    status, plan = call(page, "POST", "/api/plan", {
        "bundle": str(built.bundle_dir), "manifest_sha256": built.manifest_sha256, "analyzer": "simulated",
        "request": {"address": {"vector": 300}, "before_vectors": 8, "after_vectors": 24, "strategy": "auto"}})
    assert status == 200 and plan["plan"]["status"] == "PLANNED", plan
    status, error = call(page, "POST", "/api/config", {"plan_id": plan["plan_id"], "mode": "device"})
    assert status == 400 and "simulated mode" in error["error"]
    status, written = call(page, "POST", "/api/config", {"plan_id": plan["plan_id"], "mode": "simulated",
                                                        "directory": str(tmp_path / "accessory")})
    assert status == 200 and written["mode"] == "simulated" and Path(written["config"]).is_file()
    # the operator now selects external.json in the Runner's External accessory card; the Runner calls this
    result = run_with_external_accessory(built.bundle_dir, manifest_sha256=built.manifest_sha256,
                                         device=SIMULATED_DEVICE, spec=load_external_accessory(Path(written["config"])),
                                         output_dir=tmp_path / "run", confirm_run=True,
                                         stream_factory=lambda _: SimulatedStpg(), port_provider=simulated_ports)
    assert result["external"]["outcome"] == "COMPLETED" and result["external"]["adopted"] is True
    status, view = call(page, "POST", "/api/capture", {"path": str(tmp_path / "run")})
    assert status == 200 and view["result"]["outcome"] == "CAPTURED" and view["result"]["mode"] == "simulated"
    assert view["run"]["outcome"] == "COMPLETED" and view["result"]["timing"]["adopted"] and view["waveform"]
    status, wave = call(page, "POST", "/api/waveform", {"capture_id": view["capture_id"]})
    assert status == 200 and wave["aligned"] and wave["end_ps"] > wave["start_ps"]
    assert {lane["signal"] for lane in wave["lanes"]} >= {"A0", "CLK"}
    assert all(lane["expected"] and lane["captured"] for lane in wave["lanes"])
    status, mock = call(page, "POST", "/api/mock", {"plan_id": plan["plan_id"], "offset_ps": 0})
    assert status == 200 and mock["result"]["outcome"] == "MOCK" and mock["run"] is None
    status, recent = call(page, "GET", "/api/results")
    assert [i["kind"] for i in recent["items"]] == ["capture"]
    status, error = call(page, "POST", "/api/capture", {"path": str(tmp_path)})
    assert status == 400 and "no capture.json" in error["error"]


def test_port_listing_is_metadata_on_request_and_off_without_device_io(page, tmp_path, monkeypatch):
    from serial.tools import list_ports

    calls = []
    port = type("Port", (), {"device": "COM9", "vid": 0x2E8A, "pid": 0x000A, "serial_number": "PUBLIC-1",
                             "description": "synthetic"})()
    monkeypatch.setattr(list_ports, "comports", lambda include_links=False: calls.append(1) or [port])
    status, listing = call(page, "POST", "/api/ports", {})
    assert status == 200 and listing["real_scan_performed"] is False and calls == []  # --no-device-io in this page
    assert [p["device"] for p in listing["ports"]] == ["SIMULATED-LA"]
    assert call(page, "GET", "/api/ports")[0] == 405
    page.service.device_io = True
    status, listing = call(page, "POST", "/api/ports", {})
    assert status == 200 and listing["real_scan_performed"] is True and calls == [1]
    assert [p["device"] for p in listing["ports"]] == ["SIMULATED-LA", "COM9"]
    assert listing["ports"][1]["serial_number"] == "PUBLIC-1"


def test_listing_for_the_path_picker(page, tmp_path):
    (tmp_path / "folder").mkdir()
    (tmp_path / "file.json").write_text("{}")
    (tmp_path / ".hidden").write_text("")
    status, listing = call(page, "POST", "/api/ls", {"path": str(tmp_path)})
    names = [e["name"] for e in listing["entries"]]
    assert status == 200 and "folder" in names and "file.json" in names and ".hidden" not in names
    assert names.index("folder") < names.index("file.json")
