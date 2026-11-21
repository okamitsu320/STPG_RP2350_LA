"""Analyzer Identify through the actual CLI/API; literal wire oracle, no devices."""
from types import SimpleNamespace
from pathlib import Path

import json
import pytest

from stpg_capture import cli, la_device
from stpg_capture import identify as identify_module
from stpg_capture.config import AnalyzerConfig
from stpg_runner import CancellationToken
from stpg_runner.artifacts import RunnerError, read_json, write_json
from stpg_runner.device import DeviceLease

pytestmark = pytest.mark.stpg_host_only
REPLY = b"LOGIC_ANALYZER_PICO_2_V6_0\nFREQ:200000000\nBLASTFREQ:400000000\nBUFFER:393216\nCHANNELS:24\n"
REQUEST = b"\x55\xaa\x00\xaa\x55"


def binding():
    return AnalyzerConfig.from_dict({
        "kind": "stpg_capture_analyzer", "schema_version": 1,
        "port": "MOCK-LA-IDENTIFY", "vid": 0x2e8a, "pid": 10,
        "serial_number": "SYNTHETIC-LA-IDENTIFY",
        "limits": {"id_line": "LOGIC_ANALYZER_PICO_2_V6_0", "max_frequency": 200000000,
                   "blast_frequency": 400000000, "buffer_bytes": 393216, "channels": 24},
        "wiring": "same-gpio"})


def ports(config):
    return [SimpleNamespace(device=config.port, vid=config.vid, pid=config.pid, serial_number=config.serial_number)]


class Peer:
    def __init__(self, reply=REPLY, *, close_error=False):
        self.reply = reply
        self.rx = bytearray()
        self.writes, self.closed = [], 0
        self.now = 0.0
        self.close_error = close_error

    def clock(self):
        return self.now

    def write(self, data):
        self.writes.append(bytes(data))
        assert data == REQUEST, "unexpected capture/cancel or other wire command"
        self.rx.extend(self.reply)
        return len(data)

    def read(self, count):
        self.now += 0.1 if self.rx else 2.0
        part = bytes(self.rx[:count]); del self.rx[:count]
        return part

    def close(self):
        self.closed += 1
        if self.close_error:
            raise OSError("synthetic close failure")


@pytest.fixture(autouse=True)
def no_hardware(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Identify test attempted real metadata/open")
    monkeypatch.setattr(la_device, "open_analyzer_serial", forbidden)
    monkeypatch.setattr(la_device, "enumerate_ports", forbidden)


def cli_call(tmp_path, monkeypatch, peer):
    config = binding()
    path = tmp_path / "binding.json"; write_json(path, config.to_dict())
    real = la_device.AnalyzerSession
    def injected(config, **kwargs):
        kwargs.update(stream_factory=lambda _: peer, port_provider=lambda: ports(config), clock=peer.clock)
        return real(config, **kwargs)
    monkeypatch.setattr(la_device, "AnalyzerSession", injected)
    return cli.main(["identify", "--analyzer", str(path), "--output-dir", str(tmp_path / "identify"),
                     "--confirm-identify"])


def test_cli_success_keeps_one_identify_exchange_in_a_saved_result(tmp_path, monkeypatch, capsys):
    peer = Peer()
    assert cli_call(tmp_path, monkeypatch, peer) == 0
    report = json.loads(capsys.readouterr().out)
    assert peer.writes == [REQUEST] and peer.closed == 1
    assert report["status"] == "IDENTIFIED" and report["result_persisted"] is True
    saved = json.loads(Path(report["result_file"]).read_text())
    assert "transport" not in report and "config" not in report  # raw/machine evidence is local-only
    assert saved["transport"]["journal"] and saved["transport"]["counters"]["writes_completed"] == 1


def test_cli_close_failure_keeps_the_reply_and_once_close_counters(tmp_path, monkeypatch, capsys):
    peer = Peer(close_error=True)
    assert cli_call(tmp_path, monkeypatch, peer) == 2
    report = json.loads(capsys.readouterr().out)
    assert peer.writes == [REQUEST] and peer.closed == 1
    assert report["status"] == "REJECT" and report["analyzer"] == binding().limits.to_dict()
    saved = json.loads(Path(report["result_file"]).read_text())
    assert saved["transport"]["counters"]["close_attempts"] == 1
    assert saved["transport"]["counters"]["close_successes"] == 0


def test_cli_timeout_keeps_the_partial_consumed_reply(tmp_path, monkeypatch, capsys):
    peer = Peer(reply=b"LOGIC_ANALYZER_PICO_2_")
    assert cli_call(tmp_path, monkeypatch, peer) == 2
    report = json.loads(capsys.readouterr().out)
    assert peer.writes == [REQUEST] and peer.closed == 1
    saved = json.loads(Path(report["result_file"]).read_text())
    assert report["status"] == "REJECT" and saved["transport"]["pending_hex"] == peer.reply.hex()


def identify(tmp_path, peer=None, **overrides):
    peer = Peer() if peer is None else peer
    config = binding()
    options = dict(output_dir=tmp_path / "identify", confirm_identify=True, clock=peer.clock,
                   stream_factory=lambda _: peer, port_provider=lambda: ports(config))
    options.update(overrides)
    return identify_module.identify_analyzer(config, **options), peer


def consumed(result):
    return b"".join(bytes.fromhex(row["hex"]) for row in result["transport"]["journal"]
                    if row["event"] == "read" and row["phase"] == "line")


def test_public_api_positive_control_exact_saved_report_and_no_capture(tmp_path):
    import stpg_capture
    assert stpg_capture.identify_analyzer is identify_module.identify_analyzer
    result, peer = identify(tmp_path)
    assert result["status"] == "IDENTIFIED" and result["matches_binding"] is True
    assert result["result_persisted"] and result["analyzer"] == binding().limits.to_dict()
    assert result == read_json(Path(result["result_file"])) and consumed(result) == REPLY
    counters = result["transport"]["counters"]
    assert peer.writes == [REQUEST] and peer.closed == counters["close_attempts"] == counters["close_successes"] == 1
    assert counters["reader_started"] is False and counters["cancel_sent"] is False and counters["request_sent"] is False
    assert result["capture_commands_written"] == 0 and result["electrical_validation"] is False
    events = result["transport"]["journal"]
    assert [row["time_s"] for row in events] == sorted(row["time_s"] for row in events)


@pytest.mark.parametrize("prefix", range(len(REPLY)))
def test_every_truncated_reply_retains_all_consumed_bytes_without_retry(tmp_path, prefix):
    result, peer = identify(tmp_path, Peer(REPLY[:prefix]))
    assert result["status"] == "REJECT" and result["result_persisted"]
    assert consumed(result) == REPLY[:prefix] and peer.writes == [REQUEST] and peer.closed == 1
    assert result["transport"]["pending_hex"] == REPLY[:prefix].split(b"\n")[-1].hex()
    assert any(row["event"] == "line.timeout" for row in result["transport"]["journal"])


@pytest.mark.parametrize("reply", [REPLY.replace(b"FREQ:200000000", b"FREQ:100000000"),
    REPLY.replace(b"LOGIC_ANALYZER_PICO_2_V6_0", b"FOREIGN_DEVICE"),
    REPLY.replace(b"FREQ:200000000", b"FREQ:not-a-number"), REPLY.replace(b"BUFFER:", b"\xffBUFFER:"),
    REPLY + b"unexpected\n", REPLY + b"partial"])
def test_mismatch_malformed_and_trailing_bytes_reject_with_the_prefix(tmp_path, reply):
    result, peer = identify(tmp_path, Peer(reply))
    assert result["status"] == "REJECT" and consumed(result) == reply and result["errors"]
    assert peer.writes == [REQUEST] and peer.closed == 1
    if b"FREQ:100000000" in reply:
        assert result["reported"]["max_frequency"] == 100000000 and result["matches_binding"] is False


def test_late_complete_reply_is_kept_but_never_accepted(tmp_path):
    class Late(Peer):
        def read(self, count):
            data = super().read(count)
            self.now += 11
            return data
    result, peer = identify(tmp_path, Late())
    assert result["status"] == "REJECT" and consumed(result) == REPLY
    assert result["analyzer"] is None and peer.writes == [REQUEST]


@pytest.mark.parametrize("fault", ["write", "short", "read", "open", "close"])
def test_failures_never_hide_the_prefix_or_send_cleanup(tmp_path, fault):
    class Fault(Peer):
        def write(self, data):
            self.writes.append(bytes(data))
            if fault == "write":
                raise OSError("synthetic unknown write")
            if fault == "short":
                return len(data) - 1
            self.rx.extend(self.reply)
            return len(data)
        def read(self, count):
            if fault == "read":
                raise ValueError("synthetic driver read error")
            return super().read(count)
    peer = Fault(close_error=fault == "close")
    def factory(_):
        if fault == "open":
            raise OSError("synthetic open failure")
        return peer
    result, _ = identify(tmp_path, peer, stream_factory=factory)
    assert result["status"] == "REJECT" and result["errors"] and result["result_persisted"]
    assert peer.writes == ([] if fault == "open" else [REQUEST])
    assert peer.closed == (0 if fault == "open" else 1)
    assert not result["transport"]["counters"]["cancel_sent"]
    if fault == "write":
        assert result["transport"]["counters"]["unknown_bytes_sent"] is True
    if fault == "close":
        assert consumed(result) == REPLY and result["matches_binding"] is True


@pytest.mark.parametrize("phase", ["before", "metadata", "after_open", "read"])
def test_cancellation_issues_no_query_after_its_boundary(tmp_path, phase):
    token, opened, enumerated = CancellationToken(), [], []
    class CancelPeer(Peer):
        def read(self, count):
            data = super().read(count)
            if phase == "read":
                token.cancel()
            return data
    peer = CancelPeer()
    if phase == "before":
        token.cancel()
    def metadata():
        enumerated.append(True)
        if phase == "metadata":
            token.cancel()
        return ports(binding())
    def factory(_):
        opened.append(True)
        if phase == "after_open":
            token.cancel()
        return peer
    result, _ = identify(tmp_path, peer, cancel=token, stream_factory=factory, port_provider=metadata)
    assert result["status"] == "CANCELLED" and result["result_persisted"]
    assert opened == ([] if phase in ("before", "metadata") else [True])
    assert enumerated == ([] if phase == "before" else [True])
    assert peer.writes == ([REQUEST] if phase == "read" else [])
    assert peer.closed == (1 if opened else 0)
    if phase == "read":
        assert consumed(result) == REPLY


@pytest.mark.parametrize("field", ["port", "vid", "pid", "serial_number"])
def test_wrong_metadata_and_active_common_lease_reject_before_open(tmp_path, field):
    rows = ports(binding())
    setattr(rows[0], "device" if field == "port" else field, "WRONG" if field in ("port", "serial_number") else 123)
    result, peer = identify(tmp_path / "metadata", port_provider=lambda: rows)
    assert result["status"] == "REJECT" and not peer.writes and peer.closed == 0
    with DeviceLease(binding()):
        result, peer = identify(tmp_path / "lease")
    assert result["status"] == "REJECT" and not peer.writes and peer.closed == 0


@pytest.mark.parametrize("intent", [False, None, 1, "yes", [], {}])
def test_intent_must_be_exact_and_reject_before_reservation(tmp_path, intent):
    with pytest.raises(RunnerError):
        identify(tmp_path, confirm_identify=intent)
    assert not (tmp_path / "identify").exists()


def test_no_overwrite_or_endpoint_fallback(tmp_path):
    result, peer = identify(tmp_path, port_provider=lambda: [])
    assert result["status"] == "REJECT" and not peer.writes and peer.closed == 0
    original = Path(result["result_file"]).read_bytes()
    with pytest.raises(FileExistsError):
        identify(tmp_path)
    assert Path(result["result_file"]).read_bytes() == original


@pytest.mark.parametrize("initial", [True, False])
def test_checkpoint_failure_has_no_contact_or_a_truthful_in_memory_prefix(tmp_path, monkeypatch, initial):
    real, calls = identify_module.write_json, []
    def failing(path, value):
        calls.append(path)
        if initial or len(calls) == 2:
            raise OSError("synthetic checkpoint failure")
        return real(path, value)
    monkeypatch.setattr(identify_module, "write_json", failing)
    peer = Peer()
    if initial:
        with pytest.raises(OSError):
            identify(tmp_path, peer)
        assert not peer.writes and peer.closed == 0
    else:
        result, peer = identify(tmp_path, peer)
        assert result["status"] == "REJECT" and not result["result_persisted"] and consumed(result) == REPLY
        assert peer.closed == 1 and read_json(Path(result["result_file"]))["status"] == "PENDING"


def test_first_failure_close_failure_and_persistence_failure_are_all_retained(tmp_path, monkeypatch):
    real, calls = identify_module.write_json, []
    def failing(path, value):
        calls.append(path)
        if len(calls) == 2:
            raise OSError("synthetic disk failure")
        return real(path, value)
    monkeypatch.setattr(identify_module, "write_json", failing)
    result, peer = identify(tmp_path, Peer(REPLY.replace(b"FREQ:200000000", b"FREQ:100000000"), close_error=True))
    assert result["status"] == "REJECT" and len(result["errors"]) == 3
    assert "binding expects" in result["errors"][0] and "close:" in result["errors"][1]
    assert "checkpoint:" in result["errors"][2] and not result["result_persisted"] and peer.closed == 1


def test_old_unrecorded_cli_or_unconfirmed_contact_is_no_longer_available(tmp_path, capsys):
    path = tmp_path / "binding.json"; write_json(path, binding().to_dict())
    with pytest.raises(SystemExit) as exc:
        cli.main(["identify", "--analyzer", str(path)])
    assert exc.value.code == 2
    capsys.readouterr()
    assert cli.main(["identify", "--analyzer", str(path), "--output-dir", str(tmp_path / "identify")]) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "REJECT" and not (tmp_path / "identify").exists()


@pytest.mark.parametrize("phase", ["open", "read", "write", "close"])
def test_interrupt_retains_evidence_and_cleanup_error_never_masquerades_as_success(tmp_path, phase):
    class Interrupted(Peer):
        def write(self, data):
            if phase == "write":
                self.writes.append(bytes(data))
                raise KeyboardInterrupt("synthetic write interrupt")
            return super().write(data)
        def read(self, count):
            if phase == "read":
                raise KeyboardInterrupt("synthetic read interrupt")
            return super().read(count)
        def close(self):
            super().close()
            if phase == "close":
                raise KeyboardInterrupt("synthetic close interrupt")
    peer = Interrupted()
    def factory(_):
        if phase == "open":
            raise KeyboardInterrupt("synthetic open interrupt")
        return peer
    result, _ = identify(tmp_path, peer, stream_factory=factory)
    assert result["status"] == ("REJECT" if phase == "close" else "CANCELLED")
    assert result["result_persisted"] and peer.closed == (0 if phase == "open" else 1)
    assert peer.writes == ([] if phase == "open" else [REQUEST])
    if phase == "close":
        assert consumed(result) == REPLY


def test_cli_ctrl_c_path_returns_130_with_a_saved_prefix(tmp_path, monkeypatch, capsys):
    class Interrupted(Peer):
        def read(self, count):
            raise KeyboardInterrupt("synthetic Ctrl-C")
    peer = Interrupted()
    assert cli_call(tmp_path, monkeypatch, peer) == 130
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "CANCELLED" and report["result_persisted"] and peer.closed == 1
    saved = read_json(Path(report["result_file"]))
    assert saved["transport"]["counters"]["write_attempts"] == 1 and saved["errors"]


@pytest.mark.parametrize("mutation", ["extra_identify", "ignore_close", "ignore_persistence", "late_reservation"])
def test_actual_function_mutations_are_detected_by_wire_and_evidence_oracles(tmp_path, mutation):
    source = Path(identify_module.__file__).read_text()
    changes = {
        "extra_identify": ("info = session.identify()", "session.identify(); info = session.identify()"),
        "ignore_close": ('report["status"] = "REJECT"', 'report["status"] = "IDENTIFIED"'),
        "ignore_persistence": ('report["status"], report["result_persisted"] = "REJECT", False',
                               'report["status"], report["result_persisted"] = "IDENTIFIED", True'),
        "late_reservation": ('write_json(output / "identify.json", report)  # no metadata/open if reservation fails',
                             'pass  # incorrectly delay the first checkpoint until after contact'),
    }
    old, new = changes[mutation]
    assert source.count(old) == 1
    namespace = {"__name__": "stpg_capture._identify_mutation", "__package__": "stpg_capture"}
    exec(compile(source.replace(old, new), "<injected-host-only-Identify-mutation>", "exec"), namespace)
    original, calls = namespace["write_json"], []
    def checkpoint(path, value):
        calls.append(path)
        if mutation == "ignore_persistence" and len(calls) == 2:
            raise OSError("synthetic final write error")
        return original(path, value)
    namespace["write_json"] = checkpoint
    peer = Peer(close_error=mutation == "ignore_close")
    config = binding()
    def metadata():
        assert read_json(tmp_path / "identify" / "identify.json")["status"] == "PENDING"
        return ports(config)
    result = namespace["identify_analyzer"](config, output_dir=tmp_path / "identify", confirm_identify=True,
                                             stream_factory=lambda _: peer, port_provider=metadata, clock=peer.clock)
    # These are the same production-callsite wire/persistence acceptance rules,
    # not a constant-dictionary comparison or the implementation's own judge.
    with pytest.raises(AssertionError):
        if mutation in ("ignore_close", "ignore_persistence"):
            assert result["status"] == "REJECT"
        else:
            assert result["status"] == "IDENTIFIED" and peer.writes == [REQUEST]
