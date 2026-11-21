"""Local analyzer binding and per-invocation capture requests (strict schema 1 JSON).

Neither belongs in the portable runner project: ``capture.enabled`` stays
false there. The analyzer binding is a machine-local file like the STPG device
binding; a capture request is one invocation's intent and is recorded with its
result.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from stpg_runner import physical_identity_key
from stpg_runner.artifacts import RunnerError, read_json

from .address import Address
from .la_protocol import AnalyzerInfo, ProtocolError
from .wiring import STPG_DUT_GPIO, Wiring

ANALYZER_KIND = "stpg_capture_analyzer"
REQUEST_KIND = "stpg_capture_request"
MARKER_SIGNAL = "STPG_MARKER"
STRATEGIES = ("auto", "pattern", "level", "marker")


class CaptureConfigError(RunnerError):
    pass


def _exact(value: Any, required: set[str], optional: set[str], label: str) -> dict:
    if not isinstance(value, dict):
        raise CaptureConfigError(f"{label} must be an object")
    missing, unknown = required - value.keys(), value.keys() - required - optional
    if missing or unknown:
        raise CaptureConfigError(f"{label}: missing={sorted(missing)}, unknown={sorted(unknown)}")
    return value


def _text(value: Any, label: str) -> str:
    if (not isinstance(value, str) or not value.strip() or value != value.strip()
            or any(ord(ch) < 32 for ch in value)):
        raise CaptureConfigError(f"{label} must be a nonempty, exact string")
    return value


@dataclass(frozen=True)
class AnalyzerConfig:
    port: str
    vid: int
    pid: int
    serial_number: str
    limits: AnalyzerInfo
    wiring: Wiring
    marker_gpio: int | None  # default pin offered for new marker derivations

    @classmethod
    def from_dict(cls, value: dict) -> "AnalyzerConfig":
        _exact(value, {"kind", "schema_version", "port", "vid", "pid", "serial_number", "limits", "wiring"},
               {"marker_gpio"}, "analyzer config")
        if value["kind"] != ANALYZER_KIND or type(value["schema_version"]) is not int or value["schema_version"] != 1:
            raise CaptureConfigError("unsupported analyzer config kind/schema_version")
        port, serial = _text(value["port"], "analyzer.port"), _text(value["serial_number"], "analyzer.serial_number")
        for key in ("vid", "pid"):
            if type(value[key]) is not int or not 1 <= value[key] <= 0xFFFF:
                raise CaptureConfigError(f"analyzer.{key} must be an integer in 1..65535")
        try:
            limits = AnalyzerInfo.from_dict(value["limits"])
            wiring = Wiring.from_config(value["wiring"])
        except (ProtocolError, ValueError) as exc:
            raise CaptureConfigError(str(exc)) from exc
        marker = value.get("marker_gpio")
        if marker is not None:
            if type(marker) is not int or marker not in STPG_DUT_GPIO:
                raise CaptureConfigError("marker_gpio must be an STPG DUT GPIO number (2..18, 20..23, 26..28)")
            if wiring.channel(marker) is None:
                raise CaptureConfigError(f"marker GPIO{marker} is not wired to an analyzer channel")
        return cls(port, value["vid"], value["pid"], serial, limits, wiring, marker)

    def to_dict(self) -> dict:
        out = {"kind": ANALYZER_KIND, "schema_version": 1, "port": self.port, "vid": self.vid, "pid": self.pid,
               "serial_number": self.serial_number, "limits": self.limits.to_dict(),
               "wiring": self.wiring.to_config()}
        if self.marker_gpio is not None:
            out["marker_gpio"] = self.marker_gpio
        return out

    @property
    def identity(self) -> tuple[int, int, str]:
        return (self.vid, self.pid, self.serial_number)

    @property
    def lease_key(self) -> str:
        # The SDK lease derives this itself: one lock per physical USB identity, whatever the role.
        return physical_identity_key(self)


def load_analyzer_config(path: Path) -> AnalyzerConfig:
    return AnalyzerConfig.from_dict(read_json(Path(path)))


@dataclass(frozen=True)
class CaptureRequestSpec:
    address: Address
    before_vectors: int
    after_vectors: int
    signals: tuple[str, ...] | None
    strategy: str
    sample_rate_hz: int | None
    pretrigger_policy: str

    @classmethod
    def from_dict(cls, value: dict) -> "CaptureRequestSpec":
        _exact(value, {"kind", "schema_version", "address", "before_vectors", "after_vectors", "strategy"},
               {"signals", "sample_rate_hz", "pretrigger_policy"}, "capture request")
        if value["kind"] != REQUEST_KIND or type(value["schema_version"]) is not int or value["schema_version"] != 1:
            raise CaptureConfigError("unsupported capture request kind/schema_version")
        address = Address.from_dict(value["address"])
        for key in ("before_vectors", "after_vectors"):
            if type(value[key]) is not int or not 0 <= value[key] <= 10_000_000:
                raise CaptureConfigError(f"{key} must be an integer in 0..10000000")
        if value["strategy"] not in STRATEGIES:
            raise CaptureConfigError(f"strategy must be one of {STRATEGIES}")
        signals = value.get("signals")
        if signals is not None:
            if (not isinstance(signals, list) or not signals or len(set(signals)) != len(signals)
                    or any(not isinstance(name, str) or not name for name in signals)):
                raise CaptureConfigError("signals must be null or a non-empty list of distinct signal names")
            signals = tuple(signals)
        rate = value.get("sample_rate_hz")
        if rate is not None and (type(rate) is not int or rate <= 0):
            raise CaptureConfigError("sample_rate_hz must be null or a positive integer")
        policy = value.get("pretrigger_policy", "driver")
        if policy not in ("driver", "firmware"):
            raise CaptureConfigError('pretrigger_policy must be "driver" (10 % of the buffer, as the official '
                                     'software) or "firmware" (any split the firmware accepts)')
        return cls(address, value["before_vectors"], value["after_vectors"], signals, value["strategy"], rate, policy)

    def to_dict(self) -> dict:
        out = {"kind": REQUEST_KIND, "schema_version": 1, "address": self.address.to_dict(),
               "before_vectors": self.before_vectors, "after_vectors": self.after_vectors,
               "strategy": self.strategy, "signals": list(self.signals) if self.signals is not None else None,
               "sample_rate_hz": self.sample_rate_hz, "pretrigger_policy": self.pretrigger_policy}
        return out


def load_capture_request(path: Path) -> CaptureRequestSpec:
    return CaptureRequestSpec.from_dict(read_json(Path(path)))
