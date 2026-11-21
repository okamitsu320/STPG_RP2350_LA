"""Analyzer traces: the native capture format plus offline import of CLCapture CSV and .lac files."""

from __future__ import annotations

import base64
import binascii
import csv
from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
import re

from stpg_runner.artifacts import RunnerError


class TraceError(RunnerError):
    pass


@dataclass
class LaTrace:
    """Samples with bit ``i`` = analyzer channel ``channels[i]`` (0-based)."""

    sample_rate_hz: int
    channels: tuple[int, ...]
    samples: list[int]
    trigger_index: int  # sample index of the trigger moment (the user-visible pre-trigger count)
    source: dict = field(default_factory=dict)
    names: tuple[str, ...] = ()
    period_ps: int | None = None  # exact period when known (analyzer max rate / integer divider)

    @property
    def sample_period_ps(self) -> int:
        return self.period_ps if self.period_ps else round(10**12 / self.sample_rate_hz)

    def bit_of(self, channel: int) -> int | None:
        return self.channels.index(channel) if channel in self.channels else None

    def edges(self, bit: int) -> list[tuple[int, int]]:
        """(sample index, new level) for every change of one bit."""

        out = []
        mask = 1 << bit
        prev = self.samples[0] & mask if self.samples else 0
        for index in range(1, len(self.samples)):
            now = self.samples[index] & mask
            if now != prev:
                out.append((index, 1 if now else 0))
                prev = now
        return out

    def level(self, bit: int, index: int) -> int:
        return self.samples[index] >> bit & 1

    def summary(self) -> dict:
        return {"sample_rate_hz": self.sample_rate_hz, "sample_period_ps": self.sample_period_ps,
                "channels": [c + 1 for c in self.channels],
                "samples": len(self.samples), "trigger_index": self.trigger_index, "source": self.source,
                "names": list(self.names)}


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _channel_from_name(name: str) -> int | None:
    match = re.fullmatch(r"(?:channel|ch)\s*([0-9]+)", name.strip(), re.IGNORECASE)
    return int(match.group(1)) - 1 if match else None


def import_csv(path: Path, *, sample_rate_hz: int, trigger_index: int,
               channels: list[int] | None = None) -> LaTrace:
    """CLCapture CSV: a header of channel names, then one 0/1 row per sample.

    The CSV has neither the sample rate nor the trigger position, so both are
    explicit inputs. ``channels`` are 1-based analyzer channel numbers in column
    order; without them the header must read "Channel N".
    """

    if type(sample_rate_hz) is not int or not 1 <= sample_rate_hz <= 10**12:
        raise TraceError("sample rate must be a positive integer with a nonzero picosecond period")
    if type(trigger_index) is not int:
        raise TraceError("trigger index must be an integer")
    path = Path(path)
    with path.open(newline="", encoding="utf-8") as stream:
        rows = list(csv.reader(stream))
    if len(rows) < 2:
        raise TraceError(f"{path.name}: no samples")
    header = [name.strip() for name in rows[0]]
    if channels is None:
        parsed = [_channel_from_name(name) for name in header]
        if any(ch is None or not 0 <= ch < 24 for ch in parsed):
            raise TraceError(f"{path.name}: header {header} does not name analyzer channels; "
                             "give the channel numbers in column order")
        chans = [ch for ch in parsed if ch is not None]
    else:
        if len(channels) != len(header) or any(type(c) is not int or not 1 <= c <= 24 for c in channels):
            raise TraceError("give one analyzer channel number (1..24) per CSV column")
        chans = [c - 1 for c in channels]
    if len(set(chans)) != len(chans):
        raise TraceError("a channel appears twice")
    order = sorted(range(len(chans)), key=lambda i: chans[i])
    samples = []
    for number, row in enumerate(rows[1:], 2):
        if len(row) != len(header) or any(cell not in ("0", "1") for cell in row):
            raise TraceError(f"{path.name}:{number}: expected {len(header)} columns of 0/1")
        value = 0
        for bit, column in enumerate(order):
            if row[column] == "1":
                value |= 1 << bit
        samples.append(value)
    if not 0 <= trigger_index < len(samples):
        raise TraceError("trigger index outside the capture")
    return LaTrace(sample_rate_hz, tuple(chans[i] for i in order), samples, trigger_index,
                   {"kind": "csv", "path": str(path), "sha256": _sha(path)}, tuple(header[i] for i in order))


def import_lac(path: Path) -> LaTrace:
    """LogicAnalyzer desktop/CLCapture .lac (JSON ExportedCapture with per-channel base64 samples)."""

    path = Path(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        settings = data["Settings"]
        frequency = settings["Frequency"]
        pre = settings["PreTriggerSamples"]
        loops = settings.get("LoopCount") or 0
        channels = settings["CaptureChannels"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise TraceError(f"{path.name}: not a LogicAnalyzer capture: {exc}") from exc
    if loops:
        raise TraceError(f"{path.name}: burst captures (LoopCount={loops}) are not supported")
    if (type(frequency) is not int or not 1 <= frequency <= 10**12 or type(pre) is not int
            or not isinstance(channels, list) or not channels):
        raise TraceError(f"{path.name}: invalid sample rate, pre-trigger count or channel list")
    columns = []
    for item in channels:
        if (not isinstance(item, dict) or type(item.get("ChannelNumber")) is not int
                or not 0 <= item["ChannelNumber"] < 24):
            raise TraceError(f"{path.name}: invalid analyzer channel (expected 0..23)")
        number = item["ChannelNumber"]
        raw = item.get("Samples")
        if isinstance(raw, str):
            try:
                levels = base64.b64decode(raw, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise TraceError(f"{path.name}: malformed base64 samples") from exc
        elif isinstance(raw, list):
            if any(type(v) is not int or v not in (0, 1) for v in raw):
                raise TraceError(f"{path.name}: non-binary samples")
            levels = bytes(raw)
        else:
            raise TraceError(f"{path.name}: channel {number + 1} has no samples (legacy format?)")
        if any(v not in (0, 1) for v in levels):
            raise TraceError(f"{path.name}: non-binary samples")
        columns.append((number, item.get("ChannelName") or f"Channel {number + 1}", levels))
    if len({c[0] for c in columns}) != len(columns):
        raise TraceError(f"{path.name}: a channel appears twice")
    columns.sort(key=lambda c: c[0])
    length = len(columns[0][2])
    if any(len(c[2]) != length for c in columns) or length == 0:
        raise TraceError(f"{path.name}: channels have different sample counts")
    samples = [0] * length
    for bit, (_, _, levels) in enumerate(columns):
        for index, level in enumerate(levels):
            if level:
                samples[index] |= 1 << bit
    if not 0 <= pre < length:
        raise TraceError(f"{path.name}: pre-trigger count outside the capture")
    return LaTrace(frequency, tuple(c[0] for c in columns), samples, pre,
                   {"kind": "lac", "path": str(path), "sha256": _sha(path)}, tuple(c[1] for c in columns))


def save_native(trace: LaTrace, directory: Path) -> dict:
    """Write samples (little-endian u32 per sample) and return their descriptor."""

    directory.mkdir(parents=True, exist_ok=True)
    target = directory / "capture.samples.u32"
    payload = b"".join(value.to_bytes(4, "little") for value in trace.samples)
    target.write_bytes(payload)
    return {"file": target.name, "format": "u32le", "count": len(trace.samples),
            "sha256": hashlib.sha256(payload).hexdigest()}


def load_native(directory: Path, descriptor: dict, *, sample_rate_hz: int, channels: list[int],
                trigger_index: int, source: dict | None = None, period_ps: int | None = None) -> LaTrace:
    target = Path(directory) / descriptor["file"]
    payload = target.read_bytes()
    if hashlib.sha256(payload).hexdigest() != descriptor["sha256"] or len(payload) != 4 * descriptor["count"]:
        raise TraceError(f"{target.name}: samples do not match their recorded hash/size")
    samples = [int.from_bytes(payload[i:i + 4], "little") for i in range(0, len(payload), 4)]
    return LaTrace(sample_rate_hz, tuple(channels), samples, trigger_index, source or {"kind": "native"},
                   period_ps=period_ps)
