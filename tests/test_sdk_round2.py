"""SDK round-2 integration; public synthetic files and no device I/O."""
import shutil
from pathlib import Path

import pytest

from stpg_runner import load_external_accessory
from stpg_runner.artifacts import RunnerError
from stpg_capture import timeline, external_config
from test_accessory_process import counter, _write


def test_timeline_uses_only_public_sdk_verification(counter, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("companion called a private SDK alias")
    monkeypatch.setattr(timeline, "_reuse_invocation_plan", forbidden, raising=False)
    monkeypatch.setattr(timeline, "_verify_inventory", forbidden, raising=False)
    built = counter[0]
    loaded = timeline.load_bundle_timeline(built.bundle_dir, manifest_sha256=built.manifest_sha256)
    assert loaded.total_vectors == counter[1].total_vectors


def test_public_final_check_still_rejects_a_map_changed_during_decode(counter, tmp_path, monkeypatch):
    bundle = tmp_path / "bundle"
    shutil.copytree(counter[0].bundle_dir, bundle)
    maps = [path for path in bundle.rglob("*") if ".map.json" in path.name]
    assert maps
    original, changed = timeline.load_bin, []
    def decode(*args, **kwargs):
        trace = original(*args, **kwargs)
        if not changed:
            maps[0].write_bytes(maps[0].read_bytes() + b" ")
            changed.append(True)
        return trace
    monkeypatch.setattr(timeline, "load_bin", decode)
    with pytest.raises(RunnerError, match="bundle changed during timeline decoding"):
        timeline.load_bundle_timeline(bundle, manifest_sha256=counter[0].manifest_sha256)
    assert changed


@pytest.mark.parametrize("seconds,expected", [(0, 60), (120, 250), (1800, 3600)])
def test_open_allowance_is_separate_from_capture_wait(seconds, expected):
    assert external_config.open_limit(seconds) == expected


@pytest.mark.parametrize("seconds", [-1, True, float("nan"), float("inf"), 3600])
def test_invalid_or_excessive_validation_measurement_is_not_silently_accepted(seconds):
    with pytest.raises(RunnerError):
        external_config.open_limit(seconds)


def test_writer_declares_both_budgets_and_eof_grace(counter, tmp_path, monkeypatch):
    monkeypatch.setattr(external_config, "open_limit", lambda seconds: 120.0)
    info = _write(counter, tmp_path / "config", mode="simulated")
    spec = load_external_accessory(Path(info["config"]))
    assert spec.open_timeout_s == info["open_timeout_s"] == 120
    assert spec.close_timeout_s == info["close_timeout_s"] == 5
    assert spec.timeout_s == info["timeout_s"] <= 20


@pytest.mark.parametrize("options", [{"open_timeout_s": 3601}, {"open_timeout_s": True},
                                  {"close_timeout_s": None}, {"close_timeout_s": 21}])
def test_bad_phase_limit_does_not_leave_a_config_folder(counter, tmp_path, options):
    with pytest.raises(RunnerError):
        _write(counter, tmp_path / "never", **options)
    assert not (tmp_path / "never").exists()
