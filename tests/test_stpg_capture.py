"""Host-only tests of the cycle-addressed logic-analyzer capture (stpg_capture).

Analyzer traffic uses the byte-level ModelAnalyzer and the STPG side the
simulated firmware peer; no port is opened. A run is the core SDK's run with this
tool as its external accessory (``accessory_harness``). Results of these models
are not analyzer or DUT measurements.
"""

from __future__ import annotations

import base64
import gzip
import json
import re
from pathlib import Path
import struct
from types import SimpleNamespace

import pytest

from stpg import constants as c
from stpg_capture import la_protocol as lp
from stpg_capture.address import Address, AddressError, resolve
from stpg_capture.align import align
from stpg_capture.cli import main as capture_main
from stpg_capture.config import AnalyzerConfig, CaptureConfigError, CaptureRequestSpec
from stpg_capture.la_device import AnalyzerError, AnalyzerSession
from stpg_capture.mock import PICO2_LIMITS, PICO2_TURBO_LIMITS, ModelAnalyzer, synthesize
from stpg_capture.planner import PlanError, plan_capture
from stpg_capture.timeline import ChannelMap, load_bundle_timeline, vector_steps
from stpg_capture.trace import TraceError, import_csv, import_lac
from stpg_capture.wiring import Wiring, stpg_gpio
from stil.source_artifacts import read_source_text
from stpg_runner import build_project, init_project, load_project
from stpg_runner.artifacts import read_json
from stpgview.runner.simulated import SIMULATED_DEVICE, SimulatedStpg, simulated_ports

from la_support import forbid_hardware
from accessory_harness import run_external

pytestmark = pytest.mark.stpg_host_only


@pytest.fixture(autouse=True)
def _no_hardware(monkeypatch):
    forbid_hardware(monkeypatch)  # real serial opens and USB metadata enumeration fail the test
FIXTURES = Path(__file__).parent / "fixtures"


def counter_stil(n: int) -> str:
    """Public synthetic pattern: 8-bit address counter, clock pulse, two compared outputs."""

    sig = [f"A{i}" for i in range(8)] + ["CLK", "EN", "DQ0", "DQ1"]
    lines = ['Header { Title "Synthetic counter pattern (public)"; }', "Signals {"]
    lines += [f"  {s} In;" for s in sig[:10]] + [f"  {s} Out;" for s in sig[10:]]
    lines += ["}", "SignalGroups {", "  ALL = '" + " + ".join(sig) + "';", "}", "Timing {",
              "  WaveformTable NORMAL {", "    Period '400ns';", "    Waveforms {"]
    lines += [f"      {s} {{ 0: 0ns Drive(0); 1: 0ns Drive(1); }}" for s in sig[:8] + ["EN"]]
    lines.append("      CLK { 0: 0ns Drive(0); 1: 0ns Drive(1); P: 0ns Drive(0), 100ns Drive(1), 300ns Drive(0); }")
    lines += [f"      {s} {{ H: 250ns Compare(1); L: 250ns Compare(0); X: 250ns Compare(X); }}" for s in sig[10:]]
    lines += ["    }", "  }", "}", "Pattern MAIN {", "  W NORMAL;"]
    for i in range(n):
        a = (i // 4) % 256 if i < n // 2 else (0x55 if i % 2 else 0xAA)
        bits = "".join("1" if a >> b & 1 else "0" for b in range(8))
        lines.append(f"  V {{ ALL = {bits}P1{'HL'[i % 2]}{'HLX'[i % 3]}; }}")
    lines.append("}")
    return "\n".join(lines) + "\n"


LOOP_STIL = """Header { Title "Public loop addressing fixture"; }
Signals {
  D0 In;
  D1 In;
  Q Out;
}
SignalGroups {
  ALL = 'D0 + D1 + Q';
}
Timing {
  WaveformTable W1 {
    Period '200ns';
    Waveforms {
      D0 { 0: 0ns Drive(0); 1: 0ns Drive(1); }
      D1 { 0: 0ns Drive(0); 1: 0ns Drive(1); }
      Q { H: 100ns Compare(1); L: 100ns Compare(0); X: 100ns Compare(X); }
    }
  }
}
Pattern MAIN {
  W W1;
  V { ALL = 00X; }
  V { ALL = 10X; }
  Loop 3 {
    V { ALL = 01H; }
    V { ALL = 11L; }
  }
  V { ALL = 00X; }
}
"""


def analyzer_config(**extra) -> AnalyzerConfig:
    value = {"kind": "stpg_capture_analyzer", "schema_version": 1, "port": "MOCK-LA", "vid": 0x2E8A, "pid": 0x000A,
             "serial_number": "PUBLIC-LA-0001", "limits": PICO2_TURBO_LIMITS.to_dict(), "wiring": "same-gpio"}
    value.update(extra)
    return AnalyzerConfig.from_dict(value)


def request(vector=None, *, line=None, occurrence=None, before=8, after=24, **extra) -> CaptureRequestSpec:
    address = {"vector": vector} if vector is not None else {"line": line}
    if occurrence is not None:
        address["occurrence"] = occurrence
    value = {"kind": "stpg_capture_request", "schema_version": 1, "address": address, "before_vectors": before,
             "after_vectors": after, "strategy": "auto"}
    value.update(extra)
    return CaptureRequestSpec.from_dict(value)


def _build(tmp_path_factory, name: str, text: str, **init) -> object:
    root = tmp_path_factory.mktemp(name)
    source = root / "pattern.stil"
    source.write_text(text, encoding="utf-8")
    project = init_project(source, root / "project", wait_preset="none", completion="hold", **init)
    return build_project(project)


@pytest.fixture(scope="module")
def counter_bundle(tmp_path_factory):
    return _build(tmp_path_factory, "capture-counter", counter_stil(1600))


@pytest.fixture(scope="module")
def counter_timeline(counter_bundle):
    return load_bundle_timeline(counter_bundle.bundle_dir, manifest_sha256=counter_bundle.manifest_sha256)


@pytest.fixture(scope="module")
def loop_timeline(tmp_path_factory):
    built = _build(tmp_path_factory, "capture-loop", LOOP_STIL)
    return load_bundle_timeline(built.bundle_dir, manifest_sha256=built.manifest_sha256)


# ---- wire format --------------------------------------------------------------

def test_frame_escapes_reserved_bytes_and_round_trips():
    payload = bytes([0x01, 0x55, 0xAA, 0xF0, 0x00, 0x7F])
    framed = lp.frame(payload)
    assert framed.startswith(b"\x55\xaa") and framed.endswith(b"\xaa\x55")
    assert b"\xf0\xa5" in framed and b"\xf0\x5a" in framed and b"\xf0\x00" in framed
    assert lp.unframe(framed) == payload
    assert lp.identify_frame() == b"\x55\xaa\x00\xaa\x55"


def test_capture_request_matches_the_firmware_struct_layout():
    req = lp.CaptureRequest(lp.TRIGGER_COMPLEX, 3, 5, 0x1234, (0, 1, 2, 9), 50_000_000, 100, 900, lp.MODE_16)
    raw = req.pack()
    assert len(raw) == 48
    # C layout: u8,u8,u8,pad,u16 @4, channels[24] @6, count @30, pad, u32 freq @32, pre @36, post @40, loop @44 ...
    assert raw[0:3] == bytes([1, 3, 5]) and struct.unpack_from("<H", raw, 4)[0] == 0x1234
    assert raw[6:10] == bytes([0, 1, 2, 9]) and raw[30] == 4
    assert struct.unpack_from("<III", raw, 32) == (50_000_000, 100, 900)
    assert raw[44:48] == bytes([0, 0, lp.MODE_16, 0])
    assert lp.CaptureRequest.unpack(raw) == req


def test_identify_parsing_is_strict():
    lines = ["LOGIC_ANALYZER_PICO_2_V6_0", "FREQ:200000000", "BLASTFREQ:400000000", "BUFFER:393216", "CHANNELS:24"]
    assert lp.parse_identify(lines) == PICO2_TURBO_LIMITS
    with pytest.raises(lp.ProtocolError):
        lp.parse_identify(["garbage"] + lines[1:])
    with pytest.raises(lp.ProtocolError):
        lp.parse_identify(["LOGIC_ANALYZER_PICO_2_V5_9"] + lines[1:])
    with pytest.raises(lp.ProtocolError):
        lp.parse_identify(lines[:4])


def test_build_request_applies_driver_limits_and_latency_compensation():
    info = PICO2_TURBO_LIMITS
    # 16 channels -> 2 bytes/sample -> 196608 samples; driver pre-trigger limit 10 %.
    with pytest.raises(lp.ProtocolError, match="pre-trigger"):
        lp.build_request(info, channels=list(range(12)), frequency=100_000_000, pre_samples=19661, post_samples=10,
                         trigger_type=lp.TRIGGER_EDGE, trigger_channel=0)
    ok = lp.build_request(info, channels=list(range(12)), frequency=100_000_000, pre_samples=19661, post_samples=10,
                          trigger_type=lp.TRIGGER_EDGE, trigger_channel=0, pretrigger_policy="firmware")
    assert ok.capture_mode == lp.MODE_16 and (ok.pre_samples, ok.post_samples) == (19661, 10)
    with pytest.raises(lp.ProtocolError, match="contiguous"):
        lp.build_request(info, channels=[0, 1], frequency=10_000_000, pre_samples=10, post_samples=10,
                         trigger_type=lp.TRIGGER_COMPLEX, trigger_channel=10, trigger_count=7)
    with pytest.raises(lp.ProtocolError, match="ascending"):
        lp.build_request(info, channels=[3, 1], frequency=10_000_000, pre_samples=10, post_samples=10,
                         trigger_type=lp.TRIGGER_EDGE, trigger_channel=0)
    complex_req = lp.build_request(info, channels=[0, 1, 2], frequency=200_000_000, pre_samples=20, post_samples=100,
                                   trigger_type=lp.TRIGGER_COMPLEX, trigger_channel=0, trigger_count=3,
                                   trigger_value=0b101)
    offset = round(5 * 5 / 5 + 0.3)  # official driver: delay 5 cycles of max frequency, in samples
    assert (complex_req.pre_samples, complex_req.post_samples) == (20 + offset, 100 - offset)
    assert complex_req.total_samples == 120


@pytest.mark.parametrize("mode,channels", [(lp.MODE_8, [0, 7]), (lp.MODE_16, [0, 15]), (lp.MODE_24, [0, 23])])
def test_parse_capture_widths_and_trailers(mode, channels):
    req = lp.CaptureRequest(lp.TRIGGER_EDGE, 0, 0, 0, tuple(channels), 1_000_000, 2, 3, mode)
    from stpg_capture.mock import capture_bytes

    data = capture_bytes(req, [0, 1, 2, 3, 1])
    assert lp.expected_capture_bytes(req) == len(data)
    assert lp.parse_capture(data, req) == [0, 1, 2, 3, 1]
    with pytest.raises(lp.ProtocolError):
        lp.parse_capture(data + b"\x00", req)
    with pytest.raises(lp.ProtocolError):
        lp.parse_capture(data[:-2], req)
    with pytest.raises(lp.ProtocolError):
        lp.parse_capture(struct.pack("<I", 4) + data[4:], req)
    with pytest.raises(lp.ProtocolError, match="bits above"):
        lp.parse_capture(capture_bytes(req, [0, 4, 0, 0, 0]), req)


# ---- wiring / configuration --------------------------------------------------------

def test_wiring_follows_the_board_map_and_skips_gpio23():
    assert stpg_gpio(0, 16) == 2 and stpg_gpio(15, 16) == 17
    assert [stpg_gpio(b, 24) for b in (16, 17, 20, 21, 23)] == [18, 20, 23, 26, 28]
    wiring = Wiring.same_gpio()
    assert wiring.channel(2) == 0 and wiring.channel(22) == 20 and wiring.channel(26) == 21
    assert wiring.channel(23) is None
    with pytest.raises(ValueError):
        Wiring.from_config({"2": 1, "3": 1})
    custom = Wiring.from_config({"2": 5})
    assert custom.channel(2) == 4 and custom.to_config() == {"2": 5}


def test_wiring_presets_cover_the_planned_board_and_explicit_gpio_maps(tmp_path, capsys):
    planned = Wiring.preset("stpg20-23-to-la19-22")
    to_la = {gpio: lp.CHANNEL_GPIO[ch] for gpio, ch in planned.gpio_to_channel.items()}
    assert {gpio: to_la[gpio] for gpio in (18, 20, 21, 22, 23, 26, 27, 28)} == {
        18: 18, 20: 19, 21: 20, 22: 21, 23: 22, 26: 26, 27: 27, 28: 28}  # the user's table
    assert all(to_la[gpio] == gpio for gpio in range(2, 19))
    assert len(to_la) == 24 and Wiring.same_gpio().channel(23) is None and planned.channel(23) == 20
    assert Wiring.from_config(planned.to_config()) == planned and planned.to_config() == "stpg20-23-to-la19-22"
    assert [row["analyzer_gpio"] for row in planned.rows()][16:21] == [18, 19, 20, 21, 22]
    explicit = Wiring.from_config({"analyzer_gpio": {"20": 19, "2": 2}})
    assert explicit.channel(20) == 17 and explicit.to_config() == {"analyzer_gpio": {"2": 2, "20": 19}}
    for bad in ("no-such-preset", {"analyzer_gpio": {"19": 19}}, {"analyzer_gpio": {"20": 23}},
                {"analyzer_gpio": {"20": 19, "21": 19}}, {"analyzer_gpio": {"20": "19"}}, {"analyzer_gpio": {}}):
        with pytest.raises(ValueError):
            Wiring.from_config(bad)
    # GPIO23 (logical bit 20) becomes a usable marker pin only on the planned wiring.
    assert analyzer_config(marker_gpio=23, wiring="stpg20-23-to-la19-22").wiring.channel(23) == 20
    out = tmp_path / "la.json"
    assert capture_main(["init-analyzer", str(out), "--port", "MOCK-LA", "--vid", "0x2e8a", "--pid", "0x000a",
                         "--serial-number", "PUBLIC-LA-0001", "--limits", "pico2-v6",
                         "--wiring", "stpg20-23-to-la19-22", "--marker-gpio", "23"]) == 0
    assert read_json(out)["wiring"] == "stpg20-23-to-la19-22" and read_json(out)["marker_gpio"] == 23
    capsys.readouterr()


def test_analyzer_config_and_request_reject_unknown_or_unsafe_fields():
    with pytest.raises(CaptureConfigError):
        analyzer_config(extra_field=1)
    with pytest.raises(CaptureConfigError, match="marker"):
        analyzer_config(marker_gpio=23)  # executable but not wired to the analyzer
    assert analyzer_config(marker_gpio=18).marker_gpio == 18
    with pytest.raises(CaptureConfigError):
        request(1, strategy="guess")
    with pytest.raises(AddressError):
        CaptureRequestSpec.from_dict({"kind": "stpg_capture_request", "schema_version": 1,
                                      "address": {"vector": 1, "line": 3}, "before_vectors": 0,
                                      "after_vectors": 0, "strategy": "auto"})
    with pytest.raises(AddressError):
        request(None, line=10, occurrence=-1)


# ---- addressing ------------------------------------------------------------------------

def test_line_addresses_require_an_occurrence_inside_loops(loop_timeline):
    segment = loop_timeline.segments[0]
    lines = list(segment.trace.lines)
    assert loop_timeline.total_vectors == 9  # 2 + 3 * 2 + 1 expanded vectors
    loop_line = lines[2]
    assert lines.count(loop_line) == 3
    with pytest.raises(AddressError, match="occurrence 0..2"):
        resolve(loop_timeline, Address(line=loop_line))
    third = resolve(loop_timeline, Address(line=loop_line, occurrence=2))
    assert (third.vector, third.occurrences) == (6, 3)
    with pytest.raises(AddressError, match="does not exist"):
        resolve(loop_timeline, Address(line=loop_line, occurrence=3))
    single = resolve(loop_timeline, Address(line=lines[0]))
    assert single.vector == 0 and single.occurrence == 0
    with pytest.raises(AddressError, match="no vector"):
        resolve(loop_timeline, Address(line=1))
    with pytest.raises(Exception, match="outside the bundle"):
        resolve(loop_timeline, Address(vector=9))


# ---- planning ---------------------------------------------------------------------------

def _earliest_possible_match(timeline, plan) -> int:
    """Independent oracle: first time the trigger condition can be seen (steady or while switching).

    The analyzer is armed right before the target segment's RUN, so it sees the
    previous segment's HOLD levels first (released channels unknown): a match
    there is reported as -1.
    """

    segment = timeline.segments[plan["bundle"]["segment"]]
    cmap = ChannelMap.build(segment, Wiring.same_gpio())
    trig = plan["trigger"]
    mask = ((1 << trig["count"]) - 1) << trig["base_channel"]
    cond = trig["value"] << trig["base_channel"]
    prev = None
    if segment.index:
        before = timeline.segments[segment.index - 1]
        before_map = ChannelMap.build(before, Wiring.same_gpio())
        _, value, enable = vector_steps(before.trace, before.vector_count - 1)[-1]
        prev = (before_map.project(value), before_map.project(enable))
        if (prev[0] ^ cond) & mask & prev[1] == 0:
            return -1
    for index in range(segment.vector_count):
        for t, value, enable in vector_steps(segment.trace, index):
            v, e = cmap.project(value), cmap.project(enable)
            if prev is None:
                if e:
                    prev = (v, e)  # first drive at RUN start: pre-RUN assumption
                    if v & mask == cond and e & mask == mask:
                        return t
                continue
            pv, pe = prev
            if (pv, pe) != (v, e):
                wild = ((pv ^ v) | (pe ^ e) | ~pe | ~e) & mask
                if (pv ^ cond) & mask & ~wild == 0:
                    return t
            if e & mask == mask and v & mask == cond:
                return t
            prev = (v, e)
    raise AssertionError("condition never occurs")


@pytest.mark.parametrize("target", [5, 300, 700])
def test_pattern_trigger_first_occurs_exactly_at_the_planned_time(counter_timeline, target):
    plan = plan_capture(counter_timeline, request(target), analyzer_config())
    assert plan["status"] == "PLANNED", plan["reasons"]
    assert plan["device_io_performed"] is False
    assert _earliest_possible_match(counter_timeline, plan) == plan["trigger"]["time_ps"]
    start, end = plan["predicted"]["start_ps"], plan["predicted"]["end_ps"]
    assert start <= plan["window"]["start_ps"] and end >= plan["window"]["end_ps"]
    assert plan["samples"]["pre"] <= plan["analyzer"]["pretrigger_limit"]
    assert plan["samples"]["total"] <= plan["analyzer"]["buffer_samples"]
    assert any("held before that RUN" in text for text in plan["assumptions"])


def test_plan_prefers_a_state_near_the_window_over_the_run_start(counter_timeline):
    plan = plan_capture(counter_timeline, request(300), analyzer_config())
    assert not plan["trigger"]["at_run_start"]
    assert 280 <= plan["trigger"]["vector"] <= 300


def test_plan_reports_unsupported_cases_instead_of_guessing(counter_timeline):
    nothing = analyzer_config(wiring={"28": 24})  # no pattern signal on a wired GPIO
    plan = plan_capture(counter_timeline, request(10), nothing)
    assert plan["status"] == "UNSUPPORTED" and plan["trigger"] is None and plan["reasons"]
    marker = plan_capture(counter_timeline, request(10, strategy="marker"), analyzer_config())
    assert marker["status"] == "UNSUPPORTED" and "marker" in " ".join(marker["reasons"])
    with pytest.raises(PlanError, match="max/n"):
        plan_capture(counter_timeline, request(10, sample_rate_hz=33_000_000), analyzer_config())
    with pytest.raises(PlanError, match="not in this pattern"):
        plan_capture(counter_timeline, request(10, signals=["NOPE"]), analyzer_config())


def test_long_windows_lower_the_sample_rate_to_fit_the_buffer(counter_timeline):
    small = dict(PICO2_TURBO_LIMITS.to_dict(), buffer_bytes=65536)  # 32768 samples in 16-channel mode
    plan = plan_capture(counter_timeline, request(1200, before=600, after=300), analyzer_config(limits=small))
    assert plan["status"] == "PLANNED", plan["reasons"]
    assert plan["sample_rate_hz"] < PICO2_TURBO_LIMITS.max_frequency
    # integer divider of the analyzer clock: the period is exact even when the rate in Hz is not
    assert plan["sample_period_ps"] == plan["divider"] * 10**12 // PICO2_TURBO_LIMITS.max_frequency
    assert plan["sample_rate_hz"] == PICO2_TURBO_LIMITS.max_frequency // plan["divider"]
    assert plan["samples"]["total"] <= 32768
    trace = synthesize(counter_timeline, plan, offset_ps=3_000)
    assert trace.sample_period_ps == plan["sample_period_ps"]
    assert align(counter_timeline, plan, trace)["status"] == "ALIGNED"


# ---- alignment ---------------------------------------------------------------------------

def test_alignment_recovers_offset_and_drift(counter_timeline):
    plan = plan_capture(counter_timeline, request(300, after=200), analyzer_config())
    trace = synthesize(counter_timeline, plan, offset_ps=12_000, drift_ppm=60)
    result = align(counter_timeline, plan, trace)
    assert result["status"] == "ALIGNED" and result["confidence"] == "normal"
    assert abs(result["offset_ps"] - 12_000) <= plan["sample_period_ps"]
    assert abs(result["drift_ppm"] - 60) <= 2 * result["drift_resolution_ppm"] + 5
    assert result["coverage"] == "FULL"
    assert all(level["agree"] == level["total"] for level in result["levels"])
    assert result["strobe_observations"] and "not a DUT result" in result["strobe_note"]


def test_a_misplaced_trigger_is_unaligned_not_refitted(counter_timeline):
    plan = plan_capture(counter_timeline, request(300), analyzer_config())
    early = synthesize(counter_timeline, plan, offset_ps=-8 * 400_000)  # e.g. a pre-RUN trigger 8 vectors early
    result = align(counter_timeline, plan, early)
    assert result["status"] == "UNALIGNED"
    assert result["coverage"] == "UNKNOWN"
    assert any("do not match" in note for note in result["notes"])


# ---- trace import ------------------------------------------------------------------------

def test_csv_and_lac_import(tmp_path):
    csv_path = tmp_path / "c.csv"
    csv_path.write_text("Channel 2,Channel 1\n0,1\n1,1\n1,0\n", encoding="utf-8")
    trace = import_csv(csv_path, sample_rate_hz=1_000_000, trigger_index=1)
    assert trace.channels == (0, 1) and trace.samples == [0b01, 0b11, 0b10]
    with pytest.raises(TraceError, match="channel numbers"):
        (tmp_path / "d.csv").write_text("A,B\n0,1\n", encoding="utf-8")
        import_csv(tmp_path / "d.csv", sample_rate_hz=1_000_000, trigger_index=0)
    named = import_csv(tmp_path / "d.csv", sample_rate_hz=1_000_000, trigger_index=0, channels=[3, 4])
    assert named.channels == (2, 3) and named.names == ("A", "B")
    lac = tmp_path / "x.lac"
    lac.write_text(json.dumps({"Settings": {"Frequency": 2_000_000, "PreTriggerSamples": 1, "PostTriggerSamples": 2,
                                            "LoopCount": 0, "CaptureChannels": [
                                                {"ChannelNumber": 4, "ChannelName": "B", "Samples": base64.b64encode(bytes([1, 0, 1])).decode()},
                                                {"ChannelNumber": 1, "ChannelName": "A", "Samples": base64.b64encode(bytes([0, 0, 1])).decode()}]}}),
                   encoding="utf-8")
    trace = import_lac(lac)
    assert trace.channels == (1, 4) and trace.samples == [0b10, 0b00, 0b11] and trace.trigger_index == 1


# ---- analyzer adapter ----------------------------------------------------------------------

def _ports(config):
    return lambda: [SimpleNamespace(device=config.port, vid=config.vid, pid=config.pid,
                                    serial_number=config.serial_number)]


def test_session_identifies_exactly_and_refuses_the_stpg_port():
    config = analyzer_config()
    model = ModelAnalyzer(PICO2_LIMITS)  # a 100 MHz build while the binding expects the TURBO build
    session = AnalyzerSession(config, stream_factory=lambda _: model, port_provider=_ports(config))
    session.open()
    try:
        with pytest.raises(AnalyzerError, match="binding expects"):
            session.identify()
    finally:
        session.close()
    assert model.closed == 1 and model.writes == [lp.identify_frame()]
    same = AnalyzerSession(config, stpg_identity=config.identity)
    with pytest.raises(AnalyzerError, match="second owner"):
        same.open()
    port = AnalyzerSession(config, stpg_port=config.port)
    with pytest.raises(AnalyzerError, match="second owner"):
        port.open()


def test_session_arms_collects_and_cancels():
    config = analyzer_config()
    model = ModelAnalyzer(provider=lambda req: [i % 2 for i in range(req.total_samples)])
    session = AnalyzerSession(config, stream_factory=lambda _: model, port_provider=_ports(config))
    session.open()
    session.identify()
    req = lp.build_request(config.limits, channels=[0], frequency=1_000_000, pre_samples=4, post_samples=8,
                           trigger_type=lp.TRIGGER_EDGE, trigger_channel=0)
    session.arm(req)
    assert session.wait(0.05) is None  # nothing triggered yet
    model.fire()
    data = session.wait(2.0)
    assert lp.parse_capture(data, req) == [i % 2 for i in range(12)]
    session.close()
    idle = ModelAnalyzer()
    second = AnalyzerSession(config, stream_factory=lambda _: idle, port_provider=_ports(config))
    second.open()
    second.identify()
    second.arm(req)
    second.cancel()
    second.close()
    assert idle.cancelled and idle.writes[-1] == b"\xff" and idle.closed == 1
    assert any(entry["event"] == "write.begin" and entry["label"] == "cancel" for entry in second.journal)


# ---- coordinator with the simulated STPG ----------------------------------------------------

def _run(bundle, timeline, plan, out, *, stpg=None, model=None, device=SIMULATED_DEVICE, analyzer=None):
    view, stpg, model, _ = run_external(bundle, plan, out, analyzer=analyzer or analyzer_config(), model=model,
                                        stpg=stpg, device=device)
    return view, stpg, model


def test_a_core_run_with_the_accessory_keeps_run_and_capture_apart(counter_bundle, counter_timeline, tmp_path):
    plan = plan_capture(counter_timeline, request(300), analyzer_config())
    result, stpg, model = _run(counter_bundle, counter_timeline, plan, tmp_path / "out")
    capture = result["capture"]
    assert result["run"]["outcome"] == "COMPLETED"
    assert result["external"]["outcome"] == "COMPLETED" and result["external"]["adopted"] is True
    assert capture["outcome"] == "CAPTURED" and capture["alignment_status"] == "ALIGNED"
    assert capture["dut_judgement"] is None and capture["electrical_validation"] is False
    assert capture["bundle_manifest_sha256"] == counter_bundle.manifest_sha256 and capture["mode"] == "device"
    assert capture["run"] == {"directory": None, "result": None, "summary": None}  # the run is the core's record
    assert (Path(result["run_dir"]) / "run.json.gz").is_file()
    assert read_json(Path(result["output_dir"]) / "accessory" / "result.json") == {
        "kind": "stpg_external_accessory_result", "schema_version": 1, "mode": "device", "outcome": "COMPLETED",
        "adopted": True, "errors": []}
    assert model.closed == 1 and stpg.closed == 1 and not model.cancelled


def test_stpg_rejection_before_run_leaves_the_analyzer_unarmed(counter_bundle, counter_timeline, tmp_path):
    plan = plan_capture(counter_timeline, request(300), analyzer_config())
    wrong = SIMULATED_DEVICE.__class__(SIMULATED_DEVICE.port, SIMULATED_DEVICE.vid, SIMULATED_DEVICE.pid,
                                       SIMULATED_DEVICE.serial_number, SIMULATED_DEVICE.firmware_version + 1,
                                       SIMULATED_DEVICE.capability)
    result, stpg, model = _run(counter_bundle, counter_timeline, plan, tmp_path / "out", device=wrong)
    assert result["run"]["outcome"] == "REJECT" and result["external"]["outcome"] == "INCOMPLETE"
    assert result["capture"]["outcome"] == "NO_RUN"
    # The analyzer is armed only at the target RUN: a rejection before it leaves the analyzer unarmed.
    assert c.CMD_RUN not in stpg.commands and not model.requests and not model.cancelled and model.closed == 1
    assert result["capture"]["arming"]["armed_segment"] is None


def test_analyzer_mismatch_stops_before_any_stpg_contact(counter_bundle, counter_timeline, tmp_path):
    plan = plan_capture(counter_timeline, request(300), analyzer_config())
    model = ModelAnalyzer(PICO2_LIMITS)
    result, stpg, _ = _run(counter_bundle, counter_timeline, plan, tmp_path / "out", model=model)
    assert result["capture"]["outcome"] == "ANALYZER_ERROR" and result["run"] is None
    assert result["external"]["outcome"] == "REJECT"
    assert stpg.commands == [] and not (tmp_path / "out" / "stpg").exists()
    assert any("binding expects" in e for e in result["capture"]["errors"])


def test_plan_for_another_bundle_is_rejected_before_the_analyzer_is_opened(counter_bundle, counter_timeline,
                                                                           tmp_path):
    plan = plan_capture(counter_timeline, request(300), analyzer_config())
    plan["bundle"]["segment_sha256"] = "0" * 64
    result, stpg, model = _run(counter_bundle, counter_timeline, plan, tmp_path / "out")
    assert result["external"]["outcome"] == "REJECT" and result["run"] is None and result["capture"] is None
    assert re.search("re-derivation .* bundle.segment_sha256", " ".join(result["rejected"]["errors"]))
    assert result["rejected"]["analyzer_opened"] is False and model.writes == [] and stpg.commands == []


# ---- CLI parity -------------------------------------------------------------------------------

def test_cli_plan_mock_and_import_are_offline(counter_bundle, tmp_path, capsys):
    analyzer = tmp_path / "la.json"
    assert capture_main(["init-analyzer", str(analyzer), "--port", "MOCK", "--vid", "0x2e8a", "--pid", "0xa",
                         "--serial-number", "PUBLIC", "--limits", "pico2-v6-turbo"]) == 0
    assert capture_main(["init-analyzer", str(analyzer), "--port", "MOCK", "--vid", "0x2e8a", "--pid", "0xa",
                         "--serial-number", "PUBLIC", "--limits", "pico2-v6-turbo"]) == 2  # never overwritten
    capsys.readouterr()
    common = [str(counter_bundle.bundle_dir), "--manifest-sha256", counter_bundle.manifest_sha256]
    assert capture_main(["plan", *common, "--analyzer", str(analyzer), "--vector", "300", "-o",
                         str(tmp_path / "plan.json")]) == 0
    plan = read_json(tmp_path / "plan.json")
    assert plan["status"] == "PLANNED"
    capsys.readouterr()
    assert capture_main(["mock", *common, "--plan", str(tmp_path / "plan.json"), "--output-dir",
                         str(tmp_path / "mock")]) == 0
    mock = json.loads(capsys.readouterr().out)
    assert mock["outcome"] == "MOCK" and mock["alignment_status"] == "ALIGNED"
    # Import the mock's samples back through the CSV path (same rate/trigger) and expect the same alignment.
    samples = read_json(tmp_path / "mock" / "capture.json")
    from stpg_capture.trace import load_native

    trace = load_native(tmp_path / "mock", samples["samples"], sample_rate_hz=plan["sample_rate_hz"],
                        channels=plan["capture_channels"], trigger_index=plan["samples"]["pre"])
    rows = [",".join(f"Channel {ch + 1}" for ch in trace.channels)]
    rows += [",".join(str(s >> bit & 1) for bit in range(len(trace.channels))) for s in trace.samples]
    (tmp_path / "t.csv").write_text("\n".join(rows) + "\n", encoding="utf-8")
    assert capture_main(["import", *common, "--plan", str(tmp_path / "plan.json"), "--trace", str(tmp_path / "t.csv"),
                         "--sample-rate", str(plan["sample_rate_hz"]), "--trigger-index", str(plan["samples"]["pre"]),
                         "--output-dir", str(tmp_path / "imp")]) == 0
    imported = json.loads(capsys.readouterr().out)
    assert imported["alignment_status"] == "ALIGNED"
    assert imported["alignment"]["actual"] == mock["alignment"]["actual"]


# ---- marker patterns ------------------------------------------------------------------------------

from stpg_capture.marker import MarkerError, derive_marker_project, rewrite_stil, verify_derivation  # noqa: E402


def _project_of(bundle) -> Path:
    return Path(bundle.bundle_dir).parent.parent / "project.stpg-project.json"


def test_marker_pattern_keeps_every_original_timeline(counter_bundle, counter_timeline, tmp_path):
    analyzer = analyzer_config(marker_gpio=18)
    derived = derive_marker_project(_project_of(counter_bundle), bundle_dir=counter_bundle.bundle_dir,
                                    manifest_sha256=counter_bundle.manifest_sha256, vector=900, analyzer=analyzer,
                                    directory=tmp_path / "derived")
    info = derived["derivation"]
    assert info["marker"] == {"signal": "STPG_MARKER", "gpio": 18, "bit": 16, "analyzer_channel": 17}
    assert info["active_pins"] == {"source": 16, "derived": 24}
    # The SDK names the files it stored (a large input is gzip): read them through the project.
    inputs = load_project(Path(derived["project_path"])).input_paths(tmp_path / "derived")
    pins = read_json(inputs["pin_map"])
    assert pins["signals"]["STPG_MARKER"]["role"] == "marker" and pins["active_pins"] == 24
    built = build_project(Path(derived["project_path"]))
    assert built.manifest_sha256 != counter_bundle.manifest_sha256
    timeline = load_bundle_timeline(built.bundle_dir, manifest_sha256=built.manifest_sha256)
    verification = verify_derivation(counter_timeline, timeline, info)
    assert verification["status"] == "EQUIVALENT_TIMING", verification["problems"]
    plan = plan_capture(timeline, request(900), analyzer)
    trig = plan["trigger"]
    assert plan["status"] == "PLANNED" and trig["strategy"] == "marker"
    assert (trig["vector"], trig["base_channel"], trig["value"]) == (900, 16, 1)
    assert trig["robust_pre_run"] is True
    # A derived pattern whose timing differs is rejected, not approximately accepted.
    text = read_source_text(inputs["stil"])
    assert "Period '400ns'" in text
    changed = tmp_path / "changed"
    for role, path in inputs.items():  # the same file names and codecs as the derived project
        target = changed / path.relative_to(tmp_path / "derived")
        target.parent.mkdir(parents=True, exist_ok=True)
        data = text.replace("Period '400ns'", "Period '420ns'").encode("utf-8") if role == "stil" else path.read_bytes()
        target.write_bytes(gzip.compress(data, mtime=0) if role == "stil" and path.suffix == ".gz" else data)
    (changed / "project.stpg-project.json").write_text(Path(derived["project_path"]).read_text())
    other = build_project(changed / "project.stpg-project.json")
    other_timeline = load_bundle_timeline(other.bundle_dir, manifest_sha256=other.manifest_sha256)
    assert verify_derivation(counter_timeline, other_timeline, info)["status"] == "REJECT"


def test_marker_derivation_refuses_unsafe_or_ambiguous_requests(counter_bundle, tmp_path):
    project = _project_of(counter_bundle)
    common = dict(bundle_dir=counter_bundle.bundle_dir, manifest_sha256=counter_bundle.manifest_sha256)
    with pytest.raises(MarkerError, match="marker_gpio"):
        derive_marker_project(project, vector=10, analyzer=analyzer_config(), directory=tmp_path / "a", **common)
    with pytest.raises(MarkerError, match="used by signal A0"):
        derive_marker_project(project, vector=10, analyzer=analyzer_config(marker_gpio=2), directory=tmp_path / "b",
                              **common)
    assert not (tmp_path / "a").exists() and not (tmp_path / "b").exists()


def test_marker_inside_a_loop_is_rejected(tmp_path):
    source = tmp_path / "loop.stil"
    source.write_text(LOOP_STIL, encoding="utf-8")
    with pytest.raises(MarkerError, match="top-level"):
        rewrite_stil(LOOP_STIL, source, target_line=LOOP_STIL.splitlines().index("    V { ALL = 01H; }") + 1)
    out, info = rewrite_stil(LOOP_STIL, source, target_line=LOOP_STIL.splitlines().index("  V { ALL = 10X; }") + 1)
    assert info == {"group": "ALL", "vector_statements": 5}
    assert "V { ALL = 10X 1; }" in out and "V { ALL = 01H 0; }" in out and "STPG_MARKER In;" in out
