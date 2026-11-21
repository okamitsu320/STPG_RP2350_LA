# STPG_RP2350_LA

This optional companion for [STPG_RP2350](https://github.com/okamitsu320/STPG_RP2350) plans a capture around a
chosen pattern vector and shows the captured waveform next to the expected levels. It runs as a separate program;
the STPG Runner controls the STPG itself. Try the simulated analyzer without any hardware.

[GPL-3.0-only](LICENSE) · [Third-party notices](NOTICE.md)

## Acknowledgements

Thank you to [Agustín Gimenez Bernad (gusmanb)](https://github.com/gusmanb) and the contributors to
[LogicAnalyzer](https://github.com/gusmanb/logicanalyzer). The companion uses their wire protocol and
adapts parts of the host driver to Python. [NOTICE.md](NOTICE.md) records the source revision and what was adapted.

STPG_RP2350_LA is not affiliated with or endorsed by the LogicAnalyzer project. For physical captures,
get the original Pico 2 firmware from [LogicAnalyzer Release 6.0](https://github.com/gusmanb/logicanalyzer/releases/tag/v6.0.0.0).
The companion does not modify, bundle or install that firmware. Report problems with **this companion** in
[this repository](https://github.com/okamitsu320/STPG_RP2350_LA/issues/new/choose), not to LogicAnalyzer.
For suspected vulnerabilities in this companion, use the [private reporting instructions](SECURITY.md).

## Try the simulated analyzer

Place matching local source folders `STPG_RP2350` and `STPG_RP2350_LA` beside each other and open a terminal
in `STPG_RP2350`. Both packages install from the local folders. If the core guide already created `stpg-env`,
skip the first line.

Windows PowerShell:

```powershell
python -m venv stpg-env
.\stpg-env\Scripts\python.exe -m pip install . ../STPG_RP2350_LA
.\stpg-env\Scripts\python.exe -m stpg_capture.ui.server --demo --no-device-io --config-dir ./settings-check --results-dir ./la-runs-check
```

Linux (also the command form for macOS; macOS is untested and not actively supported):

```sh
python3 -m venv stpg-env
./stpg-env/bin/python -m pip install . ../STPG_RP2350_LA
./stpg-env/bin/python -m stpg_capture.ui.server --demo --no-device-io --config-dir ./settings-check --results-dir ./la-runs-check
```

See the [source-install walkthrough](manual/setup.md) to open the core alongside the analyzer and try a mock capture.

![Simulated analyzer result showing ch0 to ch7 and the not-a-measurement warning](manual/images/capture-simulated.png)

This capture uses synthesized levels from the public counter example. It is not a measurement.

`stpg-la` and `stpg-capture` are also installed as console commands; they need the environment's Scripts/bin folder
on PATH. The module command above does not.

## Compatibility

Install matching local sources or wheels together: this `stpg-rp2350-la` version (`0.1.0.dev7`) requires
`stpg-rp2350==0.1.0.dev7`. The shared core reader accepts `run.json.gz` and older `run.json`, but refuses a
folder containing both. Identity links hash the stored file bytes. Re-export accessory configs after changing the
SDK, bundle or Python environment; do not edit the saved hashes.

Start with [the manual](manual/README.md).
