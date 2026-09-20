"""The Claude Code role files must match the servers they name.

Claude Code enforces the allowlists in `.claude/agents/*.md`; these tests prove the files
say what the design says (investigators read only, the change agent cannot export, the
verifier sees only netverify) and that every MCP tool they name really exists.
"""

from __future__ import annotations

from pathlib import Path

from netbench.clients import memory_session
from netbench.roles import load_roles, load_skills, parse_role, server_of
from nettwin_core.executor import FakeExecutor
from nettwin_core.settings import Settings
from netverify.app import NetVerify
from netverify.server import build_server as build_netverify
from tests.unit.test_netbench import _env, _twin_scripted
from tests.unit.test_netverify import make_fake
from twinlab.app import TwinLab
from twinlab.server import build_server as build_twinlab

ROOT = Path(__file__).resolve().parents[2]
ROLES = load_roles(ROOT / ".claude" / "agents")
SKILLS = load_skills(ROOT / ".claude" / "skills")
INVESTIGATORS = ("l2-investigator", "l3-investigator", "policy-investigator")
FORBIDDEN_BUILTINS = {
    "Agent",
    "Bash",
    "Edit",
    "Glob",
    "Grep",
    "MultiEdit",
    "NotebookEdit",
    "Read",
    "Task",
    "WebFetch",
    "WebSearch",
    "Write",
}


async def _server_tools(tmp_path: Path) -> dict[str, set[str]]:
    settings = Settings.from_env(_env(tmp_path))
    twin = TwinLab.from_settings(settings, executor=FakeExecutor().on(_twin_scripted))
    verify = NetVerify.from_settings(settings, executor=make_fake())
    tools: dict[str, set[str]] = {}
    async with memory_session(build_twinlab(twin)) as session:
        tools["twinlab"] = {t.name for t in (await session.list_tools()).tools}
    async with memory_session(build_netverify(verify)) as session:
        tools["netverify"] = {t.name for t in (await session.list_tools()).tools}
    return tools


def test_role_files_are_present_and_bounded() -> None:
    assert set(ROLES) == {*INVESTIGATORS, "change-agent", "verifier"}
    for role in ROLES.values():
        assert role.model == "inherit", role.name
        assert role.description and role.body, role.name
        assert role.max_turns, role.name
        assert role.tools, f"{role.name} must carry an explicit allowlist"
        assert not set(role.builtin_tools) & FORBIDDEN_BUILTINS, role.name


async def test_every_mcp_tool_named_in_a_role_exists(tmp_path: Path) -> None:
    tools = await _server_tools(tmp_path)
    for role in ROLES.values():
        for tool in role.mcp_tools + role.disallowed_tools:
            server = server_of(tool)
            name = tool.split("__", 2)[2]
            assert name in tools[server], f"{role.name}: {tool} is not a {server} tool"


def test_investigators_only_run_show_commands() -> None:
    for name in INVESTIGATORS:
        assert set(ROLES[name].tools) == {"mcp__twinlab__run_show_command"}, name


def test_change_agent_can_change_but_not_verify_or_export() -> None:
    role = ROLES["change-agent"]
    assert {"mcp__twinlab__apply_config", "mcp__twinlab__rollback"} <= set(role.tools)
    assert "mcp__twinlab__export_change" not in role.tools
    assert "mcp__twinlab__export_change" in role.disallowed_tools
    assert role.servers == {"twinlab"}


def test_verifier_sees_only_netverify() -> None:
    role = ROLES["verifier"]
    assert role.mcp_servers == ("netverify",)
    assert role.servers == {"netverify"}
    assert "mcp__netverify__intent_check" in role.tools


def test_skills_reference_every_role_and_the_ablation_flag() -> None:
    assert set(SKILLS) == {"diagnose", "diagnose-solo"}
    team = SKILLS["diagnose"].body
    for name in ROLES:
        assert f"`{name}`" in team, name
    assert "$ARGUMENTS" in team and "--no-verifier" in team
    solo = SKILLS["diagnose-solo"].body
    assert "no subagents" in solo and "$ARGUMENTS" in solo and "--no-verifier" in solo
    for name in ROLES:
        assert f"`{name}`" not in solo, f"solo skill must not delegate to {name}"


def test_frontmatter_accepts_both_list_forms(tmp_path: Path) -> None:
    path = tmp_path / "x.md"
    path.write_text(
        "---\nname: x\ntools: [A, mcp__s__t]\ndisallowedTools: B, C\nmaxTurns: 3\n---\nbody\n",
        encoding="utf-8",
    )
    role = parse_role(path)
    assert role.tools == ("A", "mcp__s__t") and role.disallowed_tools == ("B", "C")
    assert role.builtin_tools == ("A",) and role.mcp_tools == ("mcp__s__t",)
    assert role.servers == {"s"} and role.max_turns == 3 and role.body == "body\n"
