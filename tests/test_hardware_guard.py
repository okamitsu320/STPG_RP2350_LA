"""The host-only guard of these tests: serial opens, port listing and debugger starts stop in the guard (host only).

Every check that could reach hardware first puts an independent fake below the guard: a fake serial constructor, a
fake port list and a fake process starter, which only record the call. The guard is applied on top of that fake.
With the guard removed, a call reaches the fake and nothing else, so no port is opened or listed and no debugger is
started even if the guard is broken. Only ordinary Python children are started for real, as positive controls.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
import subprocess
import sys

import pytest
import serial
from serial.tools import list_ports

from la_support import DEBUGGERS, forbid_hardware, starts_debugger

pytestmark = pytest.mark.stpg_host_only


class Barrier:
    """The fake layer below the guard: it records what got past the guard and never calls the OS."""

    def __init__(self):
        self.calls: list[tuple[str, object]] = []

    def serial(self, *args, **kwargs):
        self.calls.append(("serial", args))
        return object()

    def ports(self, *args, **kwargs):
        self.calls.append(("ports", args))
        return []

    def process(self, args, *positional, **kwargs):
        self.calls.append(("process", args))
        raise OSError("fake process layer: nothing was started")  # what a missing program looks like


@pytest.fixture
def barrier(monkeypatch):
    """The fake layer in place of the real one (this also takes away the guard of the test setup)."""

    lower = Barrier()
    monkeypatch.setattr(serial, "Serial", lower.serial)
    monkeypatch.setattr(list_ports, "comports", lower.ports)
    monkeypatch.setattr(subprocess, "Popen", lower.process)
    return lower


@pytest.fixture
def guarded(barrier):
    """The guard of the test setup, applied on top of the fake layer; undone before the fake layer is."""

    with pytest.MonkeyPatch.context() as patch:
        forbid_hardware(patch)
        assert subprocess.Popen is not barrier.process and serial.Serial is not barrier.serial
        yield barrier
    assert subprocess.Popen == barrier.process  # back on the fake layer, never on the real one


DEBUGGER_CALLS = {
    "argv": (["openocd", "-f", "interface/cmsis-dap.cfg"], {}),
    "absolute path": (["/usr/bin/openocd"], {}),
    "windows path": ([r"C:\Program Files\OpenOCD\bin\OpenOCD.EXE"], {}),
    "tuple": (("gdb", "-batch"), {}),
    "path object": ([Path("/opt/pico/bin/picotool"), "info"], {}),
    "path as args": (Path("/usr/local/bin/pyocd"), {}),
    "string": ("openocd", {}),
    "shell line": ("openocd -f board.cfg", {"shell": True}),
    "shell line after cd": ("cd build && 'arm-none-eabi-gdb' -batch", {"shell": True}),
    "shell list": (["gdb-multiarch -batch"], {"shell": True}),
    "bytes shell line": (b"picotool info", {"shell": True}),
    "executable keyword": ([sys.executable, "-c", "pass"], {"executable": "/usr/bin/gdb"}),
}


@pytest.mark.parametrize("name", sorted(DEBUGGER_CALLS))
def test_a_debugger_start_stops_in_the_guard(guarded, name):
    args, kwargs = DEBUGGER_CALLS[name]
    with pytest.raises(AssertionError, match="physical contact"):
        subprocess.Popen(args, **kwargs)
    assert guarded.calls == []


@pytest.mark.parametrize("program", sorted(DEBUGGERS) + [name.upper() + ".exe" for name in sorted(DEBUGGERS)])
def test_every_listed_debugger_stops_in_the_guard(guarded, program):
    with pytest.raises(AssertionError, match="physical contact"):
        subprocess.Popen([program, "--version"])
    assert guarded.calls == []


def test_the_executable_given_by_position_is_checked_too(guarded):
    with pytest.raises(AssertionError, match="physical contact"):
        subprocess.Popen([sys.executable], -1, "/usr/bin/openocd")
    assert guarded.calls == []


@pytest.mark.parametrize("start", [
    lambda: subprocess.run(["openocd", "--version"], capture_output=True),
    lambda: subprocess.check_output(["picotool", "info"]),
    lambda: subprocess.call(["pyocd", "list"]),
    lambda: subprocess.check_call(["gdb", "-batch"]),
    lambda: asyncio.run(asyncio.create_subprocess_exec("openocd", "--version")),
], ids=["run", "check_output", "call", "check_call", "asyncio"])
def test_the_helpers_that_start_processes_stop_too(guarded, start):
    with pytest.raises(AssertionError, match="physical contact"):
        start()
    assert guarded.calls == []


def test_a_serial_open_stops_and_the_port_list_is_empty_without_asking_the_os(guarded):
    with pytest.raises(AssertionError, match="physical contact"):
        serial.Serial("COM1")
    assert list_ports.comports() == []
    assert guarded.calls == []


@pytest.mark.parametrize("args", [
    [sys.executable, "-I", "-c", "pass"],
    [sys.executable, "-m", "stpg_capture.ui.server", "--no-browser", "--no-device-io"],
    ["node", "-e", "process.exit(0)"],
    ["geckodriver", "--port", "0"],
    ["gdbus", "monitor"],  # a name that only starts like a debugger
    ["openocd-board-notes.txt"],
], ids=["python", "this tool", "node", "geckodriver", "similar name", "longer name"])
def test_an_ordinary_program_passes_the_guard_unchanged(guarded, args):
    with pytest.raises(OSError, match="fake process layer"):
        subprocess.Popen(args, stdout=subprocess.PIPE)
    assert guarded.calls == [("process", args)]


def test_without_the_guard_every_call_reaches_only_the_fake_layer(barrier):
    # The negative control of this file's method: the guard is gone, the fake layer still holds.
    assert subprocess.Popen == barrier.process and serial.Serial == barrier.serial
    with pytest.raises(OSError, match="fake process layer"):
        subprocess.Popen(["openocd", "--version"])
    serial.Serial("COM1")
    assert list_ports.comports() == []
    assert barrier.calls == [("process", ["openocd", "--version"]), ("serial", ("COM1",)), ("ports", ())]


def test_the_test_setup_puts_this_guard_on_every_test():
    # No fake layer here, so nothing that could reach hardware is called: only where the functions come from.
    assert subprocess.Popen.__module__ == serial.Serial.__module__ == list_ports.comports.__module__ == "la_support"


def test_a_real_python_child_still_works_under_the_guard_of_the_test_setup():
    done = subprocess.run([sys.executable, "-I", "-c", "import sys; sys.stdout.write(sys.stdin.read().upper())"],
                          input="stdio child", capture_output=True, text=True, timeout=60)
    assert done.returncode == 0 and done.stdout == "STDIO CHILD"


@pytest.mark.parametrize("args,kwargs,expected", [
    (["openocd"], {}, True),
    ([], {}, False),
    ("", {}, False),
    (["python3", "openocd"], {}, False),  # an argument, not the program
    (["python3"], {"shell": False, "executable": None}, False),
    (["echo ok"], {"shell": True}, False),
])
def test_the_decision_itself(args, kwargs, expected):
    assert starts_debugger(args, (), kwargs) is expected
