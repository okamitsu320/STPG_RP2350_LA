# Plans, triggers and results

## Choosing where

Vectors are expanded, zero-based indices; waits do not renumber them. A STIL line inside a Loop, Macro or Procedure needs
its occurrence. A capture window lies within one segment, and one run captures one segment.

## What a plan does

The analyzer cannot count "the n-th event", so the plan looks for a state that does **not occur earlier** while the
analyzer is armed.

- Sample rate: the maximum frequency divided by an integer. Before and after samples must fit the analyzer's buffer.
- **auto** tries marker, then pattern, then level.
- **pattern** uses up to 16 contiguous channels (ch1–16), treats undriven intervals as "anything" and counts every
  intermediate state of simultaneous changes as possible.
- **level** is one channel's level.
- **marker** uses a derived pattern (below).

The analyzer is armed right before the target segment's RUN, so uniqueness is checked over what can be seen then: the
previous segment's held levels (unknown before segment 0) and the target segment. Levels on pins the pattern does not
drive are unknown before RUN. If the bench already shows the trigger state, the capture triggers early; that is detected
afterwards as UNALIGNED and not adopted.

A plan file is never trusted. Before each run, mock or import it is computed again from the bundle and the binding and
must match exactly.

## Marker patterns

When no unique state is near the target, card 6 makes a **new** project: a copy of the STIL with an `STPG_MARKER` signal
(pin-map role "marker") that is 1 only at the target vector, on a spare pin you choose. The source project is unchanged.

The derived build must keep every original signal's timing identical (`EQUIVALENT_TIMING`), and this is computed again
before each run, so the source bundle must stay available. The Runner names the marker pin as a pin with a role. Sources
must assign every vector to the same single group, with the target outside Loops, Macros and Procedures.

## Reading a capture

| Field | Meaning |
| --- | --- |
| `CAPTURED` | Samples arrived and were decoded |
| `MOCK`, `IMPORTED` | Offline: expected levels, or a `.lac` or CLCapture CSV file. Not a measurement of this run |
| `NO_DATA` | Armed, RUN sent, but no complete capture within the bounded wait |
| `NO_RUN` | The run ended before arming |
| `HOST_STOPPED` | The run ended after arming without asking to collect. The target RUN may or may not have been sent |
| `ANALYZER_ERROR`, `REJECT` | The analyzer or its bytes failed, or another failure here |
| `CLEANUP_ERROR`, `PERSIST_ERROR` | Data kept but not adopted. Partial data is kept as `capture.partial.bin` |

**Alignment** compares driven-pin transitions near the planned position: `ALIGNED` (at least 90 % of transitions per signal
and 98 % of driven levels agree) or `UNALIGNED`.

**Timing is adopted** only for a complete, persisted, ALIGNED capture with normal confidence and FULL or PARTIAL coverage.
Otherwise the reasons are shown, and any drawing is diagnostic only.

Levels at compare strobes are analyzer observations, not STPG compare results. Nothing here is a verdict on the device,
an electrical validation or a calibration.

## Limits

- One target segment per run; the window must fit one segment and the analyzer's buffer.
- The core waits at most 20 s for the collect reply, so a capture that needs longer cannot be used in a run: a plan with
  fewer samples or a shorter window is required, and the config writer says so.
- Arming at RUN removes exposure to upload and earlier segments, but not to levels held before RUN or host latency.
- After a failure or cancel the analyzer is closed once and **no cancel byte is sent**. It may stay armed until its own
  timeout. Use Reset LA in the Runner if it must stop now.
- Trigger latency and channel wiring have not been calibrated by this software.
