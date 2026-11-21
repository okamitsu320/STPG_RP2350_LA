"""A core run with this tool as its external accessory, inside one test process (no hardware).

``run_external`` writes an accessory config folder (binding, plan and ``external.json`` as files, so a test can
also write a plan that ``write_external_config`` would refuse) and calls the core SDK's
``run_with_external_accessory``. Its process factory runs ``accessory.serve`` in a thread on OS pipes instead of a
child process: the analyzer byte model is injected in device mode, and the simulated STPG fires it at RUN.
``test_accessory_process.py`` covers the real child process.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import subprocess
import sys
import threading
from types import SimpleNamespace

from stpg_capture import accessory
from stpg_capture.mock import ModelAnalyzer, synthesize
from stpg_capture.timeline import load_bundle_timeline
from stpg_runner import load_external_accessory, run_with_external_accessory
from stpg_runner.artifacts import canonical_json, read_json, write_json
from stpgview.runner.simulated import SIMULATED_DEVICE, SimulatedStpg, simulated_ports


def ports_of(config):
    return lambda: [SimpleNamespace(device=config.port, vid=config.vid, pid=config.pid,
                                    serial_number=config.serial_number)]


class ThreadProcess:
    """A Popen look-alike running ``accessory.serve`` in a thread on OS pipes."""

    def __init__(self, *, stream_factory, port_provider):
        child_in, self._w_in = os.pipe()
        self._r_out, child_out = os.pipe()
        self._r_err, child_err = os.pipe()
        self.stdin = os.fdopen(self._w_in, "wb", buffering=0)
        self.stdout = os.fdopen(self._r_out, "rb", buffering=0)
        self.stderr = os.fdopen(self._r_err, "rb", buffering=0)
        self._code = None
        self.terminated = 0

        def target():
            with os.fdopen(child_in, "rb") as i, os.fdopen(child_out, "wb", buffering=0) as o, \
                    os.fdopen(child_err, "wb", buffering=0) as e:
                try:
                    self._code = accessory.serve(i, o, e, stream_factory=stream_factory, port_provider=port_provider)
                except BaseException:  # noqa: BLE001 - an escaped exception is a crashed child
                    self._code = 99

        self.thread = threading.Thread(target=target, daemon=True)
        self.thread.start()
        self.returncode = None

    def poll(self):
        if self.thread.is_alive():
            return None
        self.returncode = self._code
        return self.returncode

    def wait(self, timeout=None):
        self.thread.join(timeout)
        if self.thread.is_alive():
            raise subprocess.TimeoutExpired("accessory-thread", timeout)
        self.returncode = self._code
        return self.returncode

    def terminate(self):
        self.terminated += 1

    kill = terminate


def write_config(root: Path, *, bundle, analyzer, plan, derivation=None, mode="device", segment=None,
                 timeout=10.0) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    write_json(root / "binding.json", analyzer.to_dict())
    (root / "plan.json").write_bytes(canonical_json({"kind": accessory.PLAN_KIND, "schema_version": 1,
                                                     "capture_plan": plan, "marker_derivation": derivation}) + b"\n")
    argv = [os.path.abspath(sys.executable), "-I", "-m", "stpg_capture.accessory"]
    sha = lambda path: hashlib.sha256(Path(path).read_bytes()).hexdigest()  # noqa: E731
    write_json(root / "external.json", {
        "kind": "stpg_external_accessory", "schema_version": 1, "identifier": "stpg-la", "command": argv,
        "executable_sha256": sha(argv[0]), "binding_file": "binding.json", "binding_sha256": sha(root / "binding.json"),
        "plan_file": "plan.json", "plan_sha256": sha(root / "plan.json"),
        "segment": plan["bundle"]["segment"] if segment is None else segment, "mode": mode, "timeout_s": timeout})
    return root / "external.json"


def run_external(bundle, plan, out: Path, *, analyzer, model=None, stpg=None, device=SIMULATED_DEVICE,
                 derivation=None, controller=None, control_plan=None, controller_io=None, cancel=None,
                 timeout=10.0, segment=None):
    """Returns (view, stpg, model, process): view has run, external, capture (capture.json or None), folders."""

    if model is None:
        timeline = load_bundle_timeline(bundle.bundle_dir, manifest_sha256=bundle.manifest_sha256)
        samples = synthesize(timeline, plan).samples
        model = ModelAnalyzer(analyzer.limits, provider=lambda request: samples)
    stpg = stpg or SimulatedStpg(on_run=lambda index: model.fire())
    config = write_config(out.parent / f"{out.name}-accessory-config", bundle=bundle, analyzer=analyzer, plan=plan,
                          derivation=derivation, timeout=timeout, segment=segment)
    processes = []

    def factory(argv, **kwargs):
        process = ThreadProcess(stream_factory=lambda _config: model, port_provider=ports_of(analyzer))
        processes.append(process)
        return process

    result = run_with_external_accessory(
        bundle.bundle_dir, manifest_sha256=bundle.manifest_sha256, device=device,
        spec=load_external_accessory(config), output_dir=out, confirm_run=True, controller=controller,
        control_plan=control_plan, cancel=cancel, stream_factory=lambda _config: stpg, port_provider=simulated_ports,
        process_factory=factory, sleep=lambda seconds: None, **(controller_io or {}))
    folder = out / "accessory"
    capture = folder / "capture" / "capture.json"
    rejected = folder / "rejected.json"
    view = {"run": result["run"], "external": result["external"], "control": result["control"],
            "capture": read_json(capture) if capture.is_file() else None,
            "rejected": read_json(rejected) if rejected.is_file() else None,
            "capture_dir": str(capture.parent), "run_dir": str(out / "stpg"), "output_dir": str(out),
            "result": result}
    return view, stpg, model, (processes[0] if processes else None)
