# Notices: stpg-rp2350-la

Project-specific original code: Copyright (c) 2026 okamitsu320.
Third-party portions retain their respective upstream copyrights and notices.

`stpg-rp2350-la` (Python package `stpg_capture`) is the logic-analyzer accessory of the RP2350 STPG tools. It is
distributed under the GNU General Public License version 3 (`LICENSE`, the unmodified text that the upstream
project ships). No "or later" grant is asserted: the upstream repository at the consulted revision ships the GPL v3
text and states no later-version permission, and the choice is the owner's. Not legal advice; the final licence
review of this distribution is an open release gate.

It is a separate distribution and a separate program. The MIT-licensed STPG core (`stpg-rp2350`: compiler, SDK,
CLI, Viewer, Runner GUI) does not contain or import it. The core starts this program as a child process only when
the operator selects its local config file for a run (the SDK's external accessory contract v1: JSON lines on
stdin/stdout and files in the run's `accessory/` folder). This package imports the core SDK as a library.

## Upstream reference

- Project: gusmanb/logicanalyzer (the Pico/Pico 2 logic analyzer firmware, driver and applications).
- Consulted revision: `4e88df784e4879c1f180456a72d71910dd15faf1`.
- Licence at that revision: GNU GPL v3 (`LICENSE`, SHA-256
  `3972dc9744f6499f0f9b2dbf76696f2ae7ad8af9b23dde66d6af86c9dfb36986`).

Nothing was copied byte for byte from upstream files; the code is Python written after reading them. That is not a
clean-room implementation, and parts are direct translations (marked **T** below). Licensing this package under the
GPL is what makes those parts consistent with upstream; it is not a statement that they are independent.

## Provenance by file and element

Legend: **T** translated (an upstream expression or algorithm re-expressed), **F** interface/format facts taken
from the upstream source (byte layout, command codes, text lines, file formats), **B** behaviour learned from the
upstream source and implemented independently, **O** original STPG design without upstream content.

| File / element | Class | Upstream source (at 4e88df78) |
| --- | --- | --- |
| `la_protocol.trigger_offset` (`round(delay_ns/sample_ns + 0.3)`) | **T** | `LogicAnalyzerDriver.cs` 538–540 |
| `COMPLEX_TRIGGER_DELAY = 5`, `FAST_TRIGGER_DELAY = 3` | **T** (constants) | `CaptureModes.cs` 11–12 (`TriggerDelays`) |
| `AnalyzerInfo.min_frequency = max*2 // 65535` | **T** | `AnalyzerDriverBase.cs` 16 |
| `pretrigger_limit(…, "driver") = total // 10` | **T** | `AnalyzerDriverBase.cs` 50 (`MaxPreSamples`) |
| `build_request` validation (channel range, ascending order, pre/post ≥ 2, pattern trigger 1..16 contiguous) and moving `offset` from post to pre | **T/B** | `LogicAnalyzerDriver.cs` (capture set-up), `LogicAnalyzer_Capture.c` (StartCapture checks) |
| `escape`/`frame`/`unframe` (`55 AA … AA 55`, escape `F0`, `x ^ F0`) | **F** (unframe is new, for the model) | `AnalyzerDriverBase.cs` 135–151 |
| `REQUEST_FORMAT` (48-byte `CAPTURE_REQUEST`), field order | **F** | `LogicAnalyzer_Structs.h` 9–40 |
| `CMD_*`, `TRIGGER_*`, `MODE_*`, bytes per sample | **F** | `LogicAnalyzer.c`, `CaptureModes.cs`, `LogicAnalyzerDriver.cs` |
| `CHANNEL_GPIO` (2..22, 26..28), external trigger channel 24 | **F** | `LogicAnalyzer_Capture.c` 61–65 (`pinMap[]`) |
| `parse_identify` (five lines, `LOGIC_ANALYZER_…`, `FREQ:`/`BLASTFREQ:`/`BUFFER:`/`CHANNELS:`), V6 minimum | **F** | `LogicAnalyzer.c` 252–262, `VersionValidator.cs` |
| `expected_capture_bytes`, `parse_capture` (u32 count, samples, u8 stamp count, stamps) | **F** | `LogicAnalyzer_Capture.c` readout, `LogicAnalyzerDriver.cs` readout |
| `la_device.py` session (115200 baud, DTR asserted, line phase, `CAPTURE_STARTED`/`CAPTURE_ERROR`, one-byte cancel) | **B** (sequence) + **O** (journal, lease, timeouts, evidence) | `LogicAnalyzer.c` main loop, `LogicAnalyzerDriver.cs` |
| `mock.py` `ModelAnalyzer`, `PICO2_LIMITS`/`PICO2_TURBO_LIMITS` (100/200 MHz, 200/400 MHz blast, 128×3 KiB, 24 ch), level/pattern trigger semantics | **B/F** | `LogicAnalyzer_Board_Settings.h`, `LogicAnalyzer.pio`, `LogicAnalyzer.c` |
| `trace.py` `import_csv`, `import_lac` | **F** (file formats) | `CLCapture/Program.cs` (CSV writer, `ExportedCapture`), desktop `.lac` export |
| `planner.py` (window, trigger choice, sample rate, buffer fit) | **O** using the limits/semantics above | (pattern-trigger channel rule from the PIO/driver) |
| `align.py`, `timeline.py`, `address.py`, `wiring.py` (presets map STPG GPIO to `CHANNEL_GPIO`), `marker.py`, `coordinator.py`, `config.py`, `acceptance.py`, `identify.py`, `cli.py`, `accessory.py`, `external_config.py` | **O** | — |
| `ui/` (separate page, server and service; moved from the former in-process GUI capture card) | **O** | — |
| Tests (`tests/`) | **O**, with byte vectors and limits that restate the **F/T** items above | — |

The session no longer sends the one-byte cancel after a failure inside a run: the external accessory contract v1
permits only the companion's once-close (see `accessory.py`). The standalone `stpg-capture identify` and the UI's
Identify are unchanged.
