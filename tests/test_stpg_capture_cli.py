"""Capture CLI/SDK parity without a GUI or physical devices: the core run with the accessory, and offline commands."""
import json

import pytest

from stpg import constants as c
from stpg_runner import build_project, init_project, CancellationToken, load_external_accessory
from stpg_runner import execution, external_accessory
from stpg_runner.accessories import accessory_exit
from stpg_runner.artifacts import RunnerError, read_json, write_json
from stpg_runner.progress import BuildCancelled
from stpg_capture import cli, la_device
from stpg_capture.config import CaptureRequestSpec
from stpg_capture.planner import plan_capture
from stpg_capture.timeline import load_bundle_timeline
from stpg_capture.mock import ModelAnalyzer, synthesize
from test_stpg_capture_boundaries import config as analyzer_config
from stpgview.runner.simulated import SimulatedStpg
from accessory_harness import ThreadProcess, ports_of, write_config
from la_support import CONFIG, DEVICE, Peer as GpioPeer, controller_ports, ports

pytestmark = pytest.mark.stpg_host_only


@pytest.fixture(autouse=True)
def no_hardware(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("capture CLI test attempted physical contact")
    for module in (execution, la_device):
        monkeypatch.setattr(module, "enumerate_ports", forbidden)
    monkeypatch.setattr(execution, "open_serial", forbidden)
    monkeypatch.setattr(la_device, "open_analyzer_serial", forbidden)


def counter_stil(count=64):
    names = [f"A{i}" for i in range(8)] + ["CLK"]
    lines = ['Header { Title "public counter"; }', 'Signals {']
    lines += [f"{name} In;" for name in names]
    lines += ["}", "SignalGroups {", " ALL = '" + " + ".join(names) + "';", "}",
              "Timing {", " WaveformTable NORMAL {", " Period '200ns';", " Waveforms {"]
    lines += [f"{name} {{ 0: 0ns Drive(0); 1: 0ns Drive(1); }}" for name in names[:8]]
    lines += ["CLK { P: 0ns Drive(0), 50ns Drive(1), 100ns Drive(0); }", "}", "}", "}",
              "Pattern MAIN {", " W NORMAL;"]
    lines += ["V { ALL = " + "".join(str((i >> bit) & 1) for bit in range(8)) + "P; }" for i in range(count)]
    return "\n".join(lines + ["}"])


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    root = tmp_path_factory.mktemp("cli-counter")
    source = root / "counter.stil"; source.write_text(counter_stil())
    project = init_project(source, root / "project", wait_preset="none", completion="hold")
    bundle = build_project(project)
    timeline = load_bundle_timeline(bundle.bundle_dir, manifest_sha256=bundle.manifest_sha256)
    spec = CaptureRequestSpec.from_dict({"kind": "stpg_capture_request", "schema_version": 1,
                                        "address": {"vector": 25}, "before_vectors": 8,
                                        "after_vectors": 16, "strategy": "auto"})
    analyzer = analyzer_config()
    plan = plan_capture(timeline, spec, analyzer)
    assert plan["status"] == "PLANNED"
    return project, bundle, timeline, analyzer, plan


def fake_io(built):
    _, _, timeline, analyzer, plan = built
    model = ModelAnalyzer(analyzer.limits, provider=lambda req: synthesize(timeline, plan).samples)
    peer = SimulatedStpg(on_run=lambda index: model.fire())
    return peer, model, dict(stream_factory=lambda _: peer, port_provider=ports, sleep=lambda seconds: None)


def _external(built, tmp_path, plan=None, **options):
    """The core SDK's run with this tool's accessory in a thread (device mode, injected analyzer model)."""

    _, bundle, _, analyzer, original = built
    peer, model, io = fake_io(built)
    config = write_config(tmp_path / "acc", bundle=bundle, analyzer=analyzer, plan=plan or original)
    launched = []

    def factory(argv, **kwargs):
        launched.append(argv)
        return ThreadProcess(stream_factory=lambda _: model, port_provider=ports_of(analyzer))

    kwargs = dict(stream_factory=io["stream_factory"], port_provider=io["port_provider"], sleep=io["sleep"],
                  process_factory=factory)
    kwargs.update(options)
    result = external_accessory.run_with_external_accessory(
        bundle.bundle_dir, manifest_sha256=bundle.manifest_sha256, device=DEVICE,
        spec=load_external_accessory(config), output_dir=tmp_path / "run", confirm_run=True, **kwargs)
    return result, peer, model, launched


def test_capture_without_controller_is_one_core_run_with_the_accessory(built, tmp_path):
    result, peer, model, launched = _external(built, tmp_path)
    assert result["run"]["outcome"] == "COMPLETED" and result["control"] is None
    capture = read_json(tmp_path / "run" / "accessory" / "capture" / "capture.json")
    assert capture["outcome"] == "CAPTURED" and capture["timing"]["adopted"]
    assert result["external"]["adopted"] is True and accessory_exit(result) == 0
    assert peer.closed == model.closed == 1 and len(launched) == 1


def test_capture_and_gpio_compose_without_touching_run_pins(built, tmp_path):
    _, bundle, _, _, _ = built
    gpio = GpioPeer()
    actions = {"kind": "stpg_gpio_action_plan", "schema_version": 1, "manifest_sha256": bundle.manifest_sha256,
               "actions": [{"segment": 0, "phase": "before_run", "pin": 14, "state": "HIGH"},
                           {"segment": 0, "phase": "after_terminal", "pin": 14, "state": "Hi-Z"}]}
    result, peer, model, _ = _external(built, tmp_path, controller=CONFIG, control_plan=actions,
                                       controller_stream_factory=lambda _: gpio,
                                       controller_port_provider=controller_ports)
    assert result["run"]["outcome"] == result["control"]["outcome"] == "COMPLETED"
    assert result["external"]["outcome"] == "COMPLETED" and accessory_exit(result) == 0
    assert gpio.applied == [(14, "HIGH"), (14, "Hi-Z")] and gpio.closed == peer.closed == model.closed == 1


def test_full_current_sdk_admission_happens_before_the_accessory_starts(built, tmp_path, monkeypatch):
    monkeypatch.setattr(execution, "tool_fingerprint", lambda: "0" * 64)
    with pytest.raises(RunnerError, match="installed compiler/SDK differs"):
        _external(built, tmp_path)
    assert not (tmp_path / "run").exists()


def test_precancelled_capture_starts_no_accessory_and_sends_nothing(built, tmp_path):
    token = CancellationToken(); token.cancel()
    peer = []
    with pytest.raises(BuildCancelled):
        _external(built, tmp_path, cancel=token, process_factory=lambda *a, **k: peer.append(a))
    assert peer == [] and not (tmp_path / "run").exists()


@pytest.mark.parametrize("field", ["trigger", "manifest"])
def test_modified_capture_rejects_before_gpio_or_la_contact(built, tmp_path, monkeypatch, field):
    from copy import deepcopy
    _, bundle, _, analyzer, original = built
    plan = deepcopy(original)
    if field == "trigger":
        plan["trigger"]["value"] ^= 1
    else:
        plan["bundle"]["manifest_sha256"] = "0" * 64
    opened = []
    monkeypatch.setattr(la_device.AnalyzerSession, "open", lambda session: opened.append(True))
    gpio = GpioPeer()
    actions = {"kind": "stpg_gpio_action_plan", "schema_version": 1, "manifest_sha256": bundle.manifest_sha256,
               "actions": [{"segment": 0, "phase": "before_run", "pin": 14, "state": "HIGH"}]}
    result, peer, model, _ = _external(built, tmp_path, plan=plan, controller=CONFIG, control_plan=actions,
                                       controller_stream_factory=lambda _: gpio,
                                       controller_port_provider=controller_ports)
    assert result["external"]["outcome"] == "REJECT" and result["run"] is None and accessory_exit(result) == 2
    assert not opened and not peer.commands and gpio.applied == []
    assert read_json(tmp_path / "run" / "accessory" / "rejected.json")["analyzer_opened"] is False


def test_capture_cli_plan_mock_import_and_marker_are_offline(built, tmp_path, capsys):
    project, bundle, timeline, analyzer, plan = built
    binding = tmp_path / "la.json"; write_json(binding, analyzer.to_dict())
    planned = tmp_path / "plan.json"
    assert cli.main(["plan", str(bundle.bundle_dir), "--manifest-sha256", bundle.manifest_sha256,
                     "--analyzer", str(binding), "--vector", "25", "--before", "8", "--after", "16",
                     "--output", str(planned)]) == 0
    assert read_json(planned) == plan
    capsys.readouterr()
    assert cli.main(["mock", str(bundle.bundle_dir), "--manifest-sha256", bundle.manifest_sha256,
                     "--plan", str(planned), "--output-dir", str(tmp_path / "mock")]) == 0
    mocked = json.loads(capsys.readouterr().out)
    assert mocked["mode"] == "mock" and mocked["dut_judgement"] is None
    trace = synthesize(timeline, plan)
    csv_path = tmp_path / "capture.csv"
    csv_path.write_text(",".join(f"Channel {ch + 1}" for ch in trace.channels) + "\n" +
                        "\n".join(",".join(str(value >> bit & 1) for bit in range(len(trace.channels)))
                                  for value in trace.samples) + "\n")
    assert cli.main(["import", str(bundle.bundle_dir), "--manifest-sha256", bundle.manifest_sha256,
                     "--plan", str(planned), "--trace", str(csv_path), "--sample-rate", str(trace.sample_rate_hz),
                     "--trigger-index", str(trace.trigger_index), "--output-dir", str(tmp_path / "imported")]) == 0
    imported = json.loads(capsys.readouterr().out)
    assert imported["mode"] == "import" and imported["outcome"] == "IMPORTED" and imported["timing"]["adopted"]
    # Marker path uses the exact same derive/build/equivalence helpers as GUI.
    marker_binding = analyzer.to_dict(); marker_binding["marker_gpio"] = 18; write_json(binding, marker_binding)
    assert cli.main(["marker", str(bundle.bundle_dir), "--manifest-sha256", bundle.manifest_sha256,
                     "--project", str(project), "--analyzer", str(binding), "--vector", "25",
                     "--directory", str(tmp_path / "marker-project")]) == 0
    marker = json.loads(capsys.readouterr().out)
    assert marker["status"] == "EQUIVALENT_TIMING" and marker["device_io_performed"] is False


def test_the_runner_cli_runs_the_written_accessory_config(built, tmp_path, monkeypatch, capsys):
    from stpg_runner import cli as runner_cli
    _, bundle, _, analyzer, plan = built
    device_path, analyzer_path, plan_path = (tmp_path / name for name in ("device.json", "analyzer.json", "plan.json"))
    write_json(device_path, DEVICE.to_dict()); write_json(analyzer_path, analyzer.to_dict()); write_json(plan_path, plan)
    assert cli.main(["accessory-config", str(bundle.bundle_dir), "--manifest-sha256", bundle.manifest_sha256,
                     "--analyzer", str(analyzer_path), "--plan", str(plan_path), "--mode", "device",
                     "--device-config", str(device_path), "--directory", str(tmp_path / "acc")]) == 0
    written = json.loads(capsys.readouterr().out)
    assert written["status"] == "WRITTEN" and written["checked_by_core_sdk"] is True and written["segment"] == 0
    peer, model, io = fake_io(built)
    original = external_accessory.run_with_external_accessory

    def injected(*args, **kwargs):  # the CLI's own run, with test streams and the accessory in a thread
        kwargs.update(stream_factory=io["stream_factory"], port_provider=io["port_provider"],
                      sleep=io["sleep"], process_factory=lambda argv, **k: ThreadProcess(
                          stream_factory=lambda _: model, port_provider=ports_of(analyzer)))
        return original(*args, **kwargs)

    monkeypatch.setattr(external_accessory, "run_with_external_accessory", injected)
    assert runner_cli.main(["run", str(bundle.bundle_dir), "--manifest-sha256", bundle.manifest_sha256,
                            "--device-config", str(device_path), "--external-accessory", written["config"],
                            "--output-dir", str(tmp_path / "run"), "--confirm-run", "--progress", "none"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["external"]["outcome"] == "COMPLETED" and report["run"]["outcome"] == "COMPLETED"
    assert read_json(tmp_path / "run" / "accessory" / "capture" / "capture.json")["outcome"] == "CAPTURED"


def test_this_tool_has_no_command_that_runs_the_stpg(capsys):
    with pytest.raises(SystemExit) as stopped:
        cli.main(["run", "bundle", "--confirm-run"])
    assert stopped.value.code == 2 and "invalid choice" in capsys.readouterr().err


@pytest.mark.parametrize("field,value", [("frequency", 0), ("frequency", True), ("pre", True),
                                        ("channel", -1), ("channel", 24), ("channel", True),
                                        ("samples", "%%%"), ("samples", [0, 2]), ("duplicate", True)])
def test_malformed_import_is_rejected_not_adopted(tmp_path, field, value):
    from stpg_capture.trace import import_lac, TraceError
    data = {"Settings": {"Frequency": 100000000, "PreTriggerSamples": 0, "LoopCount": 0,
                         "CaptureChannels": [{"ChannelNumber": 0, "Samples": [0, 1]}]}}
    settings = data["Settings"]
    if field == "frequency":
        settings["Frequency"] = value
    elif field == "pre":
        settings["PreTriggerSamples"] = value
    elif field == "channel":
        settings["CaptureChannels"][0]["ChannelNumber"] = value
    elif field == "samples":
        settings["CaptureChannels"][0]["Samples"] = value
    else:
        settings["CaptureChannels"] *= 2
    path = tmp_path / "invalid.lac"; write_json(path, data)
    with pytest.raises(TraceError):
        import_lac(path)


def test_lac_positive_control_and_csv_invalid_channel(tmp_path):
    from stpg_capture.trace import import_lac, import_csv, TraceError
    path = tmp_path / "valid.lac"
    write_json(path, {"Settings": {"Frequency": 100000000, "PreTriggerSamples": 1,
                                   "CaptureChannels": [{"ChannelNumber": 2, "Samples": "AAE="}]}})
    trace = import_lac(path)
    assert trace.samples == [0, 1] and trace.channels == (2,) and trace.trigger_index == 1
    csv = tmp_path / "invalid.csv"; csv.write_text("Channel 25\n0\n1\n")
    with pytest.raises(TraceError):
        import_csv(csv, sample_rate_hz=100000000, trigger_index=0)


@pytest.mark.parametrize("persisted", [False, None])
def test_accessory_cli_never_hides_unpersisted_core_behind_adopted_capture(persisted):
    run = {"outcome": "COMPLETED", "comparison": "MATCH", "result_persisted": persisted}
    capture = {"outcome": "CAPTURED", "record": "FINAL", "result_persisted": True,
               "alignment": {"status": "ALIGNED", "confidence": "normal", "coverage": "FULL", "offset_ps": 0}}
    assert accessory_exit({"run": run, "capture": capture}) == 2
