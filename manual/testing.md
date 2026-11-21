# Testing this tool

The tests are host only: the STPG and the analyzer are models, and all settings are temporary folders. In the test
process, `tests/la_support.py` blocks real serial opens, USB port listing (it answers with an empty list) and starting
a debugger or flashing tool (OpenOCD, GDB, picotool, pyOCD). The programs the tests start run with device access
switched off (`--no-device-io`, `--no-device-run`, or the accessory's simulated mode). The guard replaces Python calls
in the test process; it is not an OS sandbox, and it does not cover `os.system`, direct system calls or what a child
program starts. Its own tests put a fake layer below it first, so a broken guard still reaches nothing real.

The tests use the installed STPG package and this tool, and need no checkout of the STPG source. Do not run suites in
parallel, because they share simulated device identities.

## Set up

Use the two source folders named in [Set-up](setup.md), but run the following commands **from the LA source folder**
(`STPG_RP2350_LA`, containing this tool's `pyproject.toml`). Use a separate test environment. This installs both
matching local sources, not an STPG package found by name on an index. Build dependencies and the test tools may
need Internet access.

Windows PowerShell:

```powershell
python -m venv stpg-test-env
.\stpg-test-env\Scripts\python.exe -m pip install . ../STPG_RP2350 pytest selenium build
```

Linux / macOS:

```sh
python3 -m venv stpg-test-env
./stpg-test-env/bin/python -m pip install . ../STPG_RP2350 pytest selenium build
```

Firefox and geckodriver must be installed separately for browser tests. Node is required for the page-script
rendering tests. Missing test tools cause NOT RUN, not a pass.

The accessory is started as `python -I -m stpg_capture.accessory`. Isolated mode ignores `PYTHONPATH` and the working
folder on purpose, so this tool must really be installed in that interpreter's environment.

## Run

First build both local wheels and name them for the installed-wheel tests. These filenames are for this version,
not placeholders. From the LA source folder, Windows PowerShell:

```powershell
.\stpg-test-env\Scripts\python.exe -m build --wheel --outdir packages ../STPG_RP2350
.\stpg-test-env\Scripts\python.exe -m build --wheel --outdir packages .
$env:STPG_CORE_WHEEL = (Resolve-Path ./packages/stpg_rp2350-0.1.0.dev7-py3-none-any.whl).Path
$env:STPG_LA_WHEEL = (Resolve-Path ./packages/stpg_rp2350_la-0.1.0.dev7-py3-none-any.whl).Path
.\stpg-test-env\Scripts\python.exe -m pytest -q tests
```

Linux / macOS:

```sh
./stpg-test-env/bin/python -m build --wheel --outdir packages ../STPG_RP2350
./stpg-test-env/bin/python -m build --wheel --outdir packages .
export STPG_CORE_WHEEL="$PWD/packages/stpg_rp2350-0.1.0.dev7-py3-none-any.whl"
export STPG_LA_WHEEL="$PWD/packages/stpg_rp2350_la-0.1.0.dev7-py3-none-any.whl"
./stpg-test-env/bin/python -m pytest -q tests
```

For just the real-browser walkthrough, use `-m pytest -q tests/test_la_browser.py tests/test_two_programs_browser.py`;
run it alone, not beside the suite. For mutations use `tests/mutation_check.py` with this environment's Python.
With matching wheels already built elsewhere, set the two variables to their actual paths; no STPG index entry
is assumed. The tests resolve relative wheel and wheelhouse paths before starting pip in a fresh temporary folder.

`python -m pytest` puts the current folder first, so in a source checkout these tests import `stpg_capture` from the
checkout. To test the wheel itself, use `test_installed_core_la.py`, or run `pytest` from a copy of `tests/` and
`manual/` with no `stpg_capture` folder beside it.

`STPG_WHEELHOUSE` is a folder with the dependencies, for a run without network access. A skipped test is not a pass:
report it as NOT RUN. `test_la_page_render.py` renders the real page script in Node; it is not a browser test.

`mutation_check.py` breaks one line at a time in a temporary copy and requires the named test to pass on the unchanged
code and fail on the mutant. `--list` shows them. `STALE` and `BASELINE FAILED` do not count as caught.

## What is covered, and where

`…::` means the same file as the name before it in that row.

| ID | Requirement | Test (`tests/`) |
| --- | --- | --- |
| L1 | The same settings folder as `stpg-view` for the same inputs, with both real commands | `test_settings_folder.py::test_both_commands_report_the_same_folder_source_and_state`, `…::test_both_commands_stop_the_same_way_on_an_unusable_value`, `…::test_the_original_mismatch_cannot_return` |
| L2 | A binding saved here is listed by the Runner; old files are explained, not rewritten | `test_settings_folder.py::test_a_binding_written_by_this_tool_is_listed_by_the_runner_in_the_same_folder`, `…::test_page_state_reports_the_folder_and_an_old_file_is_explained_not_rewritten`, `test_la_page.py::test_bindings_are_written_where_the_stpg_runner_lists_them` |
| L3 | Finding the analyzer: metadata only, on request, off with `--no-device-io`, selects nothing | `test_la_page.py::test_port_listing_is_metadata_on_request_and_off_without_device_io`, `test_la_browser.py::test_scan_ports_fills_the_form_only_when_used` |
| L4 | Identify keeps its record, and reported limits can be taken over | `test_la_identify.py`, `test_la_browser.py::test_reported_limits_can_be_adopted_into_the_binding` |
| L5 | First use: guide, folder, wiring | `test_la_browser.py::test_a_new_user_is_guided_and_sees_the_settings_folder_and_the_wiring` |
| L6 | Plan, config, a real child process, a result across two segments | `test_installed_core_la.py`, `test_sdk_round3.py`, `test_accessory_process.py::test_the_core_starts_the_installed_tool_and_gets_its_neutral_result`, `test_la_page.py::test_plan_on_the_page_run_with_the_core_and_open_the_run_here` |
| L7 | Requests are strict; an unusable one gets ERROR or no reply | `test_accessory_process.py::test_requests_are_strict_and_a_rejection_is_one_error_reply`, `…::test_a_request_after_complete_or_out_of_order_is_rejected`, `…::test_a_partial_line_at_eof_is_never_a_command` |
| L8 | Arming right before the target RUN; failed arm blocks RUN; failed collect blocks the next segment | `test_stpg_capture_failpaths.py::test_arm_follows_the_target_upload_and_immediately_precedes_its_run`, `…::test_refused_arm_stops_the_sdk_before_run_without_cleanup`, `…::test_a_failed_collect_blocks_the_next_segment`, `…::test_a_completed_capture_in_segment_0_lets_segment_1_run` |
| L9 | After a failure: closed once, no cancel byte, evidence kept | `test_stpg_capture_failpaths.py::test_no_data_closes_the_analyzer_once_without_a_cancel_byte`, `…::test_cancel_after_arming_sends_no_run_and_no_cancel_byte`, `…::test_close_failure_is_counted_and_never_reported_as_clean`, `…::test_final_persistence_failure_leaves_the_pending_checkpoint` |
| L10 | A plan is never trusted, at `open` or in the writer | `test_stpg_capture_failpaths.py::test_any_changed_plan_field_rejects_before_device_io`, `test_accessory_process.py::test_the_writer_checks_offline_and_never_overwrites` |
| L11 | Page guards: token, Host, origin, no route that runs the STPG | `test_la_page.py::test_the_page_is_served_and_every_api_call_is_guarded` |
| L12 | The wheel: licence, notice, page assets, no core files | `test_installed_core_la.py::test_both_come_from_the_wheels_and_this_wheel_carries_its_licence` |
| L13 | The manual's links, options and labels are real | `test_manual_pages.py` |
| L14 | The tests can fail | `mutation_check.py` |
| L15 | From nothing to a capture, as the manual says: both real commands, a real child process, a real browser | `test_two_programs_browser.py` |
| L16 | The guard itself: serial opens, port listing and debugger starts stop in it; with it removed, calls reach only a fake layer | `test_hardware_guard.py` |
| L17 | The path picker: Go to a typed folder, Home, every drive (a model on this host, not a native Windows run), a new output folder as an existing folder plus a checked name, nothing created before writing, and no other folder opened instead of a missing one | `test_la_path_picker.py`, `test_la_browser.py::test_the_picker_opens_a_typed_folder_and_names_a_new_folder_without_creating_it` |
| L18 | Registering an analyzer follows one order (scan and Use, limits, wiring, Save, Identify); limits name their frequencies and are not taken from the USB identity; the marker pin is optional and never chosen for the user | `test_la_browser.py::test_a_new_binding_walks_through_the_steps_and_chooses_nothing_for_the_user`, `…::test_a_new_user_is_guided_and_sees_the_settings_folder_and_the_wiring` |

## Not covered here

Real analyzers and boards, native Windows and macOS, and any browser other than the one you ran. Report these as NOT
RUN unless done, each with its own approval where hardware is involved.
