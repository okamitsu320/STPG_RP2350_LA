# STPG logic-analyzer tool

This tool captures the real waveform around a chosen vector of an STPG run, with an RP2350 (Pico 2) logic analyzer
running the gusmanb/logicanalyzer V6 firmware. It is a separate program from the STPG software: its own download, its
own page, its own licence (GPL v3, see `LICENSE` and `NOTICE.md`).

It never runs the STPG. A capture happens during a run started in the STPG Runner (`stpg-view`), which starts this
tool as a separate process for that run.

1. [Set-up and first capture](setup.md): install, wire, add the analyzer, capture.
2. [Plans, triggers and results](capture.md): what a plan means and how to read a capture.
3. [Troubleshooting](troubleshooting.md).
4. [Testing this tool](testing.md).
