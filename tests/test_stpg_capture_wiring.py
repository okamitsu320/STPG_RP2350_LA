"""Independent GPIO/channel mapping assertions, offline capture plan/marker checks."""
from copy import deepcopy
from pathlib import Path

import pytest

from stpg_capture.wiring import PRESETS, STPG_DUT_GPIO, Wiring
from stpg_capture.la_protocol import CHANNEL_GPIO
from stpg_capture.config import AnalyzerConfig
from stpg_capture.marker import marker_candidates, derive_marker_project, check_marker_bundle
from stpg_capture.planner import plan_capture, validate_plan, PlanError
from stpg_capture.config import CaptureRequestSpec
from stpg_capture.timeline import load_bundle_timeline
from stpg_runner import build_project
from test_stpg_capture_cli import built

pytestmark = pytest.mark.stpg_host_only
NEW_BOARD = "stpg20-23-to-la19-22"


def test_all_twenty_four_new_board_connections_are_exact_and_current_default_is_unchanged():
    current, new = Wiring.same_gpio(), Wiring.preset(NEW_BOARD)
    assert current.to_config() == "same-gpio" and len(current.rows()) == 24
    assert sum(row["analyzer_channel"] is not None for row in current.rows()) == 23
    assert current.channel(23) is None
    expected = {**{gpio: gpio for gpio in range(2, 19)},
                20: 19, 21: 20, 22: 21, 23: 22, 26: 26, 27: 27, 28: 28}
    assert len(expected) == len(new.rows()) == 24
    assert {row["stpg_gpio"]: row["analyzer_gpio"] for row in new.rows()} == expected
    assert len({row["analyzer_channel"] for row in new.rows()}) == 24
    assert {row["stpg_gpio"]: row["stpg_bit"] for row in new.rows()} == {
        gpio: bit for bit, gpio in enumerate(STPG_DUT_GPIO)}


@pytest.mark.parametrize("name", list(PRESETS))
def test_named_and_both_custom_forms_roundtrip(name):
    wiring = Wiring.preset(name)
    channels = {str(gpio): ch + 1 for gpio, ch in wiring.gpio_to_channel.items()}
    gpios = {"analyzer_gpio": {str(gpio): CHANNEL_GPIO[ch] for gpio, ch in wiring.gpio_to_channel.items()}}
    for value in (name, channels, gpios):
        result = Wiring.from_config(value)
        assert result.gpio_to_channel == wiring.gpio_to_channel
        assert Wiring.from_config(result.to_config()).rows() == wiring.rows()


@pytest.mark.parametrize("value", [
    {"20": 18, "020": 19}, {"analyzer_gpio": {"20": 19, "020": 20}},
    {"analyzer_gpio": {"２０": 19}}, {"20 ": 1}, {"+20": 1},
    {"19": 1}, {"20": True}, {"analyzer_gpio": {"20": 23}},
    {"analyzer_gpio": {"20": 19, "21": 19}}, {"analyzer_gpio": {}},
    {"20": 1, "21": 1}, "unknown-board",
])
def test_ambiguous_invalid_or_unknown_wiring_never_defaults(value):
    with pytest.raises(ValueError):
        Wiring.from_config(value)


def test_published_presets_and_labels_do_not_silently_change_meaning():
    with pytest.raises(TypeError):
        PRESETS["same-gpio"][20] = 19
    wiring = Wiring.same_gpio()
    wiring.gpio_to_channel[20] = wiring.gpio_to_channel[21]
    with pytest.raises(ValueError):
        wiring.to_config()
    with pytest.raises(ValueError):
        wiring.rows()


def test_new_board_binding_rejects_old_capture_plan_and_marker_can_use_a_sacrificed_pin(built, tmp_path):
    project, bundle, timeline, old, plan = built
    new = AnalyzerConfig.from_dict({**old.to_dict(), "wiring": NEW_BOARD})
    with pytest.raises(PlanError):
        validate_plan(timeline, plan, new)
    assert new.wiring.channel(23) == 20  # channel 21 = analyzer physical GPIO22
    assert 23 in {item["gpio"] for item in marker_candidates(timeline, new)}
    assert 23 not in {item["gpio"] for item in marker_candidates(timeline, old)}
    source_before = project.read_bytes()
    derived = derive_marker_project(project, bundle_dir=bundle.bundle_dir,
                                    manifest_sha256=bundle.manifest_sha256, vector=25,
                                    analyzer=new, directory=tmp_path / "marker", marker_gpio=23)
    built_marker = build_project(Path(derived["project_path"]))
    marker = load_bundle_timeline(built_marker.bundle_dir, manifest_sha256=built_marker.manifest_sha256)
    checked = check_marker_bundle(marker, derived["derivation"], analyzer=new)
    assert checked["marker_gpio"] == 23 and checked["verification"]["status"] == "EQUIVALENT_TIMING"
    assert project.read_bytes() == source_before and new.marker_gpio is None
    with pytest.raises(ValueError):
        check_marker_bundle(marker, derived["derivation"], analyzer=old)
    with pytest.raises(ValueError):
        derive_marker_project(project, bundle_dir=bundle.bundle_dir, manifest_sha256=bundle.manifest_sha256,
                              vector=25, analyzer=new, directory=tmp_path / "used", marker_gpio=2)
    assert not (tmp_path / "used").exists()
