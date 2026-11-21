# STPG_RP2350_LA

This is the separate logic-analyzer tool for [STPG_RP2350](https://github.com/okamitsu320/STPG_RP2350).
The distributions are `stpg-rp2350` (core) and `stpg-rp2350-la` (this tool).
Install matching local sources or wheels together. This development version
is `0.1.0.dev7` and requires core `0.1.0.dev7` exactly.
New STPG run records are `run.json.gz`; the shared core reader also accepts
legacy `run.json`, rejecting folders that contain both. Identity links hash
the stored compressed bytes. Re-export accessory configs after changing the
SDK, bundle or Python environment; do not manually replace their hashes.
No firmware or device operation is authorized by these installation instructions.

Captures the real waveform around a chosen vector of an STPG run, with an RP2350 (Pico 2) logic analyzer running the
gusmanb/logicanalyzer V6 firmware. It is a separate program from the STPG software, which starts it as its own process
for one run. It never runs the STPG itself. Licence: GPL v3 (`LICENSE`, `NOTICE.md`).

To check the page without hardware, use the [source-install walkthrough](manual/setup.md).
It gives copyable Windows PowerShell and Linux/macOS commands, with no PATH or activation requirement.
The core and this tool are installed from two matching local source folders in one command.

After that installation, from the core folder on Windows:

```powershell
.\stpg-env\Scripts\python.exe -m stpg_capture.ui.server --demo --no-device-io --config-dir ./settings-check --results-dir ./la-runs-check
```

`stpg-la` and `stpg-capture` are also installed as console commands; they need the environment's Scripts/bin folder
on PATH. The module command above does not. Demo output is not a measurement.

Start with [the manual](manual/README.md).
