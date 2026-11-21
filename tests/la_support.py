"""Test support of this tool: only installed packages, no file of the core's own test folder.

The hardware guard mirrors what any host-only test of the STPG tools must forbid: real serial opens and USB
enumeration, for the STPG, the GPIO controller and the analyzer, and starting a debugger or flashing tool. The peers
are the simulated STPG and GPIO controller that the core package ships for its demo mode.
"""

from __future__ import annotations

import os
from pathlib import PureWindowsPath
import re
import subprocess

from stpg_runner import ControllerConfig
from stpgview.runner import simulated
from stpgview.runner.simulated import SIMULATED_DEVICE as DEVICE, SimulatedController as Peer, simulated_ports as ports

CONFIG = ControllerConfig("MOCK-GPIO", 0x2E8A, 0x000A, "PUBLIC-GPIO-0001", "rp2040-gpio-v1", allow_high=(14, 26))
# The same programs the core's test setup refuses to start.
DEBUGGERS = frozenset({"openocd", "gdb", "gdb-multiarch", "arm-none-eabi-gdb", "picotool", "pyocd"})
__all__ = ["CONFIG", "DEBUGGERS", "DEVICE", "Peer", "controller_ports", "forbid_hardware", "ports", "starts_debugger"]


def controller_ports(config=CONFIG):
    return simulated.controller_ports(config)


def _name(program) -> str:
    text = os.fsdecode(program) if isinstance(program, (str, bytes, os.PathLike)) else str(program)
    return PureWindowsPath(text).name.lower().removesuffix(".exe")  # either kind of slash


def starts_debugger(args, positional=(), kwargs=None) -> bool:
    """Whether a Popen call could start one of DEBUGGERS: its program, ``executable=`` or any word of a command line.

    A command line (a string, or the first item with ``shell=True``) is checked word by word, so a debugger named
    after a ``cd … &&`` is found too. This may refuse a harmless command that only mentions a debugger; a test never
    needs one.
    """

    kwargs = kwargs or {}
    executable = kwargs.get("executable", positional[1] if len(positional) > 1 else None)
    names = [executable] if executable is not None else []
    if isinstance(args, (str, bytes)) or (kwargs.get("shell") and isinstance(args, (list, tuple)) and args):
        line = os.fsdecode(args if isinstance(args, (str, bytes)) else args[0])
        names += [word for word in re.split(r"[\s;&|()<>`'\"]+", line) if word]
    elif isinstance(args, os.PathLike):
        names.append(args)
    elif isinstance(args, (list, tuple)) and args:
        names.append(args[0])
    return any(_name(name) in DEBUGGERS for name in names)


def forbid_hardware(monkeypatch) -> None:
    import serial
    from serial.tools import list_ports

    import stpg_runner.accessories as accessories
    import stpg_runner.controller as controller
    import stpg_runner.device as device
    import stpg_runner.execution as execution
    import stpg_runner.external_accessory as external

    from stpg_capture import la_device

    def forbidden(*args, **kwargs):
        raise AssertionError("host-only test attempted physical contact")

    monkeypatch.setattr(serial, "Serial", forbidden)
    monkeypatch.setattr(list_ports, "comports", lambda *args, **kwargs: [])
    for module in (execution, device):
        monkeypatch.setattr(module, "open_serial", forbidden)
        monkeypatch.setattr(module, "enumerate_ports", forbidden)
    for module in (accessories, external, controller, la_device):
        monkeypatch.setattr(module, "enumerate_ports", forbidden)
    monkeypatch.setattr(controller, "open_controller_serial", forbidden)
    monkeypatch.setattr(la_device, "open_analyzer_serial", forbidden)

    lower = subprocess.Popen  # whatever starts processes now; tests of this guard put a fake here first

    def host_only_process(args, *positional, **kwargs):
        if starts_debugger(args, positional, kwargs):
            forbidden()
        return lower(args, *positional, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", host_only_process)
