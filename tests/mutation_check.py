#!/usr/bin/env python3
"""Do this tool's tests catch the faults they exist for? Break one thing at a time and require a test to fail.

Each mutant changes one line of a temporary copy of ``stpg_capture``; the source tree is never touched. The named test
must pass on the unchanged code (the positive control) and fail on the mutant. Run from this tool's folder:

    python tests/mutation_check.py [--list]

Exit status 0 only if every mutant was caught.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "stpg_capture"
F = "tests/test_stpg_capture_failpaths.py"
P = "tests/test_accessory_process.py"
MUTANTS = [
    ("LA-01", "a cancel byte is sent after a failure", "accessory.py",
     '        self.close()\n        if self.session is not None and self.session.received():\n',
     '        if self.session is not None:\n            self.session.cancel()\n'
     '        self.close()\n        if self.session is not None and self.session.received():\n',
     f"{F}::test_no_data_closes_the_analyzer_once_without_a_cancel_byte"),
    ("LA-02", "a wrong nonce is accepted", "accessory.py",
     '    if not isinstance(request.get("nonce"), str) or not request["nonce"] or (nonce and request["nonce"] != nonce):\n',
     "    if False:\n", f"{P}::test_a_request_after_complete_or_out_of_order_is_rejected"),
    ("LA-03", "the config writer trusts the plan", "external_config.py",
     "    plan = validate_plan(timeline, capture_plan, analyzer)\n", "    plan = capture_plan\n",
     f"{P}::test_the_writer_checks_offline_and_never_overwrites"),
    ("LA-04", "the accessory trusts the plan at open", "accessory.py",
     "        self.plan = validate_plan(self.timeline, plan, self.analyzer)  # derived again from the bundle: no trust\n",
     "        self.plan = plan\n", f"{F}::test_any_changed_plan_field_rejects_before_device_io"),
    ("LA-05", "output after COMPLETE (the core must stop the next RUN)", "accessory.py",
     '            _reply(stdout, request, status, result if method == "after_terminal" else None)\n',
     '            _reply(stdout, request, status, result if method == "after_terminal" else None)\n'
     '            if method == "after_terminal":\n                stdout.write(b"late\\n")\n                stdout.flush()\n',
     f"{F}::test_a_completed_capture_in_segment_0_lets_segment_1_run"),
    ("LA-06", "the port list ignores --no-device-io", "ui/service.py",
     '        if not self.device_io:\n            return {"ports": ports, "error": None, "real_scan_performed": False}\n',
     "", "tests/test_la_page.py::test_port_listing_is_metadata_on_request_and_off_without_device_io"),
    ("LA-07", "the settings folder rule is forked again", "ui/server.py",
     "from stpgview.config_location import ConfigLocationError, resolve_config_dir, start_line\n",
     "from stpgview.config_location import ConfigLocationError, start_line, resolve_config_dir as _shared\n"
     "resolve_config_dir = lambda explicit=None: _shared(explicit)\n",
     "tests/test_settings_folder.py::test_the_original_mismatch_cannot_return"),
    ("LA-08", "a bundle changed during decoding is accepted", "timeline.py",
     '    checked = verify_bundle(root, manifest_sha256=manifest_sha256, cancel=progress.cancel, reuse=True)\n',
     '    checked = {"status": "PASS"}\n',
     "tests/test_sdk_round2.py::test_public_final_check_still_rejects_a_map_changed_during_decode"),
    ("LA-09", "a binding is overwritten without Replace", "ui/service.py",
     '        if path.exists() and request.get("replace") is not True:\n', "        if False:\n",
     "tests/test_settings_folder.py::test_page_state_reports_the_folder_and_an_old_file_is_explained_not_rewritten"),
    ("LA-10", "a new folder name that exists in another case is accepted", "ui/service.py",
     "        if name.casefold() in taken:\n", "        if name in taken:\n",
     "tests/test_la_path_picker.py::test_a_name_that_exists_in_any_case_is_refused"),
    ("LA-11", "the picker opens the home folder instead of a missing one", "ui/service.py",
     '            raise RunnerError(f"{folder} is not a folder (or does not exist)")\n'
     "        entries = []\n",
     "            folder = Path.home()\n        entries = []\n",
     "tests/test_la_path_picker.py::test_a_listing_names_home_and_roots_and_never_opens_another_folder_instead"),
    ("LA-12", "the level agreement table displays the saved one-based channel", "ui/static/la.js",
     "analyzerChannelLabel(l.channel)", "l.channel",
     "tests/test_la_page_render.py::test_result_tables_label_saved_channels_from_zero[levels]"),
    ("LA-13", "the strobe table displays the saved one-based channel", "ui/static/la.js",
     "analyzerChannelLabel(s.channel)", "s.channel",
     "tests/test_la_page_render.py::test_result_tables_label_saved_channels_from_zero[strobe_observations]"),
    ("LA-14", "the trigger-missed notice uses the saved one-based condition", "ui/static/la.js",
     "did not see ${triggerConditionText(trig, channels)} and sent",
     'did not see ${trig ? trig.condition : "the trigger condition"} and sent',
     "tests/test_la_page_render.py::test_no_byte_after_the_target_run_says_the_trigger_was_never_seen"),
]


def pytest_run(target: str, copy: Path | None) -> int:
    command = [sys.executable, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider", target]
    if copy:
        command[-1:-1] = ["-o", f"pythonpath={copy}"]
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    return subprocess.run(command, cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode


def check(mutant: tuple) -> tuple[str, bool]:
    ident, what, name, old, new, target = mutant
    source = (PACKAGE / name).read_text(encoding="utf-8")
    if source.count(old) != 1:
        return "STALE (text not found once)", False
    if pytest_run(target, None) != 0:
        return "BASELINE FAILED", False
    with tempfile.TemporaryDirectory(prefix="stpg-mutant-") as tmp:
        shutil.copytree(PACKAGE, Path(tmp) / "stpg_capture", ignore=shutil.ignore_patterns("__pycache__"))
        (Path(tmp) / "stpg_capture" / name).write_text(source.replace(old, new), encoding="utf-8")
        code = pytest_run(target, Path(tmp))
    return ("caught", True) if code == 1 else (f"SURVIVED (pytest exit {code})", False)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args()
    if args.list:
        for mutant in MUTANTS:
            print(f"{mutant[0]}  {mutant[1]}  ->  {mutant[5]}")
        return 0
    failed = 0
    for mutant in MUTANTS:
        text, ok = check(mutant)
        failed += not ok
        print(f"{mutant[0]}  {text}: {mutant[1]}", flush=True)
    print(f"{len(MUTANTS) - failed} of {len(MUTANTS)} mutants caught")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
