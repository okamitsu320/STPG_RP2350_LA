"""captureResultView's "Trigger never seen" notice, rendered from this tool's production la.js in Node (no browser)."""
import json
from pathlib import Path
import shutil
import subprocess

import pytest

import stpg_capture.ui as ui_package

pytestmark = pytest.mark.stpg_host_only

PATTERN ={"type": "pattern", "condition": "ch1(A0)=0, ch7(A6)=1"}
LEVEL = {"type": "level", "condition": "ch7(A6)=1"}
WIRE = "gpio0 and gpio1 of the analyzer pico are connected"


def render(view):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for production-JS rendering")
    source = (Path(ui_package.__file__).parent / "static" / "la.js").read_text(encoding="utf-8")
    start = source.index("function captureResultView(")
    body = source[start:source.index("async function showCapture(", start)]
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
    return {"result": result, "directory": "capture", "plan_summary": {"trigger": trigger} if trigger else None}


@pytest.mark.parametrize("trigger,wire", [(PATTERN, True), (LEVEL, False)])
def test_no_byte_after_the_target_run_says_the_trigger_was_never_seen(trigger, wire):
    text = render(_view("NO_DATA", 0, trigger))
    assert "trigger never seen" in text and f"did not see {trigger['condition'].lower()} and sent no samples" in text
    assert "common ground" in text
    assert (WIRE in text) == wire


def test_without_a_plan_summary_the_condition_is_named_generically():
    text = render(_view("NO_DATA", 0, None))
    assert "did not see the trigger condition" in text and WIRE not in text


@pytest.mark.parametrize("outcome,received,armed", [
    ("NO_DATA", 100, True),  # the trigger fired; the transfer stopped
    ("CANCELLED", 0, True),  # the operator cancelled while collecting
    ("CAPTURED", 49465, True),
    ("ANALYZER_ERROR", 0, False),
])
def test_other_endings_do_not_claim_the_trigger_was_never_seen(outcome, received, armed):
    text = render(_view(outcome, received, PATTERN, armed=armed))
    assert "trigger never seen" not in text and WIRE not in text
