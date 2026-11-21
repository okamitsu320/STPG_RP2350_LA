"""Additional LA boundary tests, independent of GUI and physical hardware."""
from types import SimpleNamespace

import pytest

from stpg_capture import la_device
from stpg_capture.la_device import AnalyzerSession, AnalyzerError, AnalyzerCancelled
from stpg_capture.config import AnalyzerConfig
from stpg_capture.mock import PICO2_TURBO_LIMITS, capture_bytes
from stpg_capture import la_protocol as lp

pytestmark = pytest.mark.stpg_host_only


def config():
    return AnalyzerConfig.from_dict({"kind": "stpg_capture_analyzer", "schema_version": 1,
                                    "port": "MOCK-LA-BOUNDARY", "vid": 0x2e8a, "pid": 10,
                                    "serial_number": "SYNTHETIC-LA-BOUNDARY", "limits": PICO2_TURBO_LIMITS.to_dict(),
                                    "wiring": "same-gpio"})


IDENTITY = b"LOGIC_ANALYZER_PICO_2_V6_0\nFREQ:200000000\nBLASTFREQ:400000000\nBUFFER:393216\nCHANNELS:24\n"


class Peer:
    def __init__(self, replies, clock=None, advance=0):
        self.replies = list(replies)
        self.rx = bytearray()
        self.writes, self.closed = [], 0
        self.clock, self.advance = clock, advance

    def write(self, data):
        self.writes.append(bytes(data))
        self.rx.extend(self.replies.pop(0))
        return len(data)

    def read(self, size):
        if self.clock is not None:
            self.clock[0] += self.advance
        part = bytes(self.rx[:size]); del self.rx[:size]
        return part

    def close(self):
        self.closed += 1


def session(peer, *, clock=None, cancelled=None):
    binding = config()
    return AnalyzerSession(binding, stream_factory=lambda _: peer,
                           port_provider=lambda: [SimpleNamespace(device=binding.port, vid=binding.vid,
                                                                  pid=binding.pid, serial_number=binding.serial_number)],
                           **({"clock": clock} if clock else {}), cancelled=cancelled)


@pytest.fixture(autouse=True)
def no_hardware(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("capture boundary test attempted real I/O")
    monkeypatch.setattr(la_device, "open_analyzer_serial", forbidden)
    monkeypatch.setattr(la_device, "enumerate_ports", forbidden)


def test_late_complete_identity_is_not_accepted():
    now = [0.0]
    peer = Peer([IDENTITY], clock=now, advance=11)
    device = session(peer, clock=lambda: now[0])
    device.open()
    try:
        with pytest.raises(AnalyzerError, match="in time"):
            device.identify()
    finally:
        device.close()
    assert peer.closed == 1 and len(peer.writes) == 1
    assert any(record.get("hex") == IDENTITY.hex() for record in device.journal)


def test_oversize_capture_carried_with_start_reply_is_not_silently_truncated():
    req = lp.build_request(PICO2_TURBO_LIMITS, channels=[0], frequency=1_000_000,
                           pre_samples=2, post_samples=14, trigger_type=lp.TRIGGER_EDGE, trigger_channel=0)
    data = capture_bytes(req, [0] * 16)
    peer = Peer([IDENTITY, b"CAPTURE_STARTED\n" + data + b"JUNK"])
    device = session(peer)
    device.open()
    try:
        device.identify()
        with pytest.raises(AnalyzerError, match="carried"):
            device.arm(req)
        assert device.partial() == data + b"JUNK"
    finally:
        device.close()
    assert len(peer.writes) == 2 and peer.closed == 1


def test_precancelled_analyzer_does_not_enumerate_or_open():
    peer = Peer([IDENTITY])
    device = session(peer, cancelled=lambda: True)
    try:
        with pytest.raises(AnalyzerCancelled):
            device.open()
    finally:
        device.close()
    assert not peer.writes and peer.closed == 0


def test_cancel_between_open_and_identify_sends_no_query():
    cancelled = [False]
    peer = Peer([IDENTITY])
    device = session(peer, cancelled=lambda: cancelled[0])
    device.open()
    cancelled[0] = True
    try:
        with pytest.raises(AnalyzerCancelled):
            device.identify()
    finally:
        device.close()
    assert not peer.writes and peer.closed == 1


def test_cancel_during_metadata_does_not_open_analyzer():
    cancelled, opened = [False], []
    binding = config()
    def ports():
        cancelled[0] = True
        return [SimpleNamespace(device=binding.port, vid=binding.vid, pid=binding.pid,
                                serial_number=binding.serial_number)]
    device = AnalyzerSession(binding, stream_factory=lambda _: opened.append(True), port_provider=ports,
                             cancelled=lambda: cancelled[0])
    with pytest.raises(AnalyzerCancelled):
        device.open()
    device.close()
    assert not opened and device.lease is None


def test_closed_analyzer_session_cannot_be_reopened():
    peer = Peer([IDENTITY])
    device = session(peer)
    device.open(); device.close()
    with pytest.raises(AnalyzerError, match="once"):
        device.open()
    assert peer.closed == 1 and not peer.writes
