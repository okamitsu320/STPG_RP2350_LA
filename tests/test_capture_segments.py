"""Capture planning and alignment on segmented bundles (a run through the accessory process is in other modules)."""

from __future__ import annotations

from pathlib import Path

import pytest

from stpg_capture import load_bundle_timeline, plan_capture
from stpg_capture.align import align
from stpg_capture.config import AnalyzerConfig, CaptureRequestSpec
from stpg_capture.mock import PICO2_TURBO_LIMITS, synthesize
from stpg_runner import build_project
from stpg_runner.cli import main as runner_main

from test_stpg_capture import _earliest_possible_match, counter_stil

pytestmark = pytest.mark.stpg_host_only
ANALYZER = AnalyzerConfig.from_dict({"kind": "stpg_capture_analyzer", "schema_version": 1, "port": "MOCK-LA",
                                     "vid": 0x2E8A, "pid": 0x000A, "serial_number": "PUBLIC-LA",
                                     "limits": PICO2_TURBO_LIMITS.to_dict(), "wiring": "same-gpio"})


def request(vector, before=8, after=24, **extra):
    return CaptureRequestSpec.from_dict({"kind": "stpg_capture_request", "schema_version": 1,
                                         "address": {"vector": vector}, "before_vectors": before,
                                         "after_vectors": after, "strategy": "auto", **extra})


def counter_en_stil(n: int, split: int) -> str:
    """Address counter on every vector; EN=1 before ``split`` and 0 after, so later states are new."""

    lines = counter_stil(2).split("\n")
    head = lines[:lines.index("  W NORMAL;") + 1]
    body = []
    for i in range(n):
        a = (i // 4) % 256
        bits = "".join("1" if a >> b & 1 else "0" for b in range(8))
        body.append(f"  V {{ ALL = {bits}P{1 if i < split else 0}{'HL'[i % 2]}{'HLX'[i % 3]}; }}")
    return "\n".join(head + body + ["}"]) + "\n"


@pytest.fixture(scope="module")
def segmented(tmp_path_factory):
    """Counter pattern (3200 vectors) split into segments by the CUI."""

    root = tmp_path_factory.mktemp("la-segmented")
    source = root / "counter.stil"
    source.write_text(counter_en_stil(3200, 1600), encoding="utf-8")
    assert runner_main(["init", str(source), "--directory", str(root / "project"), "--wait-preset", "none",
                        "--completion", "hold", "--max-segment-vectors", "1600"]) == 0
    return build_project(root / "project" / "project.stpg-project.json")


def test_segment_capture_is_planned_inside_one_segment_and_aligns(segmented):
    timeline = load_bundle_timeline(segmented.bundle_dir, manifest_sha256=segmented.manifest_sha256)
    assert len(timeline.segments) >= 2
    assert timeline.segments[0].global_start == 0 and all(
        b.global_start == a.global_end + 1 for a, b in zip(timeline.segments, timeline.segments[1:]))
    second = timeline.segments[1]
    target = second.global_start + 300
    plan = plan_capture(timeline, request(target), ANALYZER)
    assert plan["status"] == "PLANNED" and plan["bundle"]["segment"] == 1
    assert plan["target"]["local_vector"] == 300
    assert any("previous segment's HOLD levels" in text for text in plan["assumptions"])
    assert _earliest_possible_match(timeline, plan) == plan["trigger"]["time_ps"]
    clipped = plan_capture(timeline, request(second.global_start + 2, before=10), ANALYZER)
    assert clipped["window"]["first_vector"] == second.global_start and clipped["notes"]
    assert align(timeline, plan, synthesize(timeline, plan, offset_ps=7_000))["status"] == "ALIGNED"


def test_states_of_an_earlier_segment_no_longer_block_a_trigger(tmp_path):
    """Armed at segment 1's RUN (SDK hook), the analyzer never sees segment 0, only its HOLD levels."""

    source = tmp_path / "repeat.stil"
    source.write_text(counter_stil(3200), encoding="utf-8")  # second half repeats segment-0 states
    assert runner_main(["init", str(source), "--directory", str(tmp_path / "p"), "--wait-preset", "none",
                        "--completion", "hold", "--max-segment-vectors", "1600"]) == 0
    built = build_project(tmp_path / "p" / "project.stpg-project.json")
    timeline = load_bundle_timeline(built.bundle_dir, manifest_sha256=built.manifest_sha256)
    plan = plan_capture(timeline, request(timeline.segments[1].global_start + 300), ANALYZER)
    assert plan["status"] == "PLANNED" and plan["bundle"]["segment"] == 1, plan["reasons"]
    # first visible exactly at the planned time, HOLD state included (-1 would mean "while armed, before RUN")
    assert _earliest_possible_match(timeline, plan) == plan["trigger"]["time_ps"]
    # the same condition does occur in segment 0: arming for the whole invocation would have fired there
    earlier = {**plan, "bundle": {**plan["bundle"], "segment": 0}}
    assert _earliest_possible_match(timeline, earlier) < timeline.segments[0].trace.total_ps


