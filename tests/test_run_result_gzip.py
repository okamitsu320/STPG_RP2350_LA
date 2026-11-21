"""Companion identity links use the core's strict legacy/gzip result resolver."""
import hashlib

import pytest

from stpg_capture import coordinator
from stpg_runner.artifacts import RunnerError, write_json


@pytest.mark.parametrize("name", ["run.json", "run.json.gz"])
def test_capture_identity_names_and_hashes_stored_run_bytes(tmp_path, monkeypatch, name):
    monkeypatch.setattr(coordinator, "plan_digest", lambda value: "0" * 64)
    class Analyzer:
        def to_dict(self):
            return {}
    path = tmp_path / name
    write_json(path, {"kind": "stpg_runner_run_result", "schema_version": 1})
    result = coordinator.capture_result(plan={"bundle": {"manifest_sha256": "1" * 64}}, outcome="COMPLETED",
        analyzer=Analyzer(), bundle_dir=tmp_path, request=None, journal=[], trace=None, alignment=None,
        run_dir=tmp_path, run_summary=None, errors=[], mode="simulated", samples=None)
    assert result["run"]["result"] == {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def test_companion_does_not_choose_one_of_two_run_records(tmp_path, monkeypatch):
    for name in ("run.json", "run.json.gz"):
        write_json(tmp_path / name, {"kind": "stpg_runner_run_result", "schema_version": 1})
    monkeypatch.setattr(coordinator, "plan_digest", lambda value: "0" * 64)
    with pytest.raises(RunnerError, match="ambiguous"):
        coordinator.capture_result(plan={}, outcome="COMPLETED", analyzer=None, bundle_dir=tmp_path,
            request=None, journal=[], trace=None, alignment=None, run_dir=tmp_path,
            run_summary=None, errors=[], mode="simulated", samples=None)
