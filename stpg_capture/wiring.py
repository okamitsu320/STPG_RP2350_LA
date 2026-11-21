"""STPG logical bit -> STPG GPIO -> analyzer channel.

The STPG side follows ``firmware/src/board_rp2350a_16pin.c`` (16-pin mode uses
the first 16 entries). The analyzer side is a local bench fact, chosen in the
analyzer binding as a named preset or an explicit map:

* ``same-gpio`` (current bench): analyzer inputs GPIO2..22, 26..28 wired to the
  STPG GPIO with the same number. STPG GPIO23 (logical bit 20) has no analyzer channel.
* ``stpg20-23-to-la19-22`` (planned PCB, plain Pico 2 analyzer): STPG GPIO20..23
  wired to analyzer GPIO19..22, every other DUT GPIO to the same number, so all
  24 DUT bits are observable.

Presets are immutable once published: a board with different wiring gets a new
row in ``PRESETS`` (or an explicit map in its binding), never an edit of an old
row, so an existing binding cannot silently change meaning.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

from .la_protocol import CHANNEL_GPIO

STPG_DUT_GPIO = (2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17,
                 18, 20, 21, 22, 23, 26, 27, 28)

# Preset name -> {STPG GPIO: analyzer GPIO}. Add new rows; do not edit published ones.
PRESETS: Mapping[str, Mapping[int, int]] = MappingProxyType({
    "same-gpio": MappingProxyType({gpio: gpio for gpio in STPG_DUT_GPIO if gpio in CHANNEL_GPIO}),
    "stpg20-23-to-la19-22": MappingProxyType({
        **{gpio: gpio for gpio in STPG_DUT_GPIO if gpio < 20 or gpio > 23},
        20: 19, 21: 20, 22: 21, 23: 22}),
})
PRESET_TEXT = {
    "same-gpio": "current bench: analyzer GPIO n = STPG GPIO n (STPG GPIO23 / bit 20 not observable)",
    "stpg20-23-to-la19-22": "planned PCB: STPG GPIO20-23 -> analyzer GPIO19-22, others same number (24/24 observable)",
}


def stpg_gpio(bit: int, active_pins: int) -> int:
    if active_pins not in (16, 24):
        raise ValueError(f"unsupported active pin count {active_pins}")
    if not 0 <= bit < active_pins:
        raise ValueError(f"logical bit {bit} outside the {active_pins}-pin map")
    return STPG_DUT_GPIO[bit]


def stpg_bit_for_gpio(gpio: int) -> int | None:
    return STPG_DUT_GPIO.index(gpio) if gpio in STPG_DUT_GPIO else None


def _from_analyzer_gpio(mapping: Mapping[int, int]) -> dict[int, int]:
    out = {}
    for gpio, la_gpio in mapping.items():
        if type(gpio) is not int or gpio not in STPG_DUT_GPIO:
            raise ValueError(f"wiring: GPIO{gpio} is not an STPG DUT GPIO")
        if type(la_gpio) is not int or la_gpio not in CHANNEL_GPIO:
            raise ValueError(f"wiring: analyzer GPIO{la_gpio} is not an analyzer input (GPIO2..22, 26..28)")
        out[gpio] = CHANNEL_GPIO.index(la_gpio)
    if len(set(out.values())) != len(out):
        raise ValueError("two STPG GPIOs are wired to the same analyzer input")
    return out


def _gpio_key(value: object) -> int:
    # Do not normalize aliases such as 020 or Unicode digits: they can overwrite
    # another entry when converted to int, hiding part of the confirmed wiring.
    if not isinstance(value, str) or value not in {str(gpio) for gpio in STPG_DUT_GPIO}:
        raise ValueError("wiring keys must be canonical ASCII STPG GPIO numbers")
    return int(value)


@dataclass(frozen=True)
class Wiring:
    """STPG GPIO -> analyzer channel index (0-based; the analyzer UI shows index + 1).

    ``label`` is the preset name, ``"custom"`` (channel map) or ``"custom-gpio"``
    (analyzer-GPIO map); ``to_config`` writes the same form back.
    """

    gpio_to_channel: dict[int, int]
    label: str = "same-gpio"

    @classmethod
    def same_gpio(cls) -> "Wiring":
        return cls.preset("same-gpio")

    @classmethod
    def preset(cls, name: str) -> "Wiring":
        if not isinstance(name, str) or name not in PRESETS:
            raise ValueError(f"unknown wiring preset {name!r}; known: {sorted(PRESETS)}")
        return cls(_from_analyzer_gpio(PRESETS[name]), name)

    @classmethod
    def from_config(cls, value: object) -> "Wiring":
        if isinstance(value, str):
            return cls.preset(value)
        if isinstance(value, dict) and set(value) == {"analyzer_gpio"}:
            items = value["analyzer_gpio"]
            if not isinstance(items, dict) or not items or any(
                    type(v) is not int for v in items.values()):
                raise ValueError('wiring {"analyzer_gpio": {"<stpg gpio>": <analyzer gpio>, ...}} expects integers')
            return cls(_from_analyzer_gpio({_gpio_key(k): v for k, v in items.items()}), "custom-gpio")
        if not isinstance(value, dict) or not value:
            raise ValueError(f'wiring must be a preset name {sorted(PRESETS)}, {{"analyzer_gpio": {{...}}}} '
                             'or an object {"<stpg gpio>": <analyzer channel 1..24>}')
        mapping: dict[int, int] = {}
        for key, channel in value.items():
            if type(channel) is not int:
                raise ValueError("wiring keys are STPG GPIO numbers (strings), values analyzer channels 1..24")
            gpio = _gpio_key(key)
            if gpio not in STPG_DUT_GPIO or not 1 <= channel <= 24:
                raise ValueError(f"wiring GPIO{gpio} -> channel {channel} is out of range")
            mapping[gpio] = channel - 1
        if len(set(mapping.values())) != len(mapping):
            raise ValueError("two STPG GPIOs are wired to the same analyzer channel")
        return cls(mapping, "custom")

    def to_config(self) -> object:
        if (not self.gpio_to_channel or any(type(gpio) is not int or gpio not in STPG_DUT_GPIO
                or type(channel) is not int or not 0 <= channel < len(CHANNEL_GPIO)
                for gpio, channel in self.gpio_to_channel.items())
                or len(set(self.gpio_to_channel.values())) != len(self.gpio_to_channel)):
            raise ValueError("invalid or ambiguous wiring channel map")
        if self.label in PRESETS:
            if self.gpio_to_channel != _from_analyzer_gpio(PRESETS[self.label]):
                raise ValueError("preset label differs from its actual wiring; select an explicit custom map")
            return self.label
        if self.label == "custom-gpio":
            return {"analyzer_gpio": {str(gpio): CHANNEL_GPIO[channel]
                                      for gpio, channel in sorted(self.gpio_to_channel.items())}}
        return {str(gpio): channel + 1 for gpio, channel in sorted(self.gpio_to_channel.items())}

    def channel(self, gpio: int) -> int | None:
        return self.gpio_to_channel.get(gpio)

    def rows(self) -> list[dict]:
        """Every STPG DUT GPIO with its logical bit and analyzer input (None when not wired), for display."""

        self.to_config()  # reject a malformed/mutated mapping before displaying it
        out = []
        for bit, gpio in enumerate(STPG_DUT_GPIO):
            channel = self.gpio_to_channel.get(gpio)
            out.append({"stpg_bit": bit, "stpg_gpio": gpio,
                        "analyzer_gpio": CHANNEL_GPIO[channel] if channel is not None else None,
                        "analyzer_channel": channel + 1 if channel is not None else None})
        return out
