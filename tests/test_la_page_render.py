"""Capture results rendered from the production la.js in Node (no browser)."""
import json
from pathlib import Path
import shutil
import subprocess

import pytest

import stpg_capture.ui as ui_package

pytestmark = pytest.mark.stpg_host_only

PATTERN = {"type": "pattern", "base_channel": 0, "count": 2, "value": 2,
           "condition": "ch1(A0)=0, ch2(A1)=1"}
LEVEL = {"type": "level", "base_channel": 6, "count": 1, "value": 1, "condition": "ch7(A6)=1"}
CHANNELS = [{"channel": 0, "signal": "A0"}, {"channel": 1, "signal": "A1"}, {"channel": 6, "signal": "A6"}]
WIRE = "gpio0 and gpio1 of the analyzer pico are connected"


def render(view):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for production-JS rendering")
    source = (Path(ui_package.__file__).parent / "static" / "la.js").read_text(encoding="utf-8")
    start = source.index("function captureResultView(")
    body = source[start:source.index("async function showCapture(", start)]
    if "function triggerConditionText(" in source:
        start = source.index("function triggerConditionText(")
        body = source[start:source.index("function renderPlan(", start)] + body
    script = """
const input = JSON.parse(require('fs').readFileSync(0, 'utf8'));
class Dom {
  constructor(...children) { this.children = children.filter(x => x !== null && x !== undefined); }
  append(...children) { this.children.push(...children.filter(x => x !== null && x !== undefined)); }
  toString() { return this.children.map(x => String(x)).join(' '); }
}
const el = (_tag, _attrs, ...children) => new Dom(...children);
const badge = (text) => new Dom(text);
const notice = (_type, title, lines=[]) => new Dom(title, ...lines);
const table = (headers, rows) => new Dom(...headers, ...rows.flat());
const kvList = (rows) => new Dom(...rows.flat());
const copyable = (text) => text;
const fmtTime = (ps) => String(ps);
""" + body + "console.log(JSON.stringify(String(captureResultView(input))));"
    result = subprocess.run([node, "-e", script], input=json.dumps(view), text=True,
                            capture_output=True, check=True, timeout=10)
    return json.loads(result.stdout).lower()


def _view(outcome, received, trigger, *, armed=True):
    result = {"outcome": outcome, "alignment_status": "NOT_ATTEMPTED", "mode": "device",
              "timing": {"adopted": False, "reasons": []}, "errors": ["no complete capture within the bounded wait"],
              "analyzer": {"session": {"armed": armed, "capture_bytes_received": received}}}
    return {"result": result, "directory": "capture",
            "plan_summary": {"trigger": trigger, "channels": CHANNELS} if trigger else None}


@pytest.mark.parametrize("trigger,wire,shown", [
    (PATTERN, True, "ch0(A0)=0, ch1(A1)=1"), (LEVEL, False, "ch6(A6)=1")])
def test_no_byte_after_the_target_run_says_the_trigger_was_never_seen(trigger, wire, shown):
    text = render(_view("NO_DATA", 0, trigger))
    assert "trigger never seen" in text and f"did not see {shown.lower()} and sent no samples" in text
    assert trigger["condition"].lower() not in text
    assert "common ground" in text
    assert (WIRE in text) == wire


def test_without_a_plan_summary_the_condition_is_named_generically():
    text = render(_view("NO_DATA", 0, None))
    assert "did not see the trigger condition" in text and WIRE not in text


def test_a_legacy_summary_without_structured_channel_fields_does_not_guess():
    text = render(_view("NO_DATA", 0, {"type": "level", "condition": "ch1(A0)=1"}))
    assert "did not see the trigger condition" in text and "ch1(a0)" not in text


@pytest.mark.parametrize("outcome,received,armed", [
    ("NO_DATA", 100, True),  # the trigger fired; the transfer stopped
    ("CANCELLED", 0, True),  # the operator cancelled while collecting
    ("CAPTURED", 49465, True),
    ("ANALYZER_ERROR", 0, False),
])
def test_other_endings_do_not_claim_the_trigger_was_never_seen(outcome, received, armed):
    text = render(_view(outcome, received, PATTERN, armed=armed))
    assert "trigger never seen" not in text and WIRE not in text


@pytest.mark.parametrize("section", ["levels", "strobe_observations"])
def test_result_tables_label_saved_channels_from_zero(section):
    view = _view("CAPTURED", 256, None)
    rows = [{"signal": "first_pin", "channel": 1}, {"signal": "last_pin", "channel": 24},
            {"signal": "invalid_pin", "channel": 0}]
    if section == "levels":
        for row in rows:
            row.update(agree=4, total=4)
    else:
        for row in rows:
            row.update(H_seen_high=2, H_total=2, L_seen_low=2, L_total=2)
    view["result"]["alignment"] = {"status": "ALIGNED", section: rows}
    text = render(view)
    assert "first_pin ch0 " in text and "last_pin ch23 " in text
    assert "invalid_pin — " in text
    assert "signal analyzer channel" in text
