"""The path picker's server side: listing with home and roots, and the check of a new output folder (host only).

Nothing here creates a folder: a new folder is an existing parent plus a checked name, made only when the tool
writes into it. Windows drives are checked with a model of the drive list on this host; that is not a native run.
"""

from __future__ import annotations

import os
from pathlib import Path
import sys

import pytest

from stpg_capture.ui.service import LaService, folder_name_problem, path_roots
from stpg_runner.artifacts import RunnerError

pytestmark = pytest.mark.stpg_host_only


def tree(root: Path) -> list[str]:
    return sorted(str(p.relative_to(root)) for p in root.rglob("*"))


def test_a_listing_names_home_and_roots_and_never_opens_another_folder_instead(tmp_path, monkeypatch):
    # The empty-path branch lists home: never inspect the test runner's real
    # home directory (or settings) just to check this UI default.
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    (tmp_path / "sub").mkdir()
    listed = LaService.listing({"path": str(tmp_path)})
    assert listed["path"] == str(tmp_path.resolve()) and listed["home"] == str(Path.home())
    assert [e["name"] for e in listed["entries"]] == ["home", "sub"] and listed["roots"] == path_roots()
    with pytest.raises(RunnerError, match="is not a folder"):
        LaService.listing({"path": str(tmp_path / "missing")})  # an error, not the home folder
    assert LaService.listing({"path": ""})["path"] == str(Path.home().resolve())  # empty means home, by choice


def test_roots_on_windows_are_every_logical_drive_and_elsewhere_slash():
    # A model of the drive list: a native Windows run is a separate check.
    letters = [f"{letter}:\\" for letter in "AZDCK"]
    assert path_roots("nt", letters) == ["A:\\", "C:\\", "D:\\", "K:\\", "Z:\\"]
    assert path_roots("posix") == ["/"]
    if os.name == "nt":  # native: whatever drives exist now, including ones past J:
        assert path_roots() and all(len(root) >= 2 and root[1] == ":" for root in path_roots())


@pytest.mark.parametrize("name", ["run 1", "日本語のフォルダ", "capture-2026.11.08", "a" * 100])
def test_a_new_folder_is_a_parent_and_a_name_and_nothing_is_created(tmp_path, name):
    before = tree(tmp_path)
    chosen = LaService.new_folder({"parent": str(tmp_path), "name": name})
    assert chosen == {"path": str(tmp_path.resolve() / name), "parent": str(tmp_path.resolve()), "name": name,
                      "created": False}
    assert tree(tmp_path) == before


@pytest.mark.parametrize("name,why", [
    ("", "type a name"), ("   ", "type a name"), (".", "not a single folder name"), ("..", "not a single folder name"),
    ("a/b", "not a single folder name"), ("a\\b", "not a single folder name"), ("c:x", "not a single folder name"),
    ("bad?", "not a single folder name"), ("tab\tname", "not a single folder name"),
    (" lead", "start or end with a space"), ("trail ", "start or end with a space"), ("dot.", "end with a dot"),
    ("CON", "reserved device name"), ("con.txt", "reserved device name"), ("Lpt9", "reserved device name"),
    ("nul.json", "reserved device name"),
])
def test_an_unusable_name_is_refused_before_anything_is_written(tmp_path, name, why):
    with pytest.raises(RunnerError, match=why):
        LaService.new_folder({"parent": str(tmp_path), "name": name})
    assert tree(tmp_path) == []


@pytest.mark.parametrize("existing,typed", [("run", "run"), ("Run", "run"), ("RUN", "rUn"), ("file.txt", "FILE.txt")])
def test_a_name_that_exists_in_any_case_is_refused(tmp_path, existing, typed):
    (tmp_path / existing).mkdir() if "." not in existing else (tmp_path / existing).write_text("kept")
    before = tree(tmp_path)
    with pytest.raises(RunnerError, match="already exists"):
        LaService.new_folder({"parent": str(tmp_path), "name": typed})
    assert tree(tmp_path) == before


def test_the_parent_must_be_an_existing_folder_and_absolute(tmp_path):
    for parent, why in ((str(tmp_path / "missing"), "is not a folder"), ("relative/folder", "choose the folder"),
                        ("", "choose the folder"), (None, "choose the folder")):
        with pytest.raises(RunnerError, match=why):
            LaService.new_folder({"parent": parent, "name": "run"})
    (tmp_path / "file").write_text("x")
    with pytest.raises(RunnerError, match="is not a folder"):
        LaService.new_folder({"parent": str(tmp_path / "file"), "name": "run"})


@pytest.mark.skipif(sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0),
                    reason="needs POSIX permissions and a non-root user")
def test_an_unreadable_parent_is_reported_not_treated_as_empty(tmp_path):
    locked = tmp_path / "locked"
    locked.mkdir()
    os.chmod(locked, 0)
    try:
        with pytest.raises(RunnerError, match="cannot list"):
            LaService.new_folder({"parent": str(locked), "name": "run"})
        with pytest.raises(RunnerError, match="cannot list"):
            LaService.listing({"path": str(locked)})
    finally:
        os.chmod(locked, 0o755)


def test_folder_name_problem_accepts_what_new_folder_accepts():
    assert folder_name_problem("run 1") is None and folder_name_problem("日本語") is None
    assert folder_name_problem("COM1") and folder_name_problem("x|y")
