"""Explicit simulated or device mode of the accessory, end to end through the core SDK (no hardware)."""
import io
import json
import os
import threading
import time

import pytest

from stpg_runner import accessory_exit, load_external_accessory, run_with_external_accessory
from stpg_runner.artifacts import RunnerError, read_json
from stpg_capture import accessory
from accessory_harness import ThreadProcess, run_external, write_config
from la_support import CONFIG, DEVICE, Peer, controller_ports, ports
from test_stpg_capture_cli import built, fake_io  # noqa: F401 - module fixture

pytestmark = pytest.mark.stpg_host_only


def _actions(bundle):
    return {"kind": "stpg_gpio_action_plan", "schema_version": 1, "manifest_sha256": bundle.manifest_sha256,
            "actions": [{"segment": 0, "phase": "before_run", "pin": 14, "state": "LOW"}]}


@pytest.mark.parametrize("controlled", [False, True])
def test_simulated_mode_reaches_the_persisted_capture_without_affecting_wire_order(built, tmp_path, controlled):
    _, bundle, _, analyzer, plan = built
    peer, _, options = fake_io(built)
    gpio = Peer()
    config = write_config(tmp_path / "acc", bundle=bundle, analyzer=analyzer, plan=plan, mode="simulated")
    started = []

    def factory(argv, **kwargs):  # simulated: the accessory builds its own analyzer model, nothing is injected
        started.append(argv)
        return ThreadProcess(stream_factory=None, port_provider=None)

    result = run_with_external_accessory(
        bundle.bundle_dir, manifest_sha256=bundle.manifest_sha256, device=DEVICE, spec=load_external_accessory(config),
        output_dir=tmp_path / "run", confirm_run=True, controller=CONFIG if controlled else None,
        control_plan=_actions(bundle) if controlled else None, process_factory=factory,
        controller_stream_factory=lambda _: gpio, controller_port_provider=controller_ports,
        stream_factory=options["stream_factory"], port_provider=options["port_provider"],
        sleep=options["sleep"])
    assert result["run"]["outcome"] == "COMPLETED" and accessory_exit(result) == 0
    assert result["external"]["mode"] == "simulated" and result["external"]["adopted"] is True
    capture = read_json(tmp_path / "run" / "accessory" / "capture" / "capture.json")
    assert capture["mode"] == "simulated" and capture["physical_calibration"] is False
    assert read_json(tmp_path / "run" / "accessory" / "result.json")["mode"] == "simulated"
    assert gpio.applied == ([(14, "LOW")] if controlled else []) and peer.closed == 1 and len(started) == 1


@pytest.mark.parametrize("mode", ["mock", "import", "", True, 0, None, []])
def test_the_core_rejects_an_unsupported_mode_before_any_launch(built, tmp_path, mode):
    _, bundle, _, analyzer, plan = built
    config = write_config(tmp_path / "acc", bundle=bundle, analyzer=analyzer, plan=plan)
    raw = json.loads(config.read_text())
    raw["mode"] = mode
    config.write_text(json.dumps(raw))
    with pytest.raises(RunnerError):
        load_external_accessory(config)


@pytest.mark.parametrize("mode", ["mock", "Device", ""])
def test_the_accessory_rejects_an_unsupported_mode_at_open(built, tmp_path, mode):
    _, bundle, _, analyzer, plan = built
    config = write_config(tmp_path / "acc", bundle=bundle, analyzer=analyzer, plan=plan)
    spec = json.loads(config.read_text())
    out = tmp_path / "accessory"
    out.mkdir()
    request = {"protocol": accessory.PROTOCOL, "nonce": "n" * 32, "seq": 1, "method": "open",
               "bundle_dir": str(bundle.bundle_dir), "manifest_sha256": bundle.manifest_sha256,
               "binding_file": str(tmp_path / "acc" / "binding.json"), "binding_sha256": spec["binding_sha256"],
               "plan_file": str(tmp_path / "acc" / "plan.json"), "plan_sha256": spec["plan_sha256"],
               "output_dir": str(out), "segment": 0, "mode": mode}
    reply, code = _one_request(request)
    assert code == 1 and reply["status"] == "ERROR" and not (out / "capture").exists()


def _one_request(request: dict) -> tuple[dict, int]:
    """One request on a pipe that the host keeps open until the reply arrived, as the core does."""

    read_in, write_in = os.pipe()
    stdout, codes = io.BytesIO(), []
    with os.fdopen(read_in, "rb") as stdin, os.fdopen(write_in, "wb", buffering=0) as host:
        worker = threading.Thread(target=lambda: codes.append(accessory.serve(stdin, stdout)), daemon=True)
        worker.start()
        host.write(json.dumps(request).encode() + b"\n")
        deadline = time.monotonic() + 30
        while not stdout.getvalue().endswith(b"\n") and time.monotonic() < deadline:
            time.sleep(0.01)
        host.close()  # the host's EOF after the reply
        worker.join(30)
    return json.loads(stdout.getvalue()), codes[0]


@pytest.mark.parametrize("missing", ["stream_factory", "port_provider"])
def test_a_simulated_accessory_needs_the_injected_core_streams_before_any_launch(built, tmp_path, missing):
    _, bundle, _, analyzer, plan = built
    config = write_config(tmp_path / "acc", bundle=bundle, analyzer=analyzer, plan=plan, mode="simulated")
    options = {"stream_factory": lambda _: None, "port_provider": ports}
    options[missing] = None
    started = []
    with pytest.raises(RunnerError, match="injected"):
        run_with_external_accessory(bundle.bundle_dir, manifest_sha256=bundle.manifest_sha256, device=DEVICE,
                                    spec=load_external_accessory(config), output_dir=tmp_path / "run",
                                    confirm_run=True, process_factory=lambda *a, **k: started.append(a), **options)
    assert started == [] and not (tmp_path / "run").exists()


@pytest.mark.parametrize("missing", ["controller_stream_factory", "controller_port_provider"])
def test_a_simulated_accessory_with_a_controller_needs_its_injected_streams(built, tmp_path, missing):
    _, bundle, _, analyzer, plan = built
    config = write_config(tmp_path / "acc", bundle=bundle, analyzer=analyzer, plan=plan, mode="simulated")
    gpio = Peer()
    options = {"stream_factory": lambda _: None, "port_provider": ports,
               "controller_stream_factory": lambda _: gpio, "controller_port_provider": controller_ports}
    options[missing] = None
    with pytest.raises(RunnerError, match="injected"):
        run_with_external_accessory(bundle.bundle_dir, manifest_sha256=bundle.manifest_sha256, device=DEVICE,
                                    spec=load_external_accessory(config), output_dir=tmp_path / "run",
                                    confirm_run=True, controller=CONFIG, control_plan=_actions(bundle), **options)
    assert not gpio.applied and not (tmp_path / "run").exists()


def test_device_mode_with_injected_analyzer_streams_is_reported_as_device(built, tmp_path):
    # Injection here is a test of this program; the mode a result reports is the selected one, never upgraded.
    _, bundle, _, analyzer, plan = built
    view, _, model, process = run_external(bundle, plan, tmp_path / "out", analyzer=analyzer)
    assert view["external"]["mode"] == view["capture"]["mode"] == "device" and process.returncode == 0
    assert model.closed == 1
