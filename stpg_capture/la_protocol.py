"""Wire format of the gusmanb LogicAnalyzer firmware (V6 protocol), pure functions only.

Reference: github.com/gusmanb/logicanalyzer, Firmware/LogicAnalyzer_V2
(LogicAnalyzer.c, LogicAnalyzer_Structs.h, LogicAnalyzer_Capture.c) and the host
driver Software/LogicAnalyzer/SharedDriver (LogicAnalyzerDriver.cs,
AnalyzerDriverBase.cs). Nothing here opens a port.

Host -> device: binary frames ``55 AA <escaped payload> AA 55``; bytes AA, 55
and F0 inside the payload are sent as ``F0, byte ^ F0``. Payload byte 0 is the
command (0 = identify, 1 = capture request + 48-byte CAPTURE_REQUEST).
Device -> host: text lines (identify, CAPTURE_STARTED/CAPTURE_ERROR), then the
capture as ``u32 sample count | samples (1/2/4 bytes each) | u8 stamp count |
stamps (u32 each, only when the count is > 1)``.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
import struct

FRAME_START = b"\x55\xaa"
FRAME_END = b"\xaa\x55"
ESCAPE = 0xF0

CMD_IDENTIFY = 0
CMD_CAPTURE = 1

TRIGGER_EDGE = 0      # level trigger in the firmware: fires as soon as the channel is at the level
TRIGGER_COMPLEX = 1   # pattern of up to 16 contiguous channels within channels 0..15
TRIGGER_FAST = 2      # pattern of up to 5 channels (not used by the planner)
TRIGGER_BLAST = 3     # not used

MODE_8 = 0
MODE_16 = 1
MODE_24 = 2
BYTES_PER_SAMPLE = {MODE_8: 1, MODE_16: 2, MODE_24: 4}

# Firmware pinMap for the Pico / Pico 2 builds: channel index (0-based) -> analyzer GPIO.
# Index 24 is the external trigger input (GPIO1), usable only as an edge-trigger channel.
CHANNEL_GPIO = (2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 26, 27, 28)
EXT_TRIGGER_CHANNEL = 24

# Trigger latency compensation used by the official driver (in max-frequency cycles).
COMPLEX_TRIGGER_DELAY = 5
FAST_TRIGGER_DELAY = 3

REQUEST_FORMAT = "<BBBxH24sBxIIIBBBx"  # natural C layout of CAPTURE_REQUEST, 48 bytes
REQUEST_SIZE = struct.calcsize(REQUEST_FORMAT)
assert REQUEST_SIZE == 48

_VERSION = re.compile(r".*?(V([0-9]+)_([0-9]+))$")
MIN_VERSION = (6, 0)


class ProtocolError(ValueError):
    """Malformed or unexpected analyzer data; nothing is retried or guessed."""


def escape(payload: bytes) -> bytes:
    out = bytearray()
    for byte in payload:
        if byte in (0xAA, 0x55, ESCAPE):
            out += bytes((ESCAPE, byte ^ ESCAPE))
        else:
            out.append(byte)
    return bytes(out)


def frame(payload: bytes) -> bytes:
    return FRAME_START + escape(payload) + FRAME_END


def unframe(raw: bytes) -> bytes:
    """Inverse of :func:`frame` (used by the host-test analyzer model)."""

    if not (raw.startswith(FRAME_START) and raw.endswith(FRAME_END)) or len(raw) < 5:
        raise ProtocolError("not a framed message")
    body = raw[2:-2]
    out = bytearray()
    i = 0
    while i < len(body):
        if body[i] == ESCAPE:
            if i + 1 >= len(body):
                raise ProtocolError("dangling escape byte")
            out.append(body[i + 1] ^ ESCAPE)
            i += 2
        else:
            out.append(body[i])
            i += 1
    return bytes(out)


def identify_frame() -> bytes:
    return frame(bytes((CMD_IDENTIFY,)))


@dataclass(frozen=True)
class AnalyzerInfo:
    """Identify response. The numbers bound capture planning, so they are compared exactly."""

    id_line: str
    max_frequency: int
    blast_frequency: int
    buffer_bytes: int
    channels: int

    @property
    def version(self) -> tuple[int, int]:
        match = _VERSION.match(self.id_line)
        if not match:
            return (0, 0)
        return int(match.group(2)), int(match.group(3))

    @property
    def min_frequency(self) -> int:
        return (self.max_frequency * 2) // 65535

    def to_dict(self) -> dict:
        return {"id_line": self.id_line, "max_frequency": self.max_frequency,
                "blast_frequency": self.blast_frequency, "buffer_bytes": self.buffer_bytes,
                "channels": self.channels}

    @classmethod
    def from_dict(cls, value: dict) -> "AnalyzerInfo":
        fields = {"id_line", "max_frequency", "blast_frequency", "buffer_bytes", "channels"}
        if not isinstance(value, dict) or set(value) != fields:
            raise ProtocolError("analyzer limits need exactly id_line, max_frequency, blast_frequency, "
                                "buffer_bytes and channels")
        if not isinstance(value["id_line"], str) or not value["id_line"].startswith("LOGIC_ANALYZER_"):
            raise ProtocolError("id_line must be the analyzer identify line (LOGIC_ANALYZER_...)")
        for key in fields - {"id_line"}:
            if type(value[key]) is not int or value[key] <= 0:
                raise ProtocolError(f"{key} must be a positive integer")
        info = cls(**value)
        info.validate()
        return info

    def validate(self) -> None:
        if self.version < MIN_VERSION:
            raise ProtocolError(f"analyzer firmware {self.id_line!r} is older than V{MIN_VERSION[0]}_{MIN_VERSION[1]} "
                                "or has no version suffix")
        if self.channels < 1 or self.channels > 24:
            raise ProtocolError("analyzer reports an unsupported channel count")
        if self.buffer_bytes % 4:
            raise ProtocolError("analyzer buffer size is not a multiple of 4 bytes")


def parse_identify(lines: list[str]) -> AnalyzerInfo:
    """Parse the five identify lines (version, FREQ, BLASTFREQ, BUFFER, CHANNELS)."""

    if len(lines) != 5:
        raise ProtocolError(f"identify response needs 5 lines, got {len(lines)}")
    id_line = lines[0]
    if not id_line.startswith("LOGIC_ANALYZER_"):
        raise ProtocolError(f"unexpected identify line {id_line[:60]!r} (stale data or another device?)")
    values = []
    for line, key in zip(lines[1:], ("FREQ", "BLASTFREQ", "BUFFER", "CHANNELS")):
        match = re.fullmatch(rf"{key}:([0-9]+)", line)
        if not match:
            raise ProtocolError(f"expected {key}:<n>, got {line[:60]!r}")
        values.append(int(match.group(1)))
    info = AnalyzerInfo(id_line, *values)
    info.validate()
    return info


def capture_mode(channels: list[int] | tuple[int, ...]) -> int:
    highest = max(channels) if channels else 0
    return MODE_8 if highest < 8 else MODE_16 if highest < 16 else MODE_24


def buffer_samples(info: AnalyzerInfo, mode: int) -> int:
    return info.buffer_bytes // BYTES_PER_SAMPLE[mode]


def pretrigger_limit(info: AnalyzerInfo, mode: int, policy: str) -> int:
    """Largest pre-trigger sample count: the official driver allows 10 % of the buffer."""

    total = buffer_samples(info, mode)
    if policy == "driver":
        return total // 10
    if policy == "firmware":
        return total - 2
    raise ValueError(f"unknown pre-trigger policy {policy!r}")


def trigger_offset(info: AnalyzerInfo, frequency: int, trigger_type: int) -> int:
    """Samples by which the driver moves pre/post to compensate pattern-trigger latency."""

    if trigger_type not in (TRIGGER_COMPLEX, TRIGGER_FAST):
        return 0
    delay = FAST_TRIGGER_DELAY if trigger_type == TRIGGER_FAST else COMPLEX_TRIGGER_DELAY
    sample_ns = 1e9 / frequency
    delay_ns = (1.0 / info.max_frequency) * 1e9 * delay
    return int(round(delay_ns / sample_ns + 0.3))


@dataclass(frozen=True)
class CaptureRequest:
    trigger_type: int
    trigger_channel: int
    inverted_or_count: int
    trigger_value: int
    channels: tuple[int, ...]
    frequency: int
    pre_samples: int
    post_samples: int
    capture_mode: int
    loop_count: int = 0
    measure: int = 0

    def pack(self) -> bytes:
        channels = bytes(self.channels) + bytes(24 - len(self.channels))
        return struct.pack(REQUEST_FORMAT, self.trigger_type, self.trigger_channel, self.inverted_or_count,
                           self.trigger_value, channels, len(self.channels), self.frequency,
                           self.pre_samples, self.post_samples, self.loop_count, self.measure,
                           self.capture_mode)

    @classmethod
    def unpack(cls, data: bytes) -> "CaptureRequest":
        if len(data) != REQUEST_SIZE:
            raise ProtocolError("CAPTURE_REQUEST must be 48 bytes")
        (trigger_type, trigger, inverted, value, channels, count, frequency, pre, post, loops, measure,
         mode) = struct.unpack(REQUEST_FORMAT, data)
        if count > 24:
            raise ProtocolError("channel count above 24")
        return cls(trigger_type, trigger, inverted, value, tuple(channels[:count]), frequency, pre, post, mode,
                   loops, measure)

    def frame(self) -> bytes:
        return frame(bytes((CMD_CAPTURE,)) + self.pack())

    @property
    def total_samples(self) -> int:
        return self.pre_samples + self.post_samples * (self.loop_count + 1)

    def to_dict(self) -> dict:
        return {"trigger_type": self.trigger_type, "trigger_channel": self.trigger_channel,
                "inverted_or_count": self.inverted_or_count, "trigger_value": self.trigger_value,
                "channels": list(self.channels), "frequency": self.frequency,
                "pre_samples": self.pre_samples, "post_samples": self.post_samples,
                "capture_mode": self.capture_mode, "loop_count": self.loop_count, "measure": self.measure}


def build_request(info: AnalyzerInfo, *, channels: list[int], frequency: int, pre_samples: int,
                  post_samples: int, trigger_type: int, trigger_channel: int, trigger_count: int = 1,
                  trigger_value: int = 0, inverted: bool = False, pretrigger_policy: str = "driver") -> CaptureRequest:
    """Validate like the official driver and firmware, then apply latency compensation.

    ``pre_samples``/``post_samples`` are the user-visible split around the trigger;
    for pattern triggers the request sent to the firmware moves ``offset`` samples
    from post to pre, exactly as the official driver does.
    """

    if not channels or len(set(channels)) != len(channels) or any(not 0 <= c < info.channels for c in channels):
        raise ProtocolError("capture channels must be distinct indices below the analyzer channel count")
    if list(channels) != sorted(channels):
        raise ProtocolError("capture channels must be in ascending order (the firmware packs bits in request order)")
    mode = capture_mode(channels)
    total = buffer_samples(info, mode)
    if not info.min_frequency <= frequency <= info.max_frequency:
        raise ProtocolError(f"sample rate {frequency} Hz outside {info.min_frequency}..{info.max_frequency} Hz")
    if pre_samples < 2 or post_samples < 2:
        raise ProtocolError("pre/post-trigger samples must be at least 2")
    if pre_samples > pretrigger_limit(info, mode, pretrigger_policy):
        raise ProtocolError(f"pre-trigger samples {pre_samples} exceed the {pretrigger_policy} limit "
                            f"{pretrigger_limit(info, mode, pretrigger_policy)}")
    if pre_samples + post_samples > total:
        raise ProtocolError(f"{pre_samples + post_samples} samples exceed the buffer ({total} samples in this mode)")
    if trigger_type == TRIGGER_EDGE:
        if not 0 <= trigger_channel <= min(info.channels, EXT_TRIGGER_CHANNEL):
            raise ProtocolError("edge trigger channel out of range")
        count_field = 1 if inverted else 0
        value = 0
    elif trigger_type == TRIGGER_COMPLEX:
        if not (1 <= trigger_count <= 16 and 0 <= trigger_channel <= 15 and trigger_channel + trigger_count <= 16):
            raise ProtocolError("pattern trigger must use 1..16 contiguous channels within ch0..ch15")
        if not 0 <= trigger_value < (1 << trigger_count):
            raise ProtocolError("pattern trigger value has bits outside the trigger channels")
        count_field = trigger_count
        value = trigger_value
    else:
        raise ProtocolError("only edge(level) and pattern triggers are planned")
    offset = trigger_offset(info, frequency, trigger_type)
    if post_samples - offset < 2:
        raise ProtocolError("post-trigger samples too small for the trigger latency compensation")
    return CaptureRequest(trigger_type, trigger_channel, count_field, value, tuple(channels), frequency,
                          pre_samples + offset, post_samples - offset, mode)


def expected_capture_bytes(request: CaptureRequest) -> int:
    """Bytes the firmware sends after CAPTURE_STARTED for a non-burst capture (stamp count 0)."""

    return 4 + request.total_samples * BYTES_PER_SAMPLE[request.capture_mode] + 1


def parse_capture(data: bytes, request: CaptureRequest) -> list[int]:
    """Decode the binary capture; samples carry channel ``request.channels[i]`` in bit ``i``."""

    width = BYTES_PER_SAMPLE[request.capture_mode]
    if len(data) < 4:
        raise ProtocolError("capture shorter than its length field")
    (count,) = struct.unpack_from("<I", data, 0)
    if count != request.total_samples:
        raise ProtocolError(f"analyzer sent {count} samples, requested {request.total_samples}")
    end = 4 + count * width
    if len(data) < end + 1:
        raise ProtocolError("capture truncated before the timestamp count")
    stamps = data[end]
    tail = 4 * stamps if stamps > 1 else 0
    if len(data) != end + 1 + tail:
        raise ProtocolError(f"capture has {len(data) - end - 1 - tail:+d} unexpected trailing bytes")
    if stamps > 1:
        raise ProtocolError("burst timestamps received for a single capture")
    fmt = {1: "B", 2: "H", 4: "I"}[width]
    samples = list(struct.unpack_from(f"<{count}{fmt}", data, 4))
    valid = (1 << len(request.channels)) - 1
    if any(sample & ~valid for sample in samples):
        raise ProtocolError("samples carry bits above the requested channel count")
    return samples
