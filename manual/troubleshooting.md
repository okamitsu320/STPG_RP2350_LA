# Troubleshooting

| Symptom | What to do |
| --- | --- |
| The analyzer does not appear in the STPG Runner | Compare the settings folder on both pages; start both with the same `--config-dir` or `STPG_CONFIG_DIR` |
| Scan ports says "No real port scan" | This page runs with `--no-device-io`. Restart without it |
| Scan ports shows several ports | Unplug and replug the analyzer to see which row changes. Nothing is chosen for you |
| Identify: "analyzer reports … binding expects …" | The board differs from the binding. Use **Use the reported values in this binding…** after checking it is the right board |
| Identify: "already leased" | Another program uses this USB device |
| A binding is listed with an error | That file is unreadable or has another format; it is left as it is. Add a new binding, or fix the file |
| Plan UNSUPPORTED | Shorten the window, select fewer signals, or use a marker pattern |
| The config writer: "more than the core's 20 s reply limit" | Plan fewer samples or a shorter window |
| The Runner refuses the config | A file in the config folder changed, or the mode does not fit: write a new folder |
| "STPG not started", accessory REJECT | The accessory refused before READY. Open the run's `accessory/` folder: `rejected.json` or `capture/capture.json` gives the reason |
| `NO_DATA`, "Trigger never seen" with a pattern trigger | Connect analyzer GPIO0 to GPIO1, or plan with a level trigger to tell this apart from a wiring problem |
| `NO_DATA` with no bytes at all | Check the wires listed in the plan, the common ground, and that the STPG run completed |
| Capture `UNALIGNED` | An early trigger or a wiring mismatch; see the unmatched signals |
| Marker: "no derivation record" | The record is kept when the pattern is derived on this page. For one derived with `stpg-capture marker`, write the config with `stpg-capture accessory-config --marker-derivation marker.derivation.json`. Keep the source bundle available |
| After an update, the Runner refuses the bundle | Rebuild the bundle, plan again and write a new config |
