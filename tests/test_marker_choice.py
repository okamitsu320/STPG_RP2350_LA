"""Marker pin chosen per derivation, through the package, the CLI and this tool's page (host only, synthetic inputs).

The marker can use any STPG GPIO that the analyzer binding's wiring connects and the pattern leaves free; the
binding's ``marker_gpio`` is only the default. Nothing here opens a port.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from stpg_capture import cli as capture_cli
from stpg_capture.marker import MarkerError, check_marker_bundle, derive_marker_project, marker_candidates
from stpg_capture.timeline import load_bundle_timeline
from stpg_capture.ui.service import LaService
from stpg_capture.wiring import STPG_DUT_GPIO
from stpg_runner import build_project
from stpg_runner.artifacts import RunnerError, read_json, write_json

from la_support import forbid_hardware
from test_stpg_capture import _build, analyzer_config, counter_stil, request

pytestmark = pytest.mark.stpg_host_only
NEW_BOARD = "stpg20-23-to-la19-22"


@pytest.fixture(autouse=True)
def _no_hardware(monkeypatch):
    forbid_hardware(monkeypatch)


@pytest.fixture(scope="module")
def counter(tmp_path_factory):
    built = _build(tmp_path_factory, "marker-choice-counter", counter_stil(1600))  # signals on GPIO2..13
    return built, load_bundle_timeline(built.bundle_dir, manifest_sha256=built.manifest_sha256)


def _project(built) -> Path:
    return Path(built.bundle_dir).parent.parent / "project.stpg-project.json"


# ---- shared capture package --------------------------------------------------------------------------------

def test_candidates_are_the_wired_pins_no_signal_uses(counter):
    _, timeline = counter
    same = marker_candidates(timeline, analyzer_config())
    assert [c["gpio"] for c in same] == [14, 15, 16, 17, 18, 20, 21, 22, 26, 27, 28]  # GPIO23 is not wired
    new = marker_candidates(timeline, analyzer_config(wiring=NEW_BOARD))
    assert [c["gpio"] for c in new] == [14, 15, 16, 17, 18, 20, 21, 22, 23, 26, 27, 28]
    by_gpio = {c["gpio"]: c for c in new}
    assert by_gpio[23] == {"gpio": 23, "bit": 20, "analyzer_channel": 21, "analyzer_gpio": 22,
                           "switches_to_24_pins": True}
    assert by_gpio[20]["analyzer_gpio"] == 19 and not by_gpio[15]["switches_to_24_pins"]
    assert all(c["bit"] == STPG_DUT_GPIO.index(c["gpio"]) for c in new)


def test_any_free_wired_pin_can_carry_the_marker(counter, tmp_path):
    built, timeline = counter
    board = analyzer_config(wiring=NEW_BOARD, marker_gpio=18)  # default 18, this derivation picks GPIO23
    derived = derive_marker_project(_project(built), bundle_dir=built.bundle_dir, manifest_sha256=built.manifest_sha256,
                                    vector=900, analyzer=board, directory=tmp_path / "gpio23", marker_gpio=23)
    assert derived["derivation"]["marker"] == {"signal": "STPG_MARKER", "gpio": 23, "bit": 20, "analyzer_channel": 21}
    marker_bundle = build_project(Path(derived["project_path"]))
    marker_timeline = load_bundle_timeline(marker_bundle.bundle_dir, manifest_sha256=marker_bundle.manifest_sha256)
    checked = check_marker_bundle(marker_timeline, derived["derivation"], analyzer=board)
    assert checked["marker_gpio"] == 23 and checked["verification"]["status"] == "EQUIVALENT_TIMING"
    # another binding default is fine; a wiring that does not reach GPIO23 is not
    assert check_marker_bundle(marker_timeline, derived["derivation"],
                               analyzer=analyzer_config(wiring=NEW_BOARD))["marker_gpio"] == 23
    with pytest.raises(MarkerError, match="connects to no analyzer input"):
        check_marker_bundle(marker_timeline, derived["derivation"], analyzer=analyzer_config(marker_gpio=18))
    from stpg_capture.planner import plan_capture

    plan = plan_capture(marker_timeline, request(900), board)
    assert plan["trigger"]["strategy"] == "marker"
    assert [ch["gpio"] for ch in plan["channels"] if ch["signal"] == "STPG_MARKER"] == [23]


@pytest.mark.parametrize(("kwargs", "message"), [
    ({"analyzer": analyzer_config(), "marker_gpio": None}, "marker_gpio"),
    ({"analyzer": analyzer_config(), "marker_gpio": 5}, "used by signal"),
    ({"analyzer": analyzer_config(), "marker_gpio": 23}, "not wired"),
    ({"analyzer": analyzer_config(), "marker_gpio": 19}, "STPG DUT GPIO"),
    ({"analyzer": analyzer_config(), "marker_gpio": True}, "STPG DUT GPIO"),
    ({"analyzer": analyzer_config(marker_gpio=13), "marker_gpio": None}, "used by signal"),
])
def test_marker_pin_rejections_create_nothing(counter, tmp_path, kwargs, message):
    built, _ = counter
    with pytest.raises(MarkerError, match=message):
        derive_marker_project(_project(built), bundle_dir=built.bundle_dir, manifest_sha256=built.manifest_sha256,
                              vector=900, directory=tmp_path / "derived", **kwargs)
    assert not (tmp_path / "derived").exists()


def test_cli_marker_takes_a_pin_per_derivation(counter, tmp_path, capsys):
    built, _ = counter
    binding = tmp_path / "la.json"
    write_json(binding, analyzer_config(marker_gpio=18).to_dict())
    assert capture_cli.main(["marker", str(built.bundle_dir), "--manifest-sha256", built.manifest_sha256,
                             "--project", str(_project(built)), "--analyzer", str(binding), "--vector", "900",
                             "--directory", str(tmp_path / "derived"), "--marker-gpio", "20"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "EQUIVALENT_TIMING" and report["device_io_performed"] is False
    assert read_json(Path(report["derivation_file"]))["marker"]["gpio"] == 20


# ---- this tool's page -----------------------------------------------------------------------------------------

@pytest.fixture
def svc(tmp_path):
    service = LaService(config_dir=tmp_path / "cfg", results_dir=tmp_path / "res", device_io=False)
    service.save_analyzer({"name": "la", "config": analyzer_config().to_dict()})  # no default marker pin
    service.save_analyzer({"name": "la18", "config": analyzer_config(marker_gpio=18).to_dict()})
    return service


def _plan(svc, built, analyzer, vector=900):
    return svc.plan({"bundle": str(built.bundle_dir), "manifest_sha256": built.manifest_sha256, "analyzer": analyzer,
                     "request": {"address": {"vector": vector}, "before_vectors": 8, "after_vectors": 24,
                                 "strategy": "auto"}})


def test_the_page_offers_every_free_pin_and_needs_a_spare_declaration(svc, counter, tmp_path):
    built, _ = counter
    plain = _plan(svc, built, "la")
    assert plain["has_marker_signal"] is False and plain["marker_default"] is None
    assert [c["gpio"] for c in plain["marker_candidates"]] == [14, 15, 16, 17, 18, 20, 21, 22, 26, 27, 28]
    assert _plan(svc, built, "la18")["marker_default"] == 18
    base = {"project": str(_project(built)), "bundle": str(built.bundle_dir), "manifest_sha256": built.manifest_sha256,
            "vector": 900, "analyzer": "la"}
    with pytest.raises(RunnerError, match="spare on this bench"):
        svc.derive_marker({**base, "marker_gpio": 21})
    with pytest.raises(RunnerError, match="STPG DUT GPIO"):
        svc.derive_marker({**base, "marker_gpio": "21", "spare_confirmed": True})
    with pytest.raises(MarkerError, match="used by signal"):
        svc.derive_marker({**base, "marker_gpio": 5, "spare_confirmed": True, "directory": str(tmp_path / "used")})
    assert not (tmp_path / "used").exists()
    derived = svc.derive_marker({**base, "marker_gpio": 21, "spare_confirmed": True,
                                 "directory": str(tmp_path / "gpio21")})
    assert derived["verification"]["status"] == "EQUIVALENT_TIMING" and derived["marker_gpio"] == 21
    record = read_json(svc.config_dir / "derivations" / f"{derived['manifest_sha256']}.json")
    assert record["marker"]["gpio"] == 21  # found by the derived bundle's hash, also after a move
    # the binding's default pin needs no extra declaration (it was declared in the binding)
    assert svc.derive_marker({**base, "analyzer": "la18", "directory": str(tmp_path / "gpio18")})["marker_gpio"] == 18
    opened = svc.open_bundle({"bundle": derived["bundle_dir"], "manifest_sha256": derived["manifest_sha256"]})
    assert opened["marker"] == {"record": True, "status": "EQUIVALENT_TIMING", "gpio": 21}
    derived_bundle = type("B", (), {"bundle_dir": derived["bundle_dir"], "manifest_sha256": derived["manifest_sha256"]})
    marker_plan = _plan(svc, derived_bundle, "la")
    assert marker_plan["has_marker_signal"] is True and marker_plan["marker_candidates"] == []
    assert marker_plan["plan"]["trigger"]["strategy"] == "marker"
    # the accessory config carries the derivation; the STPG Runner names the marker pin as a pin with a role
    config = svc.write_config({"plan_id": marker_plan["plan_id"], "mode": "device",
                               "directory": str(tmp_path / "accessory")})
    assert read_json(Path(config["config"]).parent / "plan.json")["marker_derivation"] == record
    from stpgview.runner.service import role_pins

    assert [r for r in role_pins(Path(derived["bundle_dir"])) if r["signal"] == "STPG_MARKER"][0]["role"] == "marker"
