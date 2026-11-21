"""The real accessory process, its request grammar and the config writer (host only, simulated analyzer).

The positive control starts this tool exactly as the core does (``python -I -m stpg_capture.accessory`` from the
config written by ``write_external_config``) and runs a bundle with the core SDK and its simulated STPG. It needs
this package installed in the interpreter's environment: isolated mode
ignores PYTHONPATH and the working directory on purpose.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import threading

import pytest

from stpg_capture import accessory, external_config
from stpg_capture.align import align
from stpg_capture.config import AnalyzerConfig
from stpg_capture.marker import check_marker_bundle, derive_marker_project, is_marker_bundle
from stpg_capture.mock import synthesize
from stpg_capture.planner import plan_capture
from stpg_capture.timeline import load_bundle_timeline
from stpg_runner import (accessory_exit, build_project, init_project, load_external_accessory, load_project,
                         run_with_external_accessory)
from stpg_runner.artifacts import RunnerError, read_json, write_json
from stpgview.runner.simulated import SIMULATED_DEVICE, SimulatedStpg, simulated_ports

from la_support import forbid_hardware
from test_stpg_capture import analyzer_config, counter_stil, request
from test_stpg_capture_boundaries import config as boundary_config

pytestmark = pytest.mark.stpg_host_only


@pytest.fixture(autouse=True)
def _no_hardware(monkeypatch):
    forbid_hardware(monkeypatch)


@pytest.fixture(scope="module")
def counter(tmp_path_factory):
    root = tmp_path_factory.mktemp("process-counter")
    (root / "p.stil").write_text(counter_stil(1600))
    built = build_project(init_project(root / "p.stil", root / "project", wait_preset="none", completion="hold"))
    timeline = load_bundle_timeline(built.bundle_dir, manifest_sha256=built.manifest_sha256)
    analyzer = analyzer_config()
    write_json(root / "la.json", analyzer.to_dict())
    return built, timeline, analyzer, root / "la.json", plan_capture(timeline, request(300), analyzer)


def _write(counter, folder, **options):
    built, _, _, binding, plan = counter
    return external_config.write_external_config(folder, bundle_dir=built.bundle_dir,
                                                 manifest_sha256=built.manifest_sha256, analyzer_binding=binding,
                                                 capture_plan=plan, **options)


# ---- the real child process ----------------------------------------------------------------------------------

def test_the_core_starts_the_installed_tool_and_gets_its_neutral_result(counter, tmp_path):
    built = counter[0]
    info = _write(counter, tmp_path / "acc", mode="simulated", device=SIMULATED_DEVICE)
    assert info["checked_by_core_sdk"] is True and 5.0 <= info["timeout_s"] <= 20.0
    spec = load_external_accessory(Path(info["config"]))
    assert spec.command[1:] == ("-I", "-m", "stpg_capture.accessory") and Path(spec.command[0]).is_absolute()
    result = run_with_external_accessory(built.bundle_dir, manifest_sha256=built.manifest_sha256,
                                         device=SIMULATED_DEVICE, spec=spec, output_dir=tmp_path / "run",
                                         confirm_run=True, stream_factory=lambda _: SimulatedStpg(),
                                         port_provider=simulated_ports)
    external = result["external"]
    assert result["run"]["outcome"] == "COMPLETED" and accessory_exit(result) == 0
    assert (external["outcome"], external["mode"], external["adopted"], external["returncode"]) == (
        "COMPLETED", "simulated", True, 0)
    assert external["stderr_hex"] == "" and [r["method"] for r in external["requests"]] == [
        "open", "before_run", "after_terminal"]
    capture = read_json(tmp_path / "run" / "accessory" / "capture" / "capture.json")
    assert capture["mode"] == "simulated" and capture["outcome"] == "CAPTURED" and capture["timing"]["adopted"]
    assert capture["accessory"]["cancel_byte_sent"] is False


def test_a_partial_line_at_eof_is_never_a_command(counter, tmp_path):
    process = subprocess.run(external_config.accessory_command(), input=b'{"protocol":', capture_output=True,
                             timeout=60)
    assert process.returncode == 1 and process.stdout == b""
    assert b"partial or oversized request line" in process.stderr


def test_usage_and_an_empty_session(counter):
    process = subprocess.run(external_config.accessory_command() + ["extra"], capture_output=True, timeout=60)
    assert process.returncode == 2 and process.stdout == b""
    process = subprocess.run(external_config.accessory_command(), input=b"", capture_output=True, timeout=60)
    assert (process.returncode, process.stdout, process.stderr) == (0, b"", b"")  # the host stopped before open


# ---- request grammar ------------------------------------------------------------------------------------------

def _reply_line(replies, seconds: float = 20.0) -> dict | None:
    """One reply line, or None when none arrives (a request without a readable identity gets no reply)."""

    ready, _, _ = select.select([replies], [], [], seconds)
    return json.loads(replies.readline()) if ready else None


def _exchange(lines: list[bytes]) -> tuple[list[dict | None], int]:
    """Send request lines on a pipe the host keeps open until each reply arrived, as the core does."""

    read_in, write_in = os.pipe()
    read_out, write_out = os.pipe()
    codes = []
    with os.fdopen(read_in, "rb") as stdin, os.fdopen(write_out, "wb", buffering=0) as stdout, \
            os.fdopen(write_in, "wb", buffering=0) as host, os.fdopen(read_out, "rb") as replies:
        worker = threading.Thread(target=lambda: codes.append(accessory.serve(stdin, stdout)), daemon=True)
        worker.start()
        answers = []
        for line in lines:
            host.write(line)
            answers.append(_reply_line(replies, 3.0 if b"NaN" in line or line.count(b'"seq"') > 1 else 20.0))
            if answers[-1] is None or answers[-1]["status"] == "ERROR":
                break
        host.close()
        worker.join(30)
        stdout.close()
    return answers, codes[0]


def _open(counter, tmp_path, **change):
    built, _, analyzer, _, plan = counter
    from accessory_harness import write_config

    config = write_config(tmp_path / "acc", bundle=built, analyzer=analyzer, plan=plan)
    spec = read_json(config)
    (tmp_path / "out").mkdir(exist_ok=True)
    value = {"protocol": accessory.PROTOCOL, "nonce": "a" * 32, "seq": 1, "method": "open",
             "bundle_dir": str(built.bundle_dir), "manifest_sha256": built.manifest_sha256,
             "binding_file": str(tmp_path / "acc" / "binding.json"), "binding_sha256": spec["binding_sha256"],
             "plan_file": str(tmp_path / "acc" / "plan.json"), "plan_sha256": spec["plan_sha256"],
             "output_dir": str(tmp_path / "out"), "segment": 0, "mode": "simulated"}
    value.update(change)
    return value


@pytest.mark.parametrize("raw", [
    lambda r: json.dumps(r).encode()[:-1] + b', "seq": 1}',  # duplicate key
    lambda r: json.dumps({**r, "segment": float("nan")}).encode(),  # non-finite number
    lambda r: json.dumps({**r, "extra": 1}).encode(),  # unknown field
    lambda r: json.dumps({**r, "protocol": "stpg_external_accessory_v2"}).encode(),
    lambda r: json.dumps({**r, "seq": 2}).encode(),
    lambda r: json.dumps({**r, "method": "before_run"}).encode(),
    lambda r: json.dumps({**r, "binding_sha256": "0" * 64}).encode(),  # snapshot hash differs
    lambda r: json.dumps({**r, "segment": True}).encode(),
])
def test_requests_are_strict_and_a_rejection_is_one_error_reply(counter, tmp_path, raw):
    line = raw(_open(counter, tmp_path)) + b"\n"
    answers, code = _exchange([line])
    assert code == 1 and len(answers) == 1
    if b"NaN" in line or line.count(b'"seq"') > 1:  # unreadable: no identity to echo, so no reply at all
        assert answers == [None] and not (tmp_path / "out" / "capture").exists()
        return
    assert set(answers[0]) == {"protocol", "nonce", "seq", "method", "status", "result_file", "result_sha256"}
    assert answers[0]["status"] == "ERROR" and answers[0]["result_file"] is None
    assert not (tmp_path / "out" / "capture").exists()


def test_after_ready_the_host_stopping_closes_the_analyzer_once(counter, tmp_path):
    answers, code = _exchange([json.dumps(_open(counter, tmp_path)).encode() + b"\r\n"])  # CR LF is accepted
    assert code == 0 and [a["status"] for a in answers] == ["READY"]
    capture = read_json(tmp_path / "out" / "capture" / "capture.json")
    assert capture["outcome"] == "NO_RUN" and capture["record"] == "FINAL"
    session = capture["analyzer"]["session"]
    assert session["close_attempts"] == session["close_successes"] == 1 and not session["cancel_sent"]


def test_a_request_after_complete_or_out_of_order_is_rejected(counter, tmp_path):
    built, _, _, _, plan = counter
    first = _open(counter, tmp_path)
    arm = {"protocol": accessory.PROTOCOL, "nonce": first["nonce"], "seq": 2, "method": "before_run", "segment": 0,
           "pattern_sha256": plan["bundle"]["segment_sha256"]}
    skipped = {**arm, "method": "after_terminal", "seq": 2}
    del skipped["pattern_sha256"]
    answers, code = _exchange([json.dumps(first).encode() + b"\n", json.dumps(skipped).encode() + b"\n"])
    assert code == 1 and [a["status"] for a in answers] == ["READY", "ERROR"]
    wrong_nonce = _exchange([json.dumps(_open(counter, tmp_path / "n")).encode() + b"\n",
                             json.dumps({**arm, "nonce": "b" * 32}).encode() + b"\n"])
    assert [a["status"] for a in wrong_nonce[0]] == ["READY", "ERROR"]


# ---- the config writer -----------------------------------------------------------------------------------------

def test_the_writer_checks_offline_and_never_overwrites(counter, tmp_path):
    info = _write(counter, tmp_path / "acc", mode="device")
    folder = Path(info["config"]).parent
    assert sorted(p.name for p in folder.iterdir()) == ["binding.json", "external.json", "plan.json"]
    spec = read_json(Path(info["config"]))
    assert spec["binding_sha256"] == hashlib.sha256((folder / "binding.json").read_bytes()).hexdigest()
    assert (folder / "binding.json").read_bytes() == counter[3].read_bytes()  # a byte copy
    assert read_json(folder / "plan.json")["kind"] == accessory.PLAN_KIND and info["mode"] == "device"
    with pytest.raises(FileExistsError):
        _write(counter, tmp_path / "acc", mode="device")
    built, _, _, binding, plan = counter
    edited = json.loads(json.dumps(plan))
    edited["trigger"]["value"] ^= 1
    with pytest.raises(Exception, match="re-derivation"):
        external_config.write_external_config(tmp_path / "edited", bundle_dir=built.bundle_dir,
                                              manifest_sha256=built.manifest_sha256, analyzer_binding=binding,
                                              capture_plan=edited, mode="device")
    assert not (tmp_path / "edited").exists()
    with pytest.raises(RunnerError, match="outside the immutable bundle"):
        _write(counter, built.bundle_dir / "acc", mode="device")


def test_the_writer_runs_the_core_identity_check_and_bounds_the_reply_limit(counter, tmp_path, monkeypatch):
    built, _, analyzer, _, plan = counter
    same = {**analyzer.to_dict(), "vid": SIMULATED_DEVICE.vid, "pid": SIMULATED_DEVICE.pid,
            "serial_number": SIMULATED_DEVICE.serial_number}
    write_json(tmp_path / "same.json", same)
    with pytest.raises(RunnerError):
        external_config.write_external_config(tmp_path / "alias", bundle_dir=built.bundle_dir,
                                              manifest_sha256=built.manifest_sha256,
                                              analyzer_binding=tmp_path / "same.json", capture_plan=plan,
                                              mode="device", device=SIMULATED_DEVICE)
    monkeypatch.setattr(external_config, "collect_timeout", lambda plan, request: 25.0)
    with pytest.raises(RunnerError, match="reply limit"):
        _write(counter, tmp_path / "slow", mode="device")
    monkeypatch.setattr(external_config, "collect_timeout", lambda plan, request: 1.0)
    assert _write(counter, tmp_path / "fast", mode="device")["timeout_s"] == 5.0
    with pytest.raises(RunnerError, match="mode"):
        _write(counter, tmp_path / "mode", mode="mock")


def test_the_command_keeps_a_virtual_environment_link(tmp_path):
    link = tmp_path / "venv" / "bin" / "python"
    link.parent.mkdir(parents=True)
    link.symlink_to(sys.executable)
    assert external_config.accessory_command(str(link))[0] == str(link)  # not the base interpreter it points to


# ---- moved from the core with the analyzer code -----------------------------------------------------------------

def test_marker_derivation_reads_and_stores_compressed_sources_with_unchanged_timing(tmp_path):
    (tmp_path / "public.stil").write_text(counter_stil(1600))  # large enough to be stored compressed
    project = init_project(tmp_path / "public.stil", tmp_path / "p", wait_preset="none", completion="hold")
    parent = build_project(project)
    assert not is_marker_bundle(parent.bundle_dir)
    analyzer = AnalyzerConfig.from_dict({**boundary_config().to_dict(), "wiring": "stpg20-23-to-la19-22"})
    derived = derive_marker_project(project, bundle_dir=parent.bundle_dir, manifest_sha256=parent.manifest_sha256,
                                    vector=25, analyzer=analyzer, marker_gpio=23, directory=tmp_path / "marker")
    child_project = Path(derived["project_path"])
    assert load_project(child_project).input_paths(child_project.parent)["stil"].suffix == ".gz"
    child = build_project(child_project)
    assert is_marker_bundle(child.bundle_dir)
    timeline = load_bundle_timeline(child.bundle_dir, manifest_sha256=child.manifest_sha256)
    checked = check_marker_bundle(timeline, derived["derivation"], analyzer=analyzer)
    assert checked["verification"]["status"] == "EQUIVALENT_TIMING"


def test_a_capture_of_a_rounded_bundle_is_planned_and_aligned_on_the_emitted_times(tmp_path):
    # 402.5 ns -> 81 ticks = 405 ns (half up); the CLK fall 301 ns -> 60 ticks = 300 ns.
    text = counter_stil(1600).replace("Period '400ns'", "Period '402.5ns'").replace("300ns Drive(0)", "301ns Drive(0)")
    (tmp_path / "counter.stil").write_text(text)
    bundle = build_project(init_project(tmp_path / "counter.stil", tmp_path / "p", wait_preset="none",
                                        completion="hold", allow_approx=True))
    timeline = load_bundle_timeline(bundle.bundle_dir, manifest_sha256=bundle.manifest_sha256)
    trace = timeline.segments[0].trace
    assert [trace.starts[k] for k in (0, 1, 1000)] == [0, 405_000, 405_000_000]  # not 402 500 ps per vector
    clk = [i for i, s in enumerate(trace.signals) if s.name == "CLK"][0]
    assert sorted(e.t for e in trace.events(5) if e.signal == clk) == [0, 100_000, 300_000]
    plan = plan_capture(timeline, request(1200, after=200), analyzer_config())
    assert plan["status"] == "PLANNED", plan["reasons"]
    assert plan["window"]["start_ps"] == plan["window"]["first_vector"] * 405_000
    result = align(timeline, plan, synthesize(timeline, plan, offset_ps=0))
    assert result["status"] == "ALIGNED" and abs(result["offset_ps"]) <= plan["sample_period_ps"]
