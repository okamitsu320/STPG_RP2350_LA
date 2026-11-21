"""Identify on this tool's page keeps its evidence (host only, in-memory peers, hardware guard on).

Identify opens a real port in production, so it reserves ``identify.json`` under the results folder before any
contact and rewrites it with the exchanged bytes afterwards, even when close or the final write fails.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from stpg_capture import la_device, la_protocol as lp
from stpg_capture import identify as identify_module
from stpg_capture.mock import PICO2_TURBO_LIMITS, ModelAnalyzer
from stpg_capture.ui.service import LaService
from stpg_runner.artifacts import RunnerError
from stpg_runner.device import DeviceLease

from la_support import forbid_hardware
from test_stpg_capture import analyzer_config

pytestmark = pytest.mark.stpg_host_only
REAL_SESSION = la_device.AnalyzerSession


@pytest.fixture(autouse=True)
def _no_hardware(monkeypatch):
    forbid_hardware(monkeypatch)


@pytest.fixture
def svc(tmp_path):
    service = LaService(config_dir=tmp_path / "cfg", results_dir=tmp_path / "runs", device_io=True)
    service.save_analyzer({"name": "la", "config": analyzer_config().to_dict()})
    return service


class Opened:
    """Counts how often the injected analyzer stream was opened."""

    def __init__(self, model):
        self.model, self.count = model, 0

    def __call__(self, _config):
        self.count += 1
        return self.model


def _analyzer(monkeypatch, model, *, cancelled=None) -> Opened:
    real, opened = REAL_SESSION, Opened(model)  # never wrap an earlier injection

    def injected(config, **kwargs):
        if cancelled is not None:
            kwargs["cancelled"] = cancelled
        return real(config, stream_factory=opened, port_provider=lambda: [SimpleNamespace(
            device=config.port, vid=config.vid, pid=config.pid, serial_number=config.serial_number)], **kwargs)

    monkeypatch.setattr(la_device, "AnalyzerSession", injected)
    return opened


def _saved(result) -> dict:
    return json.loads(Path(result["result_file"]).read_text())


def test_analyzer_identify_saves_the_exchange_and_sends_only_the_identify_request(svc, monkeypatch):
    model = ModelAnalyzer(PICO2_TURBO_LIMITS)
    _analyzer(monkeypatch, model)
    result = svc.identify({"name": "la"})
    assert result["status"] == "IDENTIFIED" and result["error"] is None and result["result_persisted"] is True
    assert model.writes == [lp.identify_frame()] and model.closed == 1
    saved = _saved(result)
    assert saved["status"] == "IDENTIFIED" and saved["analyzer"] == PICO2_TURBO_LIMITS.to_dict()
    events = [entry["event"] for entry in saved["transport"]["journal"]]
    assert events[:3] == ["port.validated", "open.begin", "open.end"] and events.count("write.begin") == 1
    assert saved["transport"]["counters"]["close_successes"] == 1


def test_analyzer_identify_mismatch_reports_what_was_read_and_keeps_it(svc, monkeypatch):
    other = lp.AnalyzerInfo(**{**PICO2_TURBO_LIMITS.to_dict(), "max_frequency": 100_000_000})
    model = ModelAnalyzer(other)
    _analyzer(monkeypatch, model)
    result = svc.identify({"name": "la"})
    assert result["status"] == "REJECT" and "binding expects" in result["error"]
    assert result["reported"]["max_frequency"] == 100_000_000 and result["analyzer"] is None
    assert model.writes == [lp.identify_frame()] and _saved(result)["status"] == "REJECT"


def test_analyzer_identify_close_failure_rejects_but_keeps_the_identified_reply(svc, monkeypatch):
    class CloseFails(ModelAnalyzer):
        def close(self):
            super().close()
            raise OSError("injected close failure")

    _analyzer(monkeypatch, CloseFails(PICO2_TURBO_LIMITS))
    result = svc.identify({"name": "la"})
    assert result["status"] == "REJECT" and result["error"].startswith("close: AnalyzerError")
    assert result["analyzer"] == PICO2_TURBO_LIMITS.to_dict()  # the bytes read before the failure are kept
    counters = _saved(result)["transport"]["counters"]
    assert (counters["close_attempts"], counters["close_successes"]) == (1, 0)


def test_analyzer_identify_lease_and_cancellation_never_open_the_port(svc, monkeypatch):
    opened = _analyzer(monkeypatch, ModelAnalyzer(PICO2_TURBO_LIMITS))
    with DeviceLease(analyzer_config()):
        leased = svc.identify({"name": "la"})
    assert leased["status"] == "REJECT" and "lease" in leased["error"] and opened.count == 0
    assert _saved(leased)["status"] == "REJECT"
    opened = _analyzer(monkeypatch, ModelAnalyzer(PICO2_TURBO_LIMITS), cancelled=lambda: True)
    cancelled = svc.identify({"name": "la"})
    assert cancelled["status"] == "CANCELLED" and opened.count == 0 and _saved(cancelled)["status"] == "CANCELLED"


def test_analyzer_identify_needs_its_evidence_reserved_before_contact(svc, monkeypatch):
    opened = _analyzer(monkeypatch, ModelAnalyzer(PICO2_TURBO_LIMITS))

    def broken(path, value):
        raise OSError("disk full")

    monkeypatch.setattr(identify_module, "write_json", broken)
    with pytest.raises(OSError, match="disk full"):
        svc.identify({"name": "la"})
    assert opened.count == 0


def test_analyzer_identify_final_write_failure_is_not_reported_as_saved(svc, monkeypatch):
    model = ModelAnalyzer(PICO2_TURBO_LIMITS)
    _analyzer(monkeypatch, model)
    real, calls = identify_module.write_json, []

    def second_fails(path, value):
        calls.append(path)
        if len(calls) == 2:
            raise OSError("disk full")
        return real(path, value)

    monkeypatch.setattr(identify_module, "write_json", second_fails)
    result = svc.identify({"name": "la"})
    assert result["status"] == "REJECT" and result["result_persisted"] is False
    assert "checkpoint: OSError: disk full" in result["error"] and model.writes == [lp.identify_frame()]
    assert result["transport"]["journal"] and _saved(result)["status"] == "PENDING"  # only the reservation landed


def test_analyzer_identify_is_off_without_device_io(tmp_path):
    off = LaService(config_dir=tmp_path / "cfg", results_dir=tmp_path / "runs", device_io=False)
    off.save_analyzer({"name": "la", "config": analyzer_config().to_dict()})
    with pytest.raises(RunnerError, match="disabled"):
        off.identify({"name": "la"})
    assert not (tmp_path / "runs").exists()


def test_analyzer_identify_records_an_unexpected_failure_with_the_bytes_so_far(svc, monkeypatch):
    class Garbles(ModelAnalyzer):
        def read(self, size):
            raise ValueError("injected driver fault")

    model = Garbles(PICO2_TURBO_LIMITS)
    _analyzer(monkeypatch, model)
    result = svc.identify({"name": "la"})
    assert result["status"] == "REJECT" and "injected driver fault" in result["error"]
    assert model.writes == [lp.identify_frame()] and model.closed == 1  # closed once, nothing resent
    saved = _saved(result)
    assert saved["status"] == "REJECT" and saved["transport"]["counters"]["close_successes"] == 1


def test_the_demo_identify_uses_the_model_and_says_so(tmp_path):
    demo = LaService(config_dir=tmp_path / "cfg", results_dir=tmp_path / "runs", device_io=False, demo=True)
    result = demo.identify({"name": "simulated"})
    assert result["status"] == "IDENTIFIED" and result["simulated"] is True and result["matches_binding"] is True
