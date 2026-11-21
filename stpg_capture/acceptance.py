"""When a capture's timing is adopted, and the stpg-capture exit-code policy.

Adoption (the capture is drawn without a diagnostic note, a 0 exit code) needs every one of:

* the acquisition finished: outcome CAPTURED (device), MOCK or IMPORTED;
* the result is the FINAL record and was persisted (not a PENDING checkpoint);
* alignment ALIGNED with ``normal`` confidence (at least three matched tester
  transitions) and coverage FULL or PARTIAL of the requested window.

Anything else may still be shown, but only as an explicitly labelled
diagnostic without a timing verdict. Adoption is never a DUT verdict.
"""

from __future__ import annotations

from typing import Any

ACQUIRED = ("CAPTURED", "MOCK", "IMPORTED")
COVERED = ("FULL", "PARTIAL")

EXIT_OK = 0
EXIT_ERROR = 2
EXIT_MISMATCH = 3
EXIT_NOT_ADOPTED = 4
EXIT_CANCELLED = 130


def timing_adoption(result: dict[str, Any]) -> dict[str, Any]:
    """Recompute adoption from the result's own fields (a stored flag is never trusted)."""

    reasons = []
    if result.get("outcome") not in ACQUIRED:
        reasons.append(f"capture outcome {result.get('outcome')} (no complete acquisition)")
    if result.get("record") != "FINAL" or result.get("result_persisted") is not True:
        reasons.append("not a persisted final capture record")
    alignment = result.get("alignment") or {}
    if alignment.get("status") != "ALIGNED":
        reasons.append(f"alignment {alignment.get('status') or 'NOT_ATTEMPTED'}")
    else:
        if alignment.get("confidence") != "normal":
            reasons.append(f"alignment confidence {alignment.get('confidence')}")
        if alignment.get("coverage") not in COVERED:
            reasons.append(f"window coverage {alignment.get('coverage')}")
    if alignment.get("offset_ps") is None and not reasons:
        reasons.append("no fitted offset")
    return {"adopted": not reasons, "reasons": reasons}


def capture_exit(run: dict[str, Any] | None, capture: dict[str, Any] | None, *, device: bool) -> tuple[int, str]:
    """Exit code and reason. Precedence: cancelled 130 > error/incomplete 2 > not adopted 4 > mismatch 3 > 0.

    ``device`` is true when an STPG result is required; mock/import judge the capture alone and never return 3.
    """

    capture = capture or {}
    run = run or {}
    if run.get("outcome") == "CANCELLED" or capture.get("outcome") == "CANCELLED":
        return EXIT_CANCELLED, "cancelled by the operator"
    if device and run.get("outcome") != "COMPLETED":
        return EXIT_ERROR, f"STPG run {run.get('outcome') or 'not started'}"
    if capture.get("outcome") not in ACQUIRED:
        return EXIT_ERROR, f"capture {capture.get('outcome') or 'missing'}"
    if capture.get("record") != "FINAL" or capture.get("result_persisted") is not True:
        return EXIT_ERROR, "capture result was not persisted as a final record"
    adoption = timing_adoption(capture)
    if not adoption["adopted"]:
        return EXIT_NOT_ADOPTED, "capture kept, timing not adopted: " + "; ".join(adoption["reasons"])
    if device and run.get("comparison") == "MISMATCH":
        return EXIT_MISMATCH, "run completed with a comparison mismatch (capture adopted)"
    return EXIT_OK, "capture adopted" + (" and run completed" if device else "")
