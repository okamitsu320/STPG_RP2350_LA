"""Abnormal-path and integration tests of the analyzer accessory in a core run (host only, synthetic inputs).

Covers the accessory's place in the core's per-segment hooks (arm right before the target RUN, collect only after
a valid DONE/HOLD), canonical plan re-derivation at ``open``, fail-closed cleanup and persistence without a cancel
byte, analyzer byte evidence, marker provenance, the simulated STPG's RUN-argument checks and cross-role leases.
Every peer is an in-memory byte model and the accessory runs in a thread (``accessory_harness``); the hardware guard
forbids real serial opens and USB metadata enumeration. None of this is physical acceptance.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import shutil
import threading
import time

import pytest

from stpg import constants as c
from stpg_capture import accessory, la_device
from stpg_capture import la_protocol as lp
from stpg_capture.acceptance import capture_exit, timing_adoption
from stpg_capture.cli import main as capture_main
from stpg_capture.coordinator import offline_capture
from stpg_capture.la_device import AnalyzerError, AnalyzerSession
from stpg_capture.marker import MarkerError, check_marker_bundle, derive_marker_project
from stpg_capture.mock import PICO2_LIMITS, PICO2_TURBO_LIMITS, ModelAnalyzer, capture_bytes, synthesize
from stpg_capture.planner import PlanError, plan_capture
from stpg_capture.timeline import load_bundle_timeline
from stpg_runner import CancellationToken, accessory_exit, build_project, physical_identity_key, run_bundle
from stpg_runner.artifacts import RunnerError, read_json
from stpg_runner.cli import main as runner_main
from stpg_runner.device import DeviceLease
from stpgview.runner.simulated import SIMULATED_DEVICE, SimulatedStpg, simulated_ports

from accessory_harness import run_external
from la_support import forbid_hardware
from test_stpg_capture import _build, _ports, analyzer_config, counter_stil, request
from test_capture_segments import counter_en_stil

pytestmark = pytest.mark.stpg_host_only


@pytest.fixture(autouse=True)
def _no_hardware(monkeypatch):
    forbid_hardware(monkeypatch)


@pytest.fixture(scope="module")
def counter(tmp_path_factory):
    built = _build(tmp_path_factory, "failpath-counter", counter_stil(1600))
    return built, load_bundle_timeline(built.bundle_dir, manifest_sha256=built.manifest_sha256)


@pytest.fixture(scope="module")
def segmented(tmp_path_factory):
    root = tmp_path_factory.mktemp("failpath-segmented")
    source = root / "counter.stil"
    source.write_text(counter_en_stil(3200, 1600), encoding="utf-8")
    assert runner_main(["init", str(source), "--directory", str(root / "project"), "--wait-preset", "none",
                        "--completion", "hold", "--max-segment-vectors", "1600"]) == 0
    built = build_project(root / "project" / "project.stpg-project.json")
    return built, load_bundle_timeline(built.bundle_dir, manifest_sha256=built.manifest_sha256)


@pytest.fixture
def short_wait(monkeypatch):
    """The accessory's bounded wait for samples, shortened for tests where nothing (complete) arrives."""

    def set_wait(seconds):
        monkeypatch.setattr(accessory, "collect_timeout", lambda plan, request: seconds)
    return set_wait


def run(built, timeline, plan, out, *, model=None, stpg=None, analyzer=None, **kw):
    view, stpg, model, _ = run_external(built, plan, out, analyzer=analyzer or analyzer_config(), model=model,
                                        stpg=stpg, **kw)
    return view, stpg, model


def hook_events(result):
    return [(e["phase"], e["segment_index"], e["outcome"])
            for e in read_json(Path(result["run_dir"]) / "run.json.gz")["capture"]["hook_events"]]


class Watched(ModelAnalyzer):
    """Calls ``on_request`` when the capture request was written (the accessory just armed the analyzer)."""

    def __init__(self, *args, on_request=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.on_request = on_request

    def write(self, data):
        before = len(self.requests)
        result = super().write(data)
        if len(self.requests) > before and self.on_request is not None:
            self.on_request()
        return result


def _watched(timeline, plan, on_request):
    samples = synthesize(timeline, plan).samples
    return Watched(PICO2_TURBO_LIMITS, provider=lambda req: samples, on_request=on_request)


# ---- the accessory in the core's per-segment hooks ----------------------------------------------------------

def test_arm_follows_the_target_upload_and_immediately_precedes_its_run(counter, tmp_path):
    built, timeline = counter
    plan = plan_capture(timeline, request(300), analyzer_config())
    seen = {}
    stpg = SimulatedStpg(on_run=lambda index: model.fire())
    model = _watched(timeline, plan, lambda: seen.update(commands=list(stpg.commands)))
    result, _, _ = run(built, timeline, plan, tmp_path / "o", model=model, stpg=stpg)
    assert c.CMD_UPLOAD_END in seen["commands"] and c.CMD_RUN not in seen["commands"]
    assert stpg.commands[len(seen["commands"])] == c.CMD_RUN  # the next STPG request after arming is RUN
    arming = result["capture"]["arming"]
    assert arming["before_run_segments"] == [0] and arming["armed_segment"] == 0
    assert arming["target_terminal"] == "DONE/HOLD (reported by the host)"
    assert hook_events(result) == [("before_run", 0, "COMPLETED"), ("after_terminal", 0, "COMPLETED")]
    assert result["capture"]["outcome"] == "CAPTURED" and result["capture"]["timing"]["adopted"]
    assert result["external"]["adopted"] is True and accessory_exit(result["result"]) == 0


def test_segmented_run_arms_only_the_target_segment_and_keeps_run_arguments(segmented, tmp_path):
    built, timeline = segmented
    plan = plan_capture(timeline, request(timeline.segments[1].global_start + 300), analyzer_config())
    assert plan["status"] == "PLANNED" and plan["bundle"]["segment"] == 1
    seen = {}
    stpg = SimulatedStpg(on_run=lambda index: model.fire())
    model = _watched(timeline, plan, lambda: seen.update(commands=list(stpg.commands)))
    result, _, _ = run(built, timeline, plan, tmp_path / "o", model=model, stpg=stpg)
    assert seen["commands"].count(c.CMD_RUN) == 1  # segment 0 ran unobserved, then segment 1 was uploaded
    assert stpg.commands[len(seen["commands"])] == c.CMD_RUN
    arming = result["capture"]["arming"]
    assert arming["before_run_segments"] == [1] and arming["armed_segment"] == 1  # no request for segment 0
    assert len(model.requests) == 1
    sdk_args = [tuple(s["run_args"]) for s in read_json(Path(result["run_dir"]) / "run.json.gz")["plan"]["segments"]]
    assert stpg.run_args == sdk_args and not stpg.rejected_runs  # RUN arguments reach the peer verbatim
    assert result["run"]["outcome"] == "COMPLETED"
    assert result["capture"]["outcome"] == "CAPTURED" and result["capture"]["timing"]["adopted"]


def test_refused_arm_stops_the_sdk_before_run_without_cleanup(counter, tmp_path):
    built, timeline = counter
    plan = plan_capture(timeline, request(300), analyzer_config())
    model = ModelAnalyzer(PICO2_TURBO_LIMITS, reject_capture=True)
    result, stpg, _ = run(built, timeline, plan, tmp_path / "o", model=model)
    assert c.CMD_RUN not in stpg.commands and stpg.commands[-1] == c.CMD_UPLOAD_END  # no STOP/RESET/release
    assert stpg.closed == 1 and model.closed == 1
    assert result["run"]["outcome"] == "REJECT" and result["external"]["outcome"] == "REJECT"
    capture = result["capture"]
    assert capture["outcome"] == "ANALYZER_ERROR" and "refused" in capture["arming"]["error"]
    assert hook_events(result) == [("before_run", 0, "REJECT")]
    assert b"\xff" not in model.writes  # never a cancel byte from the accessory
    assert capture["analyzer"]["session"]["start_answer"] == "CAPTURE_ERROR"


def test_cancel_after_arming_sends_no_run_and_no_cancel_byte(counter, tmp_path):
    built, timeline = counter
    plan = plan_capture(timeline, request(300), analyzer_config())
    token = CancellationToken()
    model = _watched(timeline, plan, token.cancel)  # the operator cancels right after the analyzer was armed
    result, stpg, _ = run(built, timeline, plan, tmp_path / "o", model=model, cancel=token)
    assert c.CMD_RUN not in stpg.commands
    # the cancel seals the exchange (SDK round 2): a crossing reply is kept as unadmitted bytes, never admitted, and
    # the accessory stays CANCELLED because it exited normally after the host's EOF
    assert result["run"]["outcome"] == "CANCELLED" and result["external"]["cancel_requested"] is True
    assert result["external"]["outcome"] == "CANCELLED" and result["external"]["returncode"] == 0
    assert result["capture"]["outcome"] == "HOST_STOPPED" and result["capture"]["arming"]["armed_segment"] == 0
    # the contract permits only the once-close: the analyzer may stay armed, and the record says so
    assert b"\xff" not in model.writes and not model.cancelled and model.closed == 1
    assert any("no cancel byte" in error for error in result["capture"]["errors"])
    assert accessory_exit(result["result"]) == 130


class _TerminalError(SimulatedStpg):
    def _handle(self, command):
        if command.cmd == c.CMD_GET_STATUS and self.last.state == c.STATE_RUNNING:
            self._queue(replace(self.last, seq=command.seq, rsp=c.RSP_STATUS, state=c.STATE_ERROR,
                                error_code=5, run_progress=1))
            return
        super()._handle(command)


def test_terminal_error_skips_the_observer_and_is_not_a_success(counter, tmp_path):
    built, timeline = counter
    plan = plan_capture(timeline, request(300), analyzer_config())
    samples = synthesize(timeline, plan).samples
    model = ModelAnalyzer(PICO2_TURBO_LIMITS, provider=lambda req: samples)
    result, _, _ = run(built, timeline, plan, tmp_path / "o", model=model,
                       stpg=_TerminalError(on_run=lambda index: model.fire()))
    assert result["run"]["outcome"] == "DEVICE_ERROR"
    assert hook_events(result) == [("before_run", 0, "COMPLETED")]  # ERROR never reaches after_terminal
    assert result["capture"]["arming"]["target_terminal"] is None and result["capture"]["outcome"] == "HOST_STOPPED"
    assert result["external"]["outcome"] == "INCOMPLETE" and accessory_exit(result["result"]) == 2


# ---- canonical plan re-derivation at open --------------------------------------------------------------------

MUTATIONS = {
    "capture_channels": lambda p: p["capture_channels"].append(max(p["capture_channels"]) + 1),
    "channels": lambda p: p["channels"].pop(),
    "window": lambda p: p["window"].update(first_vector=p["window"]["first_vector"] + 1),
    "target": lambda p: p["target"].update(vector=p["target"]["vector"] + 1),
    "pre_samples": lambda p: p["samples"].update(pre=p["samples"]["pre"] + 1),
    "post_samples": lambda p: p["samples"].update(post=p["samples"]["post"] - 1),
    "sample_rate": lambda p: p.update(sample_rate_hz=p["sample_rate_hz"] // 2,
                                      sample_period_ps=p["sample_period_ps"] * 2, divider=p["divider"] * 2),
    "trigger_type": lambda p: p["trigger"].update(type="level" if p["trigger"]["type"] == "pattern" else "pattern"),
    "trigger_base": lambda p: p["trigger"].update(base_channel=p["trigger"]["base_channel"] + 1),
    "trigger_count": lambda p: p["trigger"].update(count=p["trigger"]["count"] + 1),
    "trigger_value": lambda p: p["trigger"].update(value=p["trigger"]["value"] ^ 1),
    "trigger_inverted": lambda p: p["trigger"].update(inverted=not p["trigger"]["inverted"]),
    "pretrigger_policy": lambda p: p["request"].update(pretrigger_policy="firmware"),
    "limits": lambda p: p["analyzer"]["limits"].update(buffer_bytes=p["analyzer"]["limits"]["buffer_bytes"] // 2),
    "unknown_key": lambda p: p.update(extra=True),
    "float_for_int": lambda p: p["samples"].update(total=float(p["samples"]["total"])),
    "bool_for_int": lambda p: p.update(schema_version=True),
    "status": lambda p: p.update(status="UNSUPPORTED"),
}


@pytest.mark.parametrize("name", sorted(MUTATIONS))
def test_any_changed_plan_field_rejects_before_device_io(counter, tmp_path, name):
    built, timeline = counter
    plan = deepcopy(plan_capture(timeline, request(300), analyzer_config()))
    MUTATIONS[name](plan)
    result, stpg, model = run(built, timeline, plan, tmp_path / "o", model=ModelAnalyzer(PICO2_TURBO_LIMITS))
    # the core checked only the STPG port metadata; the accessory refused the plan before opening anything
    assert result["external"]["outcome"] == "REJECT" and result["run"] is None and result["capture"] is None
    assert stpg.commands == [] and model.writes == [] and model.closed == 0
    errors = " ".join(result["rejected"]["errors"])
    assert "PlanError" in errors
    if name not in ("status", "bool_for_int"):
        assert "re-derivation" in errors
    with pytest.raises(PlanError):  # mock/import apply the same check
        offline_capture(timeline, plan, synthesize(timeline, plan_capture(timeline, request(300), analyzer_config())),
                        output_dir=tmp_path / "offline", mode="mock")
    assert not (tmp_path / "offline").exists()


def test_a_plan_for_other_analyzer_limits_is_rejected(counter, tmp_path):
    built, timeline = counter
    plan = plan_capture(timeline, request(300), analyzer_config())  # TURBO build limits
    slower = analyzer_config(limits=PICO2_LIMITS.to_dict())
    result, stpg, model = run(built, timeline, plan, tmp_path / "o", analyzer=slower, model=ModelAnalyzer(PICO2_LIMITS))
    assert result["external"]["outcome"] == "REJECT" and "analyzer.limits" in " ".join(result["rejected"]["errors"])
    assert stpg.commands == [] and model.writes == []


def test_the_host_segment_must_be_the_plan_segment(segmented, tmp_path):
    built, timeline = segmented
    plan = plan_capture(timeline, request(timeline.segments[1].global_start + 300), analyzer_config())
    result, stpg, model = run(built, timeline, plan, tmp_path / "o", segment=0, model=ModelAnalyzer(PICO2_TURBO_LIMITS))
    assert result["external"]["outcome"] == "REJECT" and model.writes == [] and stpg.commands == []
    assert "the capture plan targets 1" in " ".join(result["rejected"]["errors"])


# ---- outcome / exit policy -------------------------------------------------------------------------

ADOPTED = {"outcome": "CAPTURED", "record": "FINAL", "result_persisted": True,
           "alignment": {"status": "ALIGNED", "confidence": "normal", "coverage": "FULL", "offset_ps": 0}}


def _capture(**change):
    value = deepcopy(ADOPTED)
    alignment = change.pop("alignment", None)
    value.update(change)
    if alignment:
        value["alignment"].update(alignment)
    return value


@pytest.mark.parametrize("run_result,capture,device,code", [
    ({"outcome": "COMPLETED", "comparison": "MATCH"}, _capture(), True, 0),
    ({"outcome": "COMPLETED", "comparison": "NOT_APPLICABLE"}, _capture(), True, 0),
    ({"outcome": "COMPLETED", "comparison": "MISMATCH"}, _capture(), True, 3),
    ({"outcome": "COMPLETED", "comparison": "MISMATCH"}, _capture(alignment={"status": "UNALIGNED"}), True, 4),
    ({"outcome": "COMPLETED", "comparison": "MATCH"}, _capture(alignment={"confidence": "low"}), True, 4),
    ({"outcome": "COMPLETED", "comparison": "MATCH"}, _capture(alignment={"coverage": "NONE"}), True, 4),
    ({"outcome": "DEVICE_ERROR", "comparison": "UNOBSERVED"}, _capture(), True, 2),
    (None, _capture(outcome="ANALYZER_ERROR"), True, 2),
    ({"outcome": "COMPLETED"}, _capture(outcome="NO_DATA"), True, 2),
    ({"outcome": "COMPLETED"}, _capture(outcome="NO_RUN"), True, 2),
    ({"outcome": "COMPLETED"}, _capture(outcome="CLEANUP_ERROR"), True, 2),
    ({"outcome": "COMPLETED"}, _capture(outcome="PERSIST_ERROR", result_persisted=False), True, 2),
    ({"outcome": "COMPLETED"}, _capture(record="CHECKPOINT"), True, 2),
    ({"outcome": "CANCELLED"}, _capture(), True, 130),
    ({"outcome": "COMPLETED"}, _capture(outcome="CANCELLED"), True, 130),
    ({"outcome": "DEVICE_ERROR"}, _capture(outcome="CANCELLED"), True, 130),
    (None, _capture(outcome="MOCK"), False, 0),
    (None, _capture(outcome="IMPORTED", alignment={"status": "UNALIGNED"}), False, 4),
    (None, _capture(outcome="IMPORTED", result_persisted=False), False, 2),
])
def test_exit_code_precedence(run_result, capture, device, code):
    assert capture_exit(run_result, capture, device=device)[0] == code


def test_cli_mock_and_import_exit_codes(counter, tmp_path, capsys):
    built, timeline = counter
    la = tmp_path / "la.json"
    la.write_text(json.dumps(analyzer_config().to_dict()))
    plan_file = tmp_path / "plan.json"
    common = [str(built.bundle_dir), "--manifest-sha256", built.manifest_sha256]
    assert capture_main(["plan", *common, "--analyzer", str(la), "--vector", "300", "-o", str(plan_file)]) == 0
    capsys.readouterr()
    assert capture_main(["mock", *common, "--plan", str(plan_file), "--output-dir", str(tmp_path / "m1")]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["exit"]["code"] == 0 and report["mode"] == "mock" and report["timing"]["adopted"]
    # an early trigger: kept, not adopted -> 4
    assert capture_main(["mock", *common, "--plan", str(plan_file), "--output-dir", str(tmp_path / "m2"),
                         "--offset-ps", str(-8 * 400_000)]) == 4
    report = json.loads(capsys.readouterr().out)
    assert report["alignment_status"] == "UNALIGNED" and not report["timing"]["adopted"]
    # a plan edited after planning -> rejected before anything is written
    edited = read_json(plan_file)
    edited["trigger"]["value"] ^= 1
    plan_file.write_text(json.dumps(edited))
    assert capture_main(["mock", *common, "--plan", str(plan_file), "--output-dir", str(tmp_path / "m3")]) == 2
    assert "re-derivation" in capsys.readouterr().out and not (tmp_path / "m3").exists()


# ---- cleanup / persistence fail closed -----------------------------------------------------------------

def test_no_data_closes_the_analyzer_once_without_a_cancel_byte(counter, tmp_path, short_wait):
    built, timeline = counter
    plan = plan_capture(timeline, request(300), analyzer_config())
    short_wait(0.2)
    model = ModelAnalyzer(PICO2_TURBO_LIMITS)  # never fires: no data
    result, stpg, _ = run(built, timeline, plan, tmp_path / "o", model=model, stpg=SimulatedStpg())
    capture = result["capture"]
    # collecting is part of the after-terminal hook: its failure stops the core invocation after the DONE
    assert capture["outcome"] == "NO_DATA" and result["run"]["outcome"] == "REJECT"
    assert result["run"]["completed_segments"] == 1 and c.CMD_RUN in stpg.commands
    assert result["external"]["outcome"] == "REJECT" and accessory_exit(result["result"]) == 2
    assert b"\xff" not in model.writes and not model.cancelled
    session = capture["analyzer"]["session"]
    assert not session["cancel_sent"] and session["armed"]
    assert session["close_attempts"] == session["close_successes"] == 1 and session["lease_released"]
    assert any("no cancel byte" in error for error in capture["errors"])


def test_a_completed_capture_in_segment_0_lets_segment_1_run(segmented, tmp_path):
    # SDK round 2 checks the child again before every later RUN; a normal accessory stays silent after COMPLETE
    built, timeline = segmented
    plan = plan_capture(timeline, request(300), analyzer_config())
    assert plan["bundle"]["segment"] == 0
    result, stpg, model = run(built, timeline, plan, tmp_path / "o")
    assert result["run"]["outcome"] == "COMPLETED" and result["run"]["completed_segments"] == len(timeline.segments)
    assert len(stpg.run_args) == len(timeline.segments) and len(model.requests) == 1
    assert result["external"]["outcome"] == "COMPLETED" and result["external"]["adopted"] is True
    assert result["capture"]["outcome"] == "CAPTURED" and accessory_exit(result["result"]) == 0


def test_a_failed_collect_blocks_the_next_segment(segmented, tmp_path, short_wait):
    built, timeline = segmented
    plan = plan_capture(timeline, request(300), analyzer_config())  # segment 0 of two
    assert plan["bundle"]["segment"] == 0
    short_wait(0.2)
    model = ModelAnalyzer(PICO2_TURBO_LIMITS)  # never fires: the collect fails after segment 0's DONE/HOLD
    result, stpg, _ = run(built, timeline, plan, tmp_path / "o", model=model, stpg=SimulatedStpg())
    assert result["capture"]["outcome"] == "NO_DATA" and result["external"]["outcome"] == "REJECT"
    assert result["run"]["completed_segments"] == 1 and len(stpg.run_args) == 1  # segment 1 was never run
    assert accessory_exit(result["result"]) == 2


class _CloseFails(ModelAnalyzer):
    def close(self):
        super().close()
        raise OSError("injected analyzer close failure")


def test_close_failure_is_counted_and_never_reported_as_clean(counter, tmp_path):
    built, timeline = counter
    plan = plan_capture(timeline, request(300), analyzer_config())
    samples = synthesize(timeline, plan).samples
    model = _CloseFails(PICO2_TURBO_LIMITS, provider=lambda req: samples)
    result, _, _ = run(built, timeline, plan, tmp_path / "o", model=model)
    capture = result["capture"]
    assert capture["outcome"] == "CLEANUP_ERROR" and not capture["timing"]["adopted"]
    assert result["run"]["completed_segments"] == 1  # the segment's own terminal is kept, not relabelled
    assert result["external"]["outcome"] == "REJECT"  # an error is never a COMPLETED accessory result
    session = capture["analyzer"]["session"]
    assert (session["close_attempts"], session["close_successes"]) == (1, 0) and session["reader_stopped"]
    assert capture["samples"] and (Path(result["capture_dir"]) / "capture.raw.bin").is_file()  # evidence kept
    assert model.closed == 1  # closed once, never retried


def test_final_persistence_failure_leaves_the_pending_checkpoint(counter, tmp_path, monkeypatch):
    built, timeline = counter
    plan = plan_capture(timeline, request(300), analyzer_config())
    original = accessory.write_json

    def failing(path, value):
        if isinstance(value, dict) and value.get("record") == "FINAL":
            raise OSError("injected final record failure")
        original(path, value)

    monkeypatch.setattr(accessory, "write_json", failing)
    result, _, _ = run(built, timeline, plan, tmp_path / "o")
    on_disk = read_json(Path(result["capture_dir"]) / "capture.json")
    assert on_disk["record"] == "CHECKPOINT" and not timing_adoption(on_disk)["adopted"]
    assert result["external"]["outcome"] == "REJECT" and result["external"]["adopted"] is False
    assert not (Path(result["output_dir"]) / "accessory" / "result.json").exists()
    assert accessory_exit(result["result"]) == 2


# ---- analyzer byte evidence ------------------------------------------------------------------------------

def _ident(info=PICO2_TURBO_LIMITS) -> bytes:
    lines = [info.id_line, f"FREQ:{info.max_frequency}", f"BLASTFREQ:{info.blast_frequency}",
             f"BUFFER:{info.buffer_bytes}", f"CHANNELS:{info.channels}"]
    return "".join(f"{line}\r\n" for line in lines).encode()


class Peer:
    """Scripted byte peer: one reply per write, optional capture chunks with delays."""

    timeout = 0.05

    def __init__(self, replies=(), chunks=(), *, write_error_at=None, write_count=None, read_error_after=None,
                 block=None):
        self.replies = list(replies)
        self.chunks = list(chunks)
        self.rx = bytearray()
        self.writes: list[bytes] = []
        self.closed = 0
        self.write_error_at = write_error_at
        self.write_count = write_count
        self.read_error_after = read_error_after
        self.reads = 0
        self.block = block

    def write(self, data):
        self.writes.append(bytes(data))
        if self.write_error_at == len(self.writes) - 1:
            raise OSError("injected write failure")
        if self.replies:
            self.rx.extend(self.replies.pop(0))
        return self.write_count(data) if self.write_count else len(data)

    def read(self, size):
        self.reads += 1
        if self.read_error_after is not None and self.reads > self.read_error_after and not self.rx:
            raise OSError("injected read failure (device gone)")
        if self.block is not None and not self.rx and not self.chunks:
            self.block.wait()  # ignores close: a reader that cannot be stopped
            return b""
        if not self.rx and self.chunks:
            delay, part = self.chunks.pop(0)
            time.sleep(delay)
            return part
        if not self.rx:
            time.sleep(0.005)
            return b""
        part = bytes(self.rx[:size])
        del self.rx[:size]
        return part

    def close(self):
        self.closed += 1


def _session(peer, config=None):
    config = config or analyzer_config()
    session = AnalyzerSession(config, stream_factory=lambda _: peer, port_provider=_ports(config))
    session.open()
    return session


def _small_request():
    return lp.build_request(PICO2_TURBO_LIMITS, channels=[0, 1], frequency=1_000_000, pre_samples=4,
                            post_samples=12, trigger_type=lp.TRIGGER_EDGE, trigger_channel=0)


def test_identify_timeout_keeps_every_consumed_byte(monkeypatch):
    monkeypatch.setattr(la_device, "IDENTIFY_TIMEOUT_S", 0.2)
    partial = _ident()[:37]
    session = _session(Peer([partial]))
    try:
        with pytest.raises(AnalyzerError, match="in time"):
            session.identify()
    finally:
        session.close()
    timeout = next(e for e in session.journal if e["event"] == "line.timeout")
    lines = b"".join(bytes.fromhex(e["hex"]) + b"\n" for e in session.journal if e["event"] == "line")
    assert lines + bytes.fromhex(timeout["pending_hex"]) == partial  # complete lines + the unterminated rest
    assert b"".join(bytes.fromhex(e["hex"]) for e in session.journal if e["event"] == "read") == partial
    assert session.evidence()["line_bytes_consumed"] == len(partial)


def test_malformed_identify_and_oversize_line_are_journalled():
    bad = _ident().replace(b"V6_0", b"V5_9")
    session = _session(Peer([bad]))
    try:
        with pytest.raises(lp.ProtocolError):
            session.identify()
    finally:
        session.close()
    malformed = next(e for e in session.journal if e["event"] == "identify.malformed")
    assert "V5_9" in malformed["lines"][0]
    noisy = _session(Peer([b"A" * (la_device.LINE_MAX_BYTES + 300)]))
    try:
        with pytest.raises(AnalyzerError, match="exceeds"):
            noisy.identify()
    finally:
        noisy.close()
    over = next(e for e in noisy.journal if e["event"] == "line.oversize")
    assert len(bytes.fromhex(over["pending_hex"])) > la_device.LINE_MAX_BYTES


def test_failed_and_short_writes_are_never_resent():
    failing = _session(Peer(write_error_at=0))
    try:
        with pytest.raises(AnalyzerError, match="bytes sent unknown, not resent"):
            failing.identify()
    finally:
        failing.close()
    assert len(failing.stream.writes) == 1 and failing.evidence()["unknown_bytes_sent"]
    short = _session(Peer(write_count=lambda data: len(data) - 1))
    try:
        with pytest.raises(AnalyzerError, match="short write"):
            short.identify()
    finally:
        short.close()
    assert len(short.stream.writes) == 1
    end = next(e for e in short.journal if e["event"] == "write.end")
    assert end["sent"] == len(lp.identify_frame()) - 1


def test_delayed_chunked_capture_completes_with_bounded_read_records(monkeypatch):
    monkeypatch.setattr(la_device, "READ_EVENTS_MAX", 3)
    req = _small_request()
    data = capture_bytes(req, [i % 4 for i in range(req.total_samples)])
    chunks = [(0.01, data[i:i + 5]) for i in range(0, len(data), 5)]
    session = _session(Peer([_ident(), b"CAPTURE_STARTED\r\n"], chunks))
    try:
        session.identify()
        session.arm(req)
        assert session.wait(5.0) == data
    finally:
        session.close()
    reads = [e for e in session.journal if e["event"] == "read" and e["phase"] == "capture"]
    assert len(reads) == 3 and session.read_events_dropped == len(chunks) - 3
    received = next(e for e in session.journal if e["event"] == "capture.received")
    assert received["size"] == len(data)
    assert session.evidence()["reader_stopped"] is True and session.evidence()["lease_released"] is True


def test_oversize_capture_read_is_an_error_with_the_bytes_kept():
    req = _small_request()
    data = capture_bytes(req, [0] * req.total_samples)
    session = _session(Peer([_ident(), b"CAPTURE_STARTED\r\n"], [(0.0, data + b"JUNK")]))
    try:
        session.identify()
        session.arm(req)
        with pytest.raises(AnalyzerError, match="for a"):
            session.wait(2.0)
    finally:
        session.close()
    assert session.partial() == data
    assert any(e["event"] == "read.oversize" for e in session.journal)


def test_reader_error_and_short_capture_keep_partial_bytes(counter, tmp_path, short_wait):
    built, timeline = counter
    plan = plan_capture(timeline, request(300), analyzer_config())
    short_wait(0.3)
    short = ModelAnalyzer(PICO2_TURBO_LIMITS, provider=lambda req: [0] * req.total_samples, truncate_capture=101)
    result, _, _ = run(built, timeline, plan, tmp_path / "short", model=short)
    capture = result["capture"]
    assert capture["outcome"] == "NO_DATA" and capture["partial"]["size"] == 101
    kept = (Path(result["capture_dir"]) / "capture.partial.bin").read_bytes()
    req = short.requests[-1]
    assert kept == capture_bytes(req, [0] * req.total_samples)[:101]

    class ReadFails(ModelAnalyzer):
        def read(self, size):
            if self.capturing is False and self.requests and not self.rx:
                raise OSError("injected read failure (device gone)")
            return super().read(size)

    short_wait(1.0)
    failing = ReadFails(PICO2_TURBO_LIMITS, provider=lambda req: [0] * req.total_samples, truncate_capture=50)
    result, _, _ = run(built, timeline, plan, tmp_path / "gone", model=failing)
    capture = result["capture"]
    assert capture["outcome"] == "ANALYZER_ERROR" and "read failed" in " ".join(capture["errors"])
    assert capture["partial"]["size"] == 50
    assert any(e["event"] == "read.error" for e in capture["journal"])


def test_malformed_capture_trailer_keeps_the_raw_bytes(counter, tmp_path):
    built, timeline = counter
    plan = plan_capture(timeline, request(300), analyzer_config())

    class BadTrailer(ModelAnalyzer):
        def _emit(self):
            super()._emit()
            self.rx[-1] = 2  # announces burst timestamps that never follow: malformed trailer

    model = BadTrailer(PICO2_TURBO_LIMITS, provider=lambda req: [0] * req.total_samples)
    result, _, _ = run(built, timeline, plan, tmp_path / "o", model=model)
    capture = result["capture"]
    assert capture["outcome"] == "ANALYZER_ERROR" and result["external"]["outcome"] == "REJECT"
    raw = (Path(result["capture_dir"]) / "capture.raw.bin").read_bytes()
    assert raw[-1] == 2 and capture["samples"] is None


def test_cancellation_while_collecting_returns_promptly(counter, tmp_path, short_wait):
    built, timeline = counter
    plan = plan_capture(timeline, request(300), analyzer_config())
    short_wait(30.0)
    token = CancellationToken()
    model = ModelAnalyzer(PICO2_TURBO_LIMITS)  # never fires
    stpg = SimulatedStpg(on_run=lambda index: threading.Timer(0.3, token.cancel).start())  # cancel after RUN
    started = time.monotonic()
    result, _, _ = run(built, timeline, plan, tmp_path / "o", model=model, stpg=stpg, cancel=token, timeout=20.0)
    assert time.monotonic() - started < 10
    assert result["external"]["outcome"] == "CANCELLED" and accessory_exit(result["result"]) == 130
    assert b"\xff" not in model.writes and model.closed == 1


def test_a_reader_that_cannot_be_stopped_keeps_the_lease(monkeypatch):
    monkeypatch.setattr(la_device, "READER_JOIN_S", 0.05)
    release = threading.Event()
    config = analyzer_config()
    session = _session(Peer([_ident(), b"CAPTURE_STARTED\r\n"], block=release), config)
    session.identify()
    session.arm(_small_request())
    try:
        with pytest.raises(AnalyzerError, match="did not stop"):
            session.close()
        evidence = session.evidence()
        assert evidence["reader_stopped"] is False and evidence["lease_released"] is False
        assert evidence["close_attempts"] == evidence["close_successes"] == 1
        with pytest.raises(RunnerError, match="already leased"):
            DeviceLease(config).__enter__()
    finally:
        release.set()
        session._reader.join(2.0)
        session.lease.__exit__(None, None, None)


# ---- leases ---------------------------------------------------------------------------------------------

def test_one_lease_per_physical_identity_across_roles(counter, tmp_path):
    built, _ = counter
    same = analyzer_config(port="OTHER-PORT", vid=SIMULATED_DEVICE.vid, pid=SIMULATED_DEVICE.pid,
                           serial_number=SIMULATED_DEVICE.serial_number)
    assert same.lease_key == physical_identity_key(SIMULATED_DEVICE) == SIMULATED_DEVICE.lease_key
    touched = []
    with DeviceLease(SIMULATED_DEVICE):
        session = AnalyzerSession(same, stream_factory=lambda _: touched.append("open"),
                                  port_provider=lambda: touched.append("enum") or [])
        with pytest.raises(RunnerError, match="already leased"):
            session.open()
    assert touched == [] and session.lease is None
    holder = AnalyzerSession(same, stream_factory=lambda _: ModelAnalyzer(), port_provider=_ports(same)).open()
    try:
        report = run_bundle(built.bundle_dir, manifest_sha256=built.manifest_sha256, device=SIMULATED_DEVICE,
                            output_dir=tmp_path / "r", confirm_run=True,
                            stream_factory=lambda _: touched.append("stpg-open"), port_provider=simulated_ports)
    finally:
        holder.close()
    assert report["outcome"] == "REJECT" and touched == [] and "already leased" in report["errors"][-1]


# ---- simulated STPG: firmware RUN-argument checks ------------------------------------------------------------

@pytest.mark.parametrize("segment,change,code", [
    (1, lambda a: (a[0], a[1] ^ 1, a[2], a[3]), c.ERR_HOLD_STATE_MISMATCH),
    (1, lambda a: (a[0], a[1], a[2] ^ 1, a[3]), c.ERR_HOLD_STATE_MISMATCH),
    (0, lambda a: (a[0] | c.RUN_FLAG_PRESERVE_GPIO_STATE, 1, 0, a[3]), c.ERR_HOLD_STATE_MISMATCH),
    (0, lambda a: (a[0], 1, 0, a[3]), c.ERR_UNSUPPORTED),
    (0, lambda a: (a[0] | c.RUN_FLAG_MEASURE_FAMILY, a[1], a[2], a[3]), c.ERR_UNSUPPORTED),
    (1, lambda a: (a[0], a[1], a[2], 7), c.ERR_UNSUPPORTED),
])
def test_simulator_rejects_wrong_run_arguments(segmented, tmp_path, monkeypatch, segment, change, code):
    # The wrong arguments are put on the wire to the simulator: the executor opens a port only
    # with its own sealed execution snapshot, so a replaced prepare_run result is refused before any open.
    built, _ = segmented

    class WrongRun(SimulatedStpg):
        def _handle(self, command):
            if command.cmd == c.CMD_RUN and len(self.run_args) == segment:
                command = replace(command, **dict(zip(("arg0", "arg1", "arg2", "arg3"), change(
                    (command.arg0, command.arg1, command.arg2, command.arg3)))))
            super()._handle(command)

    stpg = WrongRun()
    report = run_bundle(built.bundle_dir, manifest_sha256=built.manifest_sha256, device=SIMULATED_DEVICE,
                        output_dir=tmp_path / "r", confirm_run=True, stream_factory=lambda _: stpg,
                        port_provider=simulated_ports, sleep=lambda s: None)
    assert report["outcome"] in ("DEVICE_ERROR", "REJECT") and report["completed_segments"] == segment
    assert stpg.rejected_runs == [code] and stpg.runs == segment


# ---- marker provenance -------------------------------------------------------------------------------------

@pytest.fixture(scope="module")
def marker(counter, tmp_path_factory):
    built, _ = counter
    analyzer = analyzer_config(marker_gpio=18)
    derived = derive_marker_project(Path(built.bundle_dir).parent.parent / "project.stpg-project.json",
                                    bundle_dir=built.bundle_dir, manifest_sha256=built.manifest_sha256,
                                    vector=900, analyzer=analyzer, directory=tmp_path_factory.mktemp("m") / "derived")
    bundle = build_project(Path(derived["project_path"]))
    return analyzer, derived, bundle


def test_marker_provenance_is_bound_to_content_not_folder(counter, marker, tmp_path):
    built, timeline = counter
    analyzer, derived, bundle = marker
    record = derived["derivation"]
    moved = tmp_path / "elsewhere" / "copied bundle"
    shutil.copytree(bundle.bundle_dir, moved)
    relocated = load_bundle_timeline(moved, manifest_sha256=bundle.manifest_sha256)
    assert check_marker_bundle(relocated, record, analyzer=analyzer)["verification"]["status"] == "EQUIVALENT_TIMING"
    with pytest.raises(MarkerError, match="does not keep the source timing"):
        check_marker_bundle(relocated, {**record, "target": {**record["target"], "vector": record["target"]["vector"] + 1}})
    with pytest.raises(MarkerError, match="does not belong to this bundle"):
        check_marker_bundle(relocated, {**record, "derived": {**record["derived"], "stil_sha256": "0" * 64}})
    with pytest.raises(MarkerError, match="no derivation record"):
        check_marker_bundle(relocated, None)
    original = Path(record["source"]["bundle_dir"])
    shutil.move(str(original), str(tmp_path / "source moved away"))
    try:
        with pytest.raises(MarkerError, match="cannot be re-verified"):
            check_marker_bundle(relocated, record)
    finally:
        shutil.move(str(tmp_path / "source moved away"), str(original))
    with pytest.raises(MarkerError, match="without a marker signal"):
        check_marker_bundle(timeline, record)


def test_the_accessory_requires_and_reverifies_the_marker_derivation(marker, tmp_path):
    analyzer, derived, bundle = marker
    marker_timeline = load_bundle_timeline(bundle.bundle_dir, manifest_sha256=bundle.manifest_sha256)
    plan = plan_capture(marker_timeline, request(900), analyzer)
    assert plan["trigger"]["strategy"] == "marker"
    result, stpg, model = run(bundle, marker_timeline, plan, tmp_path / "o1", analyzer=analyzer,
                              model=ModelAnalyzer(analyzer.limits))
    assert result["external"]["outcome"] == "REJECT" and "no derivation record" in " ".join(result["rejected"]["errors"])
    assert stpg.commands == [] and model.writes == []
    other_default = analyzer_config(marker_gpio=17)  # the binding's marker_gpio is only a default
    assert check_marker_bundle(marker_timeline, derived["derivation"], analyzer=other_default)["marker_gpio"] == 18
    unwired = analyzer_config(wiring={str(gpio): channel for gpio, channel in ((2, 1), (3, 2))})
    with pytest.raises(MarkerError, match="connects to no analyzer input"):
        check_marker_bundle(marker_timeline, derived["derivation"], analyzer=unwired)
    result, _, _ = run(bundle, marker_timeline, plan, tmp_path / "o2", analyzer=analyzer,
                       derivation=derived["derivation"])
    capture = result["capture"]
    assert capture["outcome"] == "CAPTURED" and capture["timing"]["adopted"]
    assert capture["marker"]["verification"]["status"] == "EQUIVALENT_TIMING"
