from __future__ import annotations

import json
from pathlib import Path

from netbench.timeline import build_timeline, describe, render_markdown

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "stream" / "diagnose-team.jsonl"


def _events(*events: dict) -> list[str]:
    return [json.dumps(e) for e in events]


def _call(tid: str, name: str, parent: str | None = None, **args: object) -> dict:
    return {
        "type": "assistant",
        "parent_tool_use_id": parent,
        "message": {"content": [{"type": "tool_use", "id": tid, "name": name, "input": args}]},
    }


def _result(tid: str, value: object, *, error: bool = False, parent: str | None = None) -> dict:
    text = value if isinstance(value, str) else json.dumps(value)
    return {
        "type": "user",
        "parent_tool_use_id": parent,
        "message": {
            "content": [
                {"type": "tool_result", "tool_use_id": tid, "content": text, "is_error": error}
            ]
        },
    }


def test_team_fixture_replays_every_role_in_order() -> None:
    timeline = build_timeline(FIXTURE.read_text(encoding="utf-8").splitlines())
    launched = [s.args["subagent_type"] for s in timeline.steps if s.tool == "Agent"]
    assert launched == [
        "l2-investigator",
        "l3-investigator",
        "policy-investigator",
        "change-agent",
        "verifier",
    ]
    actors = {s.actor for s in timeline.steps}
    assert {"commander", "change-agent", "verifier"} <= actors
    text = render_markdown(timeline, "fixture")
    assert "change `d4579df2edfa1430`" in text
    assert "export_change -> pending" in text
    assert "Reported root cause: **r3 ospf.area**" in text


def test_show_commands_collapse_per_agent_and_refusals_are_visible() -> None:
    lines = _events(
        _call("a1", "Agent", subagent_type="l3-investigator", description="L3"),
        _call("s1", "mcp__twinlab__run_show_command", parent="a1", node="r1", cmd="x"),
        _call("s2", "mcp__twinlab__run_show_command", parent="a1", node="r2", cmd="y"),
        _call("s3", "mcp__twinlab__run_show_command", parent="a1", node="r1", cmd="z"),
        _call("a2", "Agent", subagent_type="change-agent", description="fix"),
        _call("c1", "mcp__twinlab__apply_config", parent="a2", node="r4", ops=[{"kind": "x"}]),
        _result("c1", "Error executing tool apply_config: validation", error=True, parent="a2"),
    )
    timeline = build_timeline(lines)
    shows = [s for s in timeline.steps if s.tool == "run_show_command"]
    assert len(shows) == 1 and shows[0].count == 3 and shows[0].nodes == ["r1", "r2"]
    assert describe(shows[0]) == "3 show commands on r1, r2"
    apply = next(s for s in timeline.steps if s.tool == "apply_config")
    assert apply.actor == "change-agent" and apply.ok is False
    assert "refused: Error executing tool apply_config" in describe(apply)


def test_route_diff_counts_added_and_removed_routes() -> None:
    diff = {
        "before": "a",
        "after": "b",
        "nodes": {"isp": {"added": ["10.0.20.0/24 via r4"], "removed": []}, "r1": {}},
        "changed_nodes": 1,
    }
    lines = _events(
        _call("d1", "mcp__netverify__route_diff", before_id="a", after_id="b"),
        _result("d1", diff),
        _call("d2", "mcp__netverify__route_diff", before_id="a", after_id="a"),
        _result("d2", {"before": "a", "after": "a", "nodes": {}, "changed_nodes": 0}),
    )
    first, second = build_timeline(lines).steps
    assert describe(first) == "route_diff S0 to S1: +1 / -0 routes on 1 nodes"
    assert describe(second) == "route_diff S0 to S1: no route changes"
