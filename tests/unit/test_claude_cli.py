"""The headless Claude runner: stream-json parsing, report extraction, and scoring inputs."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from netbench.admin import CallableAdmin
from netbench.claude_cli import (
    ClaudeCliRunner,
    extract_report,
    find_claude,
    parse_stream,
    split_tool_name,
)
from netbench.runner import RunContext
from tests.unit.test_manual_runner import _bundle

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "stream"
TEAM = (FIXTURES / "diagnose-team.jsonl").read_text(encoding="utf-8").splitlines()
AUTH_ERROR = (FIXTURES / "auth-error.jsonl").read_text(encoding="utf-8").splitlines()


async def _inject(scenario_id: str) -> dict[str, Any]:
    return {}


def _ctx(use_verifier: bool = True) -> RunContext:
    return RunContext(  # type: ignore[arg-type]
        scenario_id="001-ospf-area-mismatch",
        symptom="NOC ticket:  r1 lost its\n neighbour",
        twin=None,
        verify=None,
        use_verifier=use_verifier,
    )


def test_parse_team_stream_counts_tools_agents_and_usage() -> None:
    summary = parse_stream(TEAM)
    assert summary.session_id == "11111111-2222-3333-4444-555555555555"
    assert summary.model == "claude-sonnet-5" and summary.subtype == "success"
    assert not summary.is_error and summary.error is None
    assert summary.num_turns == 9 and summary.duration_ms == 244000
    assert summary.total_cost_usd == pytest.approx(0.4321)
    assert summary.input_tokens == 1200 and summary.output_tokens == 3400
    assert summary.cache_read_tokens == 90000 and summary.cache_creation_tokens == 8000
    assert summary.agents_launched == [
        "l2-investigator",
        "l3-investigator",
        "policy-investigator",
        "change-agent",
        "verifier",
    ]
    names = [u.name for u in summary.tool_uses]
    assert names.count("Agent") == 5
    assert "mcp__twinlab__export_change" in names and "mcp__netverify__intent_check" in names
    by_parent = {u.id: u.parent_tool_use_id for u in summary.tool_uses}
    assert by_parent["toolu_apply"] == "toolu_change"
    assert by_parent["toolu_check"] == "toolu_verify"
    assert by_parent["toolu_export"] is None
    assert all(summary.tool_results.values())
    calls = summary.tool_calls
    assert {c.server for c in calls} == {"claude", "twinlab", "netverify"}
    assert sum(c.server == "netverify" for c in calls) == 2
    assert "root_cause" in summary.final_text


def test_parse_auth_error_stream_reports_the_failure() -> None:
    summary = parse_stream(AUTH_ERROR)
    assert summary.is_error and summary.error == "authentication_failed"
    assert summary.num_turns == 1 and summary.tool_uses == []
    assert summary.model == "claude-sonnet-5"


def test_parse_skips_non_json_lines() -> None:
    summary = parse_stream(["Warning: no stdin data", "", '{"type":"result","subtype":"x"}'])
    assert summary.skipped_lines == 1 and summary.events == 1 and summary.subtype == "x"


def test_extract_report_finds_the_last_report_object() -> None:
    text = parse_stream(TEAM).final_text
    report = extract_report(text)
    assert report is not None
    assert report["root_cause"]["component"] == "ospf.area"
    assert report["change_ids"] == ["d4579df2edfa1430"] and report["export_id"] == "d51b24a0865e"
    assert extract_report("no json here") is None
    assert extract_report('{"root_cause": null, "change_ids": []}')["root_cause"] is None


def test_split_tool_name() -> None:
    assert split_tool_name("mcp__twinlab__run_show_command") == ("twinlab", "run_show_command")
    assert split_tool_name("Agent") == ("claude", "Agent")


def test_prompt_and_argv_shape(tmp_path: Path) -> None:
    runner = ClaudeCliRunner(CallableAdmin(_inject, dict), cli="claude-bin", cwd=tmp_path)
    assert runner.prompt("  NOC  ticket:\n r1 down ", True) == "/diagnose NOC ticket: r1 down"
    assert runner.prompt("x", False) == "/diagnose x --no-verifier"
    solo = ClaudeCliRunner(CallableAdmin(_inject, dict), skill="diagnose-solo", cli="c")
    assert solo.prompt("x", True) == "/diagnose-solo x"
    argv = runner.argv()
    assert argv[:2] == ["claude-bin", "-p"]
    assert "--strict-mcp-config" in argv and "--verbose" in argv
    assert argv[argv.index("--model") + 1] == "sonnet"
    assert argv[argv.index("--max-turns") + 1] == "60"
    assert "mcp__twinlab,mcp__netverify,Agent" in argv[argv.index("--allowedTools") + 1]
    assert find_claude("explicit") == "explicit"


async def test_run_prefers_the_export_bundle_and_writes_a_transcript(tmp_path: Path) -> None:
    listings = [[{"export_id": "old"}], [{"export_id": "old"}, {"export_id": "abc123"}]]
    seen_prompts: list[str] = []

    async def exports() -> list[dict[str, Any]]:
        return listings.pop(0) if len(listings) > 1 else listings[0]

    async def export(export_id: str) -> dict[str, Any]:
        assert export_id == "abc123"
        return _bundle("pending")

    async def spawn(
        argv: list[str], stdin: str, cwd: Path, timeout: float
    ) -> tuple[int, list[str], str]:
        seen_prompts.append(stdin)
        assert cwd == tmp_path and timeout == 42
        return 0, TEAM, ""

    admin = CallableAdmin(_inject, dict, exports=exports, export=export)
    runner = ClaudeCliRunner(
        admin,
        cli="c",
        cwd=tmp_path,
        timeout=42,
        transcripts_dir=tmp_path / "transcripts",
        spawn=spawn,
    )
    out = await runner.run(_ctx())
    assert seen_prompts == ["/diagnose NOC ticket: r1 lost its neighbour"]
    assert out.root_cause is not None and out.root_cause.component == "link.mtu"  # bundle wins
    assert out.change_ids == ["c1"]
    assert out.verification is not None and out.verification.passed
    assert out.export == {"export_id": "abc123", "status": "pending", "decided_by": None}
    assert out.input_tokens == 1200 and out.cost_usd == pytest.approx(0.4321)
    assert len(out.tool_calls) == 13
    assert "turns=9" in out.notes and "agents=5" in out.notes and "rc=0" in out.notes
    transcript = tmp_path / "transcripts" / "001-ospf-area-mismatch.jsonl"
    assert transcript.exists() and transcript.read_text(encoding="utf-8").count("\n") == len(TEAM)


async def test_run_falls_back_to_the_final_report_without_an_export(tmp_path: Path) -> None:
    async def spawn(
        argv: list[str], stdin: str, cwd: Path, timeout: float
    ) -> tuple[int, list[str], str]:
        assert stdin.endswith(" --no-verifier")
        return 0, TEAM, ""

    runner = ClaudeCliRunner(CallableAdmin(_inject, dict), cli="c", cwd=tmp_path, spawn=spawn)
    out = await runner.run(_ctx(use_verifier=False))
    assert out.root_cause is not None and out.root_cause.component == "ospf.area"
    assert out.change_ids == ["d4579df2edfa1430"]
    assert out.verification is None and out.export is None


async def test_run_records_timeouts_and_errors(tmp_path: Path) -> None:
    async def killed(
        argv: list[str], stdin: str, cwd: Path, timeout: float
    ) -> tuple[int, list[str], str]:
        return -1, AUTH_ERROR, "killed after 5s"

    runner = ClaudeCliRunner(CallableAdmin(_inject, dict), cli="c", cwd=tmp_path, spawn=killed)
    out = await runner.run(_ctx())
    assert out.root_cause is None and out.change_ids == []
    assert "timed_out" in out.notes and "authentication_failed" in out.notes
