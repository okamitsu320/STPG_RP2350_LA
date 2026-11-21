"""Test setup of the logic-analyzer tool (host only): every test forbids real serial and USB access.

The tests use the installed core package and this tool, nothing from a core source checkout.
"""

from __future__ import annotations

import pytest

from la_support import forbid_hardware


def pytest_configure(config):
    config.addinivalue_line("markers", "stpg_host_only: test uses host-side code and does not perform physical "
                            "device I/O")


@pytest.fixture(autouse=True)
def _no_hardware(monkeypatch):
    forbid_hardware(monkeypatch)
