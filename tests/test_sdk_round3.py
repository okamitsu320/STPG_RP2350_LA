"""Round-3 handoff: real isolated companion across a later target segment.

Public synthetic inputs only. This test belongs to the separate companion, not
to the MIT core test package. Physical serial/USB I/O remains forbidden.
"""
import json

import pytest

from stpg_capture.external_config import write_external_config
from stpg_capture.planner import plan_capture
from stpg_runner import accessory_exit, load_external_accessory, run_with_external_accessory
from stpgview.runner.simulated import SIMULATED_DEVICE, SimulatedStpg, simulated_ports
from la_support import forbid_hardware
from test_stpg_capture import analyzer_config, request
from test_stpg_capture_failpaths import segmented

pytestmark = pytest.mark.stpg_host_only


@pytest.fixture(autouse=True)
def no_hardware(monkeypatch):
    forbid_hardware(monkeypatch)


def test_real_isolated_companion_completes_before_the_next_segment(segmented, tmp_path):
    built, timeline = segmented
    assert len(timeline.segments) == 2
    analyzer = analyzer_config()
    plan = plan_capture(timeline, request(300), analyzer)
    assert plan["bundle"]["segment"] == 0
    written = write_external_config(
        tmp_path / "config", bundle_dir=built.bundle_dir,
        manifest_sha256=built.manifest_sha256, analyzer_binding=analyzer,
        capture_plan=plan, mode="simulated", device=SIMULATED_DEVICE,
    )
    spec = load_external_accessory(written["config"])
    assert spec.command[1:] == ("-I", "-m", "stpg_capture.accessory")
    stpg = SimulatedStpg()
    result = run_with_external_accessory(
        built.bundle_dir, manifest_sha256=built.manifest_sha256,
        device=SIMULATED_DEVICE, spec=spec, output_dir=tmp_path / "run", confirm_run=True,
        stream_factory=lambda _: stpg, port_provider=simulated_ports,
    )
    run, external = result["run"], result["external"]
    assert run["outcome"] == "COMPLETED" and run["completed_segments"] == 2
    assert len(stpg.run_args) == 2
    assert external["outcome"] == "COMPLETED" and external["adopted"] is True
    assert accessory_exit(result) == 0
    assert [item["method"] for item in external["requests"]] == ["open", "before_run", "after_terminal"]
    assert external["spawn_attempts"] == external["stdin_close_attempts"] == 1
    assert external["terminate_attempts"] == external["kill_attempts"] == 0
    assert external["returncode"] == 0 and external["output_drain_complete"] is True
    assert external["pipe_eof_seen"] == ["stderr", "stdout"]
    root = tmp_path / "run" / "accessory"
    assert json.loads((root / "session.json").read_text()) == external
    capture = json.loads((root / "capture" / "capture.json").read_text())
    assert capture["mode"] == "simulated" and capture["outcome"] == "CAPTURED"
    assert capture["timing"]["adopted"] is True
