"""The local Ollama runner: tool bridging, role allowlists, the chat loop and its output.

A scripted chat plays the model. Everything else is real: in-memory twinlab and netverify
servers, the role and skill files, and the scorer.
"""

from __future__ import annotations

import copy
import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest

from netbench.cli import config_for
from netbench.clients import ToolClient, memory_session
from netbench.local_agent import (
    LAUNCH_AGENT,
    READ_TOPOLOGY,
    LocalRunner,
    OllamaChat,
    ToolLoop,
    Toolset,
    mcp_tools,
    truncate,
)
from netbench.runner import RunContext, RunnerUnavailable
from netbench.scoring import score_run
from nettwin_core.executor import FakeExecutor
from nettwin_core.settings import Settings
from netverify.app import NetVerify
from netverify.server import build_server as build_netverify
from tests.unit.test_netbench import SCENARIOS, _env, _twin_scripted
from tests.unit.test_netverify import make_fake
from twinlab.app import TwinLab
from twinlab.server import build_server as build_twinlab

ROOT = Path(__file__).resolve().parents[2]
ROLES_DIR = ROOT / ".claude" / "agents"
SKILLS_DIR = ROOT / ".claude" / "skills"
SCENARIO_ID = "004-ospf-mtu-mismatch"
SYMPTOM = "NOC ticket:  the r1-r2 adjacency\n never reaches Full"
ROOT_CAUSE = {"node": "r2", "layer": "L2", "component": "link.mtu", "summary": "r2 eth1 is 1400"}
FIX = {
    "node": "r2",
    "ops": [{"kind": "set_mtu", "iface": "eth1", "mtu": 1500}],
    "rationale": "restore the MTU",
}
COMMANDER_TOOLS = {
    READ_TOPOLOGY,
    "mcp__twinlab__snapshot",
    "mcp__twinlab__rollback",
    "mcp__twinlab__export_change",
    LAUNCH_AGENT,
}

Turn = dict[str, Any] | Callable[[list[dict[str, Any]]], dict[str, Any]]


class ScriptedChat:
    """Plays assistant messages back in order; a turn may be a function of the messages so
    far, the way a model reads a change id or a report out of an earlier tool result."""

    def __init__(self, *turns: Turn) -> None:
        self.turns = list(turns)
        self.requests: list[dict[str, Any]] = []

    async def __call__(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.requests.append(copy.deepcopy(payload))  # the loop keeps mutating its message list
        assert self.turns, "the script ran out of turns"
        turn = self.turns.pop(0)
        message = turn(payload["messages"]) if callable(turn) else turn
        return {
            "model": payload["model"],
            "message": message,
            "done": True,
            "prompt_eval_count": 100,
            "eval_count": 10,
        }


def _call(name: str, **args: Any) -> dict[str, Any]:
    return {"function": {"name": name, "arguments": args}}


def _says(content: str = "", *calls: dict[str, Any]) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if calls:
        message["tool_calls"] = list(calls)
    return message


def _tool_results(messages: list[dict[str, Any]], tool: str) -> list[str]:
    return [
        m["content"] for m in messages if m.get("role") == "tool" and m.get("tool_name") == tool
    ]


def _last_result(messages: list[dict[str, Any]], tool: str) -> dict[str, Any]:
    return json.loads(_tool_results(messages, tool)[-1])


def _tool_names(request: dict[str, Any]) -> set[str]:
    return {t["function"]["name"] for t in request["tools"]}


@asynccontextmanager
async def _servers(tmp_path: Path) -> AsyncIterator[tuple[ToolClient, ToolClient, TwinLab]]:
    settings = Settings.from_env(_env(tmp_path))
    twin_app = TwinLab.from_settings(settings, executor=FakeExecutor().on(_twin_scripted))
    verify_app = NetVerify.from_settings(settings, executor=make_fake())
    async with (
        memory_session(build_twinlab(twin_app)) as twin_s,
        memory_session(build_netverify(verify_app)) as verify_s,
    ):
        yield ToolClient(twin_s, "twinlab"), ToolClient(verify_s, "netverify"), twin_app


def _runner(script: ScriptedChat, skill: str, tmp_path: Path, **kwargs: Any) -> LocalRunner:
    return LocalRunner(
        OllamaChat("qwen3:14b", transport=script),
        roles_dir=ROLES_DIR,
        skills_dir=SKILLS_DIR,
        skill=skill,
        transcripts_dir=tmp_path / "transcripts",
        **kwargs,
    )


def _ctx(twin: ToolClient, verify: ToolClient, use_verifier: bool = True) -> RunContext:
    return RunContext(
        scenario_id=SCENARIO_ID,
        symptom=SYMPTOM,
        twin=twin,
        verify=verify,
        use_verifier=use_verifier,
        config_name="local",
        trial=1,
    )


def _export(messages: list[dict[str, Any]]) -> dict[str, Any]:
    """The model exports with the change id and the report it read from earlier results."""
    change = _last_result(messages, "mcp__twinlab__apply_config")
    report = _last_result(messages, "mcp__netverify__intent_check")
    return _says(
        "",
        _call(
            "mcp__twinlab__export_change",
            change_ids=[change["change_id"]],
            verification=report,
            root_cause=ROOT_CAUSE,
            summary="r2 eth1 back to 1500.",
        ),
    )


FINAL_JSON = json.dumps(
    {"root_cause": ROOT_CAUSE, "change_ids": ["made-up"], "verified": True, "exported": True}
)


async def test_solo_run_drives_the_twin_and_yields_a_scorable_output(tmp_path: Path) -> None:
    script = ScriptedChat(
        _says("baseline first", _call(READ_TOPOLOGY), _call("mcp__twinlab__snapshot")),
        _says("", _call("mcp__twinlab__run_show_command", node="r2", cmd="ip -d link show eth1")),
        _says("", _call("mcp__twinlab__apply_config", **FIX)),
        _says(
            "",
            _call("mcp__netverify__wait_converged", timeout=60),
            _call("mcp__netverify__intent_check"),
        ),
        _export,
        _says(f"Incident report: r2 eth1 MTU.\n{FINAL_JSON}"),
    )
    async with _servers(tmp_path) as (twin, verify, twin_app):
        out = await _runner(script, "diagnose-solo", tmp_path).run(_ctx(twin, verify))
        changes = [twin_app.changes.load(c) for c in out.change_ids]

    assert out.root_cause is not None
    assert (out.root_cause.node, out.root_cause.component) == ("r2", "link.mtu")
    assert len(out.change_ids) == 1 and out.change_ids != ["made-up"]  # tool calls, not claims
    assert out.verification is not None and out.verification.passed
    assert out.export is not None and out.export["status"] == "approved"
    assert out.export["decided_by"] == "bench-auto"
    assert out.input_tokens == 600 and out.output_tokens == 60
    assert [c.tool for c in out.tool_calls] == [
        "snapshot",
        "run_show_command",
        "apply_config",
        "export_change",
        "wait_converged",
        "intent_check",
    ]
    assert "turns=6" in out.notes and "agents=0" in out.notes and "max_turns" not in out.notes

    first = script.requests[0]
    assert first["model"] == "qwen3:14b" and first["stream"] is False and first["think"] is False
    assert first["options"] == {"temperature": 0, "num_ctx": 16384}
    system, user = first["messages"][:2]
    assert system["role"] == "system" and "no subagents" in system["content"]
    assert "$ARGUMENTS" not in system["content"]
    assert "NOC ticket: the r1-r2 adjacency never reaches Full" in system["content"]
    assert user == {
        "role": "user",
        "content": "/diagnose-solo NOC ticket: the r1-r2 adjacency never reaches Full",
    }
    names = _tool_names(first)
    assert {READ_TOPOLOGY, "mcp__twinlab__export_change", "mcp__netverify__intent_check"} <= names
    assert LAUNCH_AGENT not in names
    topology = _tool_results(script.requests[1]["messages"], READ_TOPOLOGY)[0]
    assert '"nodes"' in topology

    scenario = next(s for s in SCENARIOS if s.id == SCENARIO_ID)
    score = score_run(scenario, out, changes, [], [])
    assert score.root_cause and score.verified and score.exported and score.minimal

    saved = json.loads(
        (tmp_path / "transcripts" / f"{SCENARIO_ID}.local.1.json").read_text(encoding="utf-8")
    )
    assert saved["turns"] == 6 and saved["subagents"] == [] and saved["model"] == "qwen3:14b"
    assert saved["messages"][-1]["content"].endswith(FINAL_JSON)
    assert sum(m["role"] == "tool" for m in saved["messages"]) == 7


async def test_no_verifier_removes_export_and_netverify_and_refuses_them(tmp_path: Path) -> None:
    script = ScriptedChat(
        _says("", _call(READ_TOPOLOGY), _call("mcp__twinlab__apply_config", **FIX)),
        _says("", _call("mcp__netverify__intent_check"), _call("Bash", command="ls")),
        _says(json.dumps({"root_cause": ROOT_CAUSE, "change_ids": [], "verified": False})),
    )
    async with _servers(tmp_path) as (twin, verify, _):
        runner = _runner(script, "diagnose-solo", tmp_path)
        out = await runner.run(_ctx(twin, verify, use_verifier=False))

    names = _tool_names(script.requests[0])
    assert "mcp__twinlab__apply_config" in names and "mcp__twinlab__export_change" not in names
    assert not any(n.startswith("mcp__netverify__") for n in names)
    system, user = script.requests[0]["messages"][:2]
    assert "never reaches Full --no-verifier" in system["content"]
    assert user["content"].endswith(" --no-verifier")
    refused = [m["content"] for m in script.requests[2]["messages"] if m.get("role") == "tool"][-2:]
    assert refused[0].startswith("error:") and "mcp__netverify__intent_check" in refused[0]
    assert refused[1].startswith("error:") and "'Bash'" in refused[1]
    assert verify.calls == [] and out.verification is None and out.export is None
    assert len(out.change_ids) == 1
    assert out.root_cause is not None and out.root_cause.node == "r2"


async def test_team_mode_runs_each_role_in_a_nested_loop_with_its_own_tools(
    tmp_path: Path,
) -> None:
    def change_agent_final(messages: list[dict[str, Any]]) -> dict[str, Any]:
        change = _last_result(messages, "mcp__twinlab__apply_config")
        return _says(json.dumps({"root_cause": ROOT_CAUSE, "change_ids": [change["change_id"]]}))

    def verifier_final(messages: list[dict[str, Any]]) -> dict[str, Any]:
        report = _last_result(messages, "mcp__netverify__intent_check")
        return _says(f"{json.dumps(report)}\n{json.dumps({'verdict': 'pass'})}")

    def commander_export(messages: list[dict[str, Any]]) -> dict[str, Any]:
        launched = _tool_results(messages, LAUNCH_AGENT)
        change = json.loads(launched[-2])
        report = json.loads(launched[-1].splitlines()[0])
        return _says(
            "",
            _call(
                "mcp__twinlab__export_change",
                change_ids=change["change_ids"],
                verification=report,
                root_cause=ROOT_CAUSE,
                summary="s",
            ),
        )

    script = ScriptedChat(
        # commander: baseline, then a role that does not exist
        _says(
            "",
            _call(READ_TOPOLOGY),
            _call("mcp__twinlab__snapshot"),
            _call(LAUNCH_AGENT, subagent_type="l7-investigator", prompt="?"),
        ),
        _says("", _call(LAUNCH_AGENT, subagent_type="l2-investigator", prompt="symptom + topo")),
        # nested l2-investigator
        _says("", _call("mcp__twinlab__run_show_command", node="r2", cmd="ip -d link show eth1")),
        _says(json.dumps({"layer": "L2", "root_cause_candidate": ROOT_CAUSE})),
        # commander
        _says("", _call(LAUNCH_AGENT, subagent_type="change-agent", prompt="fix r2 link.mtu")),
        # nested change-agent: applies, then tries to export anyway
        _says(
            "",
            _call("mcp__twinlab__apply_config", **FIX),
            _call("mcp__twinlab__export_change", change_ids=[], verification={}, root_cause={}),
        ),
        change_agent_final,
        # commander
        _says("", _call(LAUNCH_AGENT, subagent_type="verifier", prompt="S0 / S1")),
        # nested verifier
        _says(
            "",
            _call("mcp__netverify__wait_converged", timeout=60),
            _call("mcp__netverify__intent_check"),
        ),
        verifier_final,
        # commander: export, then a report without the JSON line, twice nudged, still without
        commander_export,
        _says("Exported. Root cause on r2."),
        _says("The change is exported; nothing more to add."),
        _says("Root cause on r2, as reported."),
    )
    async with _servers(tmp_path) as (twin, verify, _):
        out = await _runner(script, "diagnose", tmp_path).run(_ctx(twin, verify))

    requests = script.requests
    assert len(requests) == 14
    for index in (0, 1, 4, 7, 10, 11):
        assert _tool_names(requests[index]) == COMMANDER_TOOLS, index
    assert requests[1]["messages"][0]["content"].count("incident commander") == 1
    launched = _tool_results(requests[1]["messages"], LAUNCH_AGENT)
    assert launched[0].startswith("error:") and "l7-investigator" in launched[0]
    assert "change-agent" in launched[0] and "verifier" in launched[0]

    l2 = requests[2]
    assert _tool_names(l2) == {"mcp__twinlab__run_show_command"}
    assert l2["messages"][0]["role"] == "system" and "Layer 2" in l2["messages"][0]["content"]
    assert l2["messages"][1] == {"role": "user", "content": "symptom + topo"}
    change = _tool_names(requests[5])
    assert {"mcp__twinlab__apply_config", "mcp__twinlab__rollback"} <= change
    assert "mcp__twinlab__export_change" not in change
    assert not any(n.startswith("mcp__netverify__") for n in change)
    refused = _tool_results(requests[6]["messages"], "mcp__twinlab__export_change")
    assert refused == [refused[0]] and refused[0].startswith("error:")
    verifier = _tool_names(requests[8])
    assert verifier and all(n.startswith("mcp__netverify__") for n in verifier)
    assert READ_TOPOLOGY not in verifier and LAUNCH_AGENT not in verifier

    assert out.root_cause is not None and out.root_cause.component == "link.mtu"  # from export
    assert len(out.change_ids) == 1
    assert out.verification is not None and out.verification.passed
    assert out.export is not None and out.export["status"] == "approved"
    assert "agents=3" in out.notes and "no_final_json" in out.notes and "nudges=2" in out.notes
    assert "launched=l2-investigator,change-agent,verifier" in out.notes
    assert [c.tool for c in out.tool_calls] == [
        "snapshot",
        "run_show_command",
        "apply_config",
        "export_change",
        "wait_converged",
        "intent_check",
    ]
    saved = json.loads(
        (tmp_path / "transcripts" / f"{SCENARIO_ID}.local.1.json").read_text(encoding="utf-8")
    )
    assert [s["subagent_type"] for s in saved["subagents"]] == [
        "l2-investigator",
        "change-agent",
        "verifier",
    ]
    assert saved["subagents"][1]["turns"] == 2 and saved["subagents"][1]["messages"]


async def test_team_mode_refuses_the_verifier_when_the_ablation_is_on(tmp_path: Path) -> None:
    script = ScriptedChat(
        _says("", _call(LAUNCH_AGENT, subagent_type="verifier", prompt="S0 / S1")),
        _says(json.dumps({"root_cause": None, "change_ids": []})),
    )
    async with _servers(tmp_path) as (twin, verify, _):
        out = await _runner(script, "diagnose", tmp_path).run(
            _ctx(twin, verify, use_verifier=False)
        )

    assert _tool_names(script.requests[0]) == COMMANDER_TOOLS - {"mcp__twinlab__export_change"}
    answer = script.requests[1]["messages"][-1]
    assert answer["role"] == "tool" and answer["content"].startswith("error:")
    assert "disabled" in answer["content"] and "--no-verifier" in answer["content"]
    assert out.root_cause is None and "agents=0" in out.notes and verify.calls == []


async def test_tool_results_are_truncated_with_a_visible_marker(tmp_path: Path) -> None:
    assert truncate("abc", 3) == "abc"
    assert truncate("x" * 7000, 6000).endswith("\n... [truncated 1000 chars]")
    async with _servers(tmp_path) as (twin, _, _):
        assert {t.name for t in await twin.list_tools()} >= {"snapshot", "apply_config"}
        tools = Toolset(await mcp_tools(twin), result_cap=40)
        text = await tools.dispatch("mcp__twinlab__snapshot", {})
    assert text.startswith('{\n  "id": "') and "\n... [truncated " in text
    assert len(text.split("\n... [truncated ")[0]) == 40


async def test_max_turns_stops_the_loop_and_is_noted(tmp_path: Path) -> None:
    script = ScriptedChat(*[_says("again", _call("mcp__twinlab__snapshot"))] * 3)
    async with _servers(tmp_path) as (twin, verify, _):
        runner = _runner(script, "diagnose-solo", tmp_path, max_turns=2)
        out = await runner.run(_ctx(twin, verify))
    assert len(script.requests) == 2 and len(twin.calls) == 2
    assert "turns=2" in out.notes and "max_turns_hit" in out.notes and out.root_cause is None


async def test_loop_accepts_string_arguments_and_reports_bad_ones(tmp_path: Path) -> None:
    script = ScriptedChat(
        _says(
            "",
            {
                "function": {
                    "name": "mcp__twinlab__run_show_command",
                    "arguments": '{"node": "r1", "cmd": "show ip route"}',
                }
            },
            {"function": {"name": "mcp__twinlab__snapshot", "arguments": "{not json"}},
            {"function": {"name": "mcp__twinlab__run_show_command", "arguments": {"node": "r9"}}},
        ),
        _says("done"),
    )
    async with _servers(tmp_path) as (twin, _, _):
        loop = ToolLoop(
            OllamaChat("m", transport=script), Toolset(await mcp_tools(twin)), max_turns=3
        )
        result = await loop.run("system", "user")
    results = [m["content"] for m in result.messages if m.get("role") == "tool"]
    assert '"stdout"' in results[0]
    assert results[1].startswith("error:") and "JSON object" in results[1]
    assert results[2].startswith("error:")  # the server's own validation error, passed through
    assert result.final_text == "done" and result.turns == 2 and not result.hit_max_turns


def _http_chat(handler: Callable[[httpx.Request], httpx.Response], **kwargs: Any) -> OllamaChat:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return OllamaChat(kwargs.pop("model", "qwen3:14b"), client=client, **kwargs)


async def test_ollama_connection_failure_stops_the_matrix() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    with pytest.raises(RunnerUnavailable, match="cannot reach Ollama at http://localhost:11434"):
        await _http_chat(refuse).chat([{"role": "user", "content": "hi"}], [])


async def test_ollama_missing_model_and_non_json_answers_stop_the_matrix() -> None:
    def missing(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": "model 'qwen3:14b' not found"})

    def proxy(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>not ollama</html>")

    def busted(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"done": True})

    with pytest.raises(RunnerUnavailable, match="ollama pull qwen3:14b"):
        await _http_chat(missing).chat([], [])
    with pytest.raises(RunnerUnavailable, match="did not answer JSON"):
        await _http_chat(proxy).chat([], [])
    with pytest.raises(RunnerUnavailable, match="without a message"):
        await _http_chat(busted).chat([], [])


async def test_ollama_request_shape_and_think_fallback() -> None:
    seen: list[dict[str, Any]] = []

    def serve(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "http://ollama.test:11434/api/chat"
        body = json.loads(request.content)
        seen.append(body)
        if "think" in body:
            return httpx.Response(
                400, json={"error": '"qwen2.5-coder:7b" does not support thinking'}
            )
        return httpx.Response(
            200,
            json={
                "message": {"role": "assistant", "content": "hello"},
                "prompt_eval_count": 7,
                "eval_count": 3,
                "done": True,
            },
        )

    chat = _http_chat(
        serve, model="qwen2.5-coder:7b", base_url="http://ollama.test:11434/", num_ctx=8192
    )
    tools = [{"type": "function", "function": {"name": "f", "parameters": {}}}]
    message = await chat.chat([{"role": "user", "content": "hi"}], tools)
    assert message["content"] == "hello"
    assert seen[0]["think"] is False and "think" not in seen[1]
    assert seen[1]["model"] == "qwen2.5-coder:7b" and seen[1]["stream"] is False
    assert seen[1]["options"] == {"temperature": 0, "num_ctx": 8192} and seen[1]["tools"] == tools
    assert chat.prompt_tokens == 7 and chat.completion_tokens == 3 and chat.requests == 1
    await chat.chat([{"role": "user", "content": "again"}], [])
    assert len(seen) == 3 and "think" not in seen[2] and chat.prompt_tokens == 14


def test_cli_builds_the_local_row_label_and_config() -> None:
    solo = config_for("local", "diagnose-solo", "qwen3:14b", no_verifier=True)
    assert solo.name == "local-diagnose-solo-qwen3-14b-noverify"
    assert solo.runner == "local" and solo.model == "qwen3:14b"
    assert solo.multi_agent is False and solo.verifier is False
    team = config_for("local", "diagnose", "hf.co/org/model:Q4", no_verifier=False)
    assert team.name == "local-diagnose-hf.co-org-model-Q4" and team.multi_agent is True
    assert config_for("claude", "diagnose", "sonnet", False).name == "claude-diagnose-sonnet"
    fake = config_for("fake", "diagnose", "sonnet", False)
    assert fake.name == "fake" and fake.model is None and fake.multi_agent is None
    assert config_for("fake", "diagnose", "x", True, name="custom").name == "custom"


def test_unknown_skill_is_rejected_up_front() -> None:
    with pytest.raises(ValueError, match="unknown skill"):
        LocalRunner(OllamaChat("m"), roles_dir=ROLES_DIR, skills_dir=SKILLS_DIR, skill="fix-it")


def test_tool_calls_written_as_text_are_recognised() -> None:
    from netbench.local_agent import text_tool_calls

    bare = json.dumps(
        {
            "name": "mcp__twinlab__run_show_command",
            "arguments": {"node": "r1", "cmd": "show ip ospf"},
        }
    )
    assert text_tool_calls(bare) == [
        {
            "function": {
                "name": "mcp__twinlab__run_show_command",
                "arguments": {"node": "r1", "cmd": "show ip ospf"},
            }
        }
    ]
    fence = "`" * 3
    fenced = (
        fence
        + "json"
        + chr(10)
        + json.dumps([{"function": {"name": "read_topology", "arguments": {}}}])
        + chr(10)
        + fence
    )
    assert [c["function"]["name"] for c in text_tool_calls(fenced)] == ["read_topology"]
    assert text_tool_calls("Root cause: r3 area mismatch.") == []
    assert text_tool_calls(json.dumps({"root_cause": {"node": "r3"}, "change_ids": []})) == []


async def test_model_timeout_is_a_run_failure_not_unavailability() -> None:
    import httpx

    from netbench.local_agent import ModelTimeout, OllamaChat

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    chat = OllamaChat("m", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    with pytest.raises(ModelTimeout):
        await chat.chat([{"role": "user", "content": "hi"}], [])
    assert not issubclass(ModelTimeout, RunnerUnavailable)


async def test_empty_or_unfinished_answers_are_nudged_at_most_twice() -> None:
    from netbench.local_agent import MAX_NUDGES, NUDGE, ToolLoop, Toolset

    final = {
        "role": "assistant",
        "content": 'Done. {"root_cause": {"node": "r3"}, "change_ids": []}',
    }
    script = ScriptedChat(
        {"role": "assistant", "content": ""},
        {"role": "assistant", "content": "Let me think about the next step."},
        final,
    )
    loop = ToolLoop(OllamaChat("m", transport=script), Toolset([]), max_turns=6, expect_report=True)
    result = await loop.run("system", "user")
    assert result.nudges == 2 and result.turns == 3 and not result.hit_max_turns
    assert '"root_cause"' in result.final_text
    assert [m["content"] for m in script.requests[2]["messages"] if m["role"] == "user"][1:] == [
        NUDGE,
        NUDGE,
    ]

    stubborn = ScriptedChat(*[{"role": "assistant", "content": ""}] * (MAX_NUDGES + 1))
    loop = ToolLoop(OllamaChat("m", transport=stubborn), Toolset([]), max_turns=6)
    result = await loop.run("system", "user")
    assert (
        result.nudges == MAX_NUDGES and result.final_text == "" and result.turns == MAX_NUDGES + 1
    )

    prose = ScriptedChat({"role": "assistant", "content": "Findings: eth1 area mismatch on r3."})
    loop = ToolLoop(OllamaChat("m", transport=prose), Toolset([]), max_turns=6)  # a role loop
    assert (await loop.run("system", "user")).nudges == 0


async def test_topology_result_is_never_truncated() -> None:
    from netbench.local_agent import BoundTool, Toolset

    async def big(args: dict[str, Any]) -> str:
        return "x" * 500

    tools = Toolset(
        [
            BoundTool("read_topology", "", {"type": "object", "properties": {}}, big, cap=0),
            BoundTool("show", "", {"type": "object", "properties": {}}, big),
        ],
        result_cap=100,
    )
    assert len(await tools.dispatch("read_topology", {})) == 500
    assert "[truncated 400 chars]" in await tools.dispatch("show", {})
