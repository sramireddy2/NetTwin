"""Drive the twinlab MCP server in-process through a real MCP client session."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path

import anyio
import pytest
from mcp.client.session import ClientSession
from mcp.shared.memory import create_client_server_memory_streams
from mcp.types import CallToolResult

from nettwin_core.executor import FakeExecutor
from nettwin_core.settings import Settings
from twinlab.app import TwinLab
from twinlab.server import build_server

RUNNING = "frr defaults traditional\nhostname r1\n!\nrouter ospf\nend\n"


def _scripted(node: str, argv: Sequence[str]) -> str | None:
    if argv[:2] == ("vtysh", "-c") and argv[2] == "show running-config":
        return RUNNING.replace("r1", node)
    if argv[:2] == ("vtysh", "-c"):
        return f"{node}: output of {argv[2]}"
    if argv[0] == "ip":
        return "[]"
    if argv[0] in ("nft", "bridge"):
        return ""
    return None


@pytest.fixture
def app(tmp_path: Path, fixtures: Path) -> TwinLab:
    settings = Settings.from_env(
        {
            "NETTWIN_STATE_DIR": str(tmp_path),
            "NETTWIN_TOPOLOGY": str(fixtures / "topology.clab.yml"),
        }
    )
    fake = FakeExecutor().on(_scripted)
    return TwinLab.from_settings(settings, executor=fake)


@asynccontextmanager
async def connected(app: TwinLab) -> AsyncIterator[ClientSession]:
    server = build_server(app)
    low = server._lowlevel_server
    async with (
        create_client_server_memory_streams() as (client_streams, server_streams),
        anyio.create_task_group() as tg,
    ):
        tg.start_soon(
            low.run,
            server_streams[0],
            server_streams[1],
            low.create_initialization_options(),
            True,
        )
        async with ClientSession(*client_streams) as session:
            await session.initialize()
            yield session
        tg.cancel_scope.cancel()


def _text(result: CallToolResult) -> str:
    return "".join(getattr(block, "text", "") for block in result.content)


async def test_tools_and_resource_are_listed(app: TwinLab) -> None:
    async with connected(app) as session:
        tools = {t.name for t in (await session.list_tools()).tools}
        assert tools == {
            "run_show_command",
            "snapshot",
            "list_snapshots",
            "rollback",
            "apply_config",
            "list_changes",
        }
        resources = {str(r.uri) for r in (await session.list_resources()).resources}
        assert "lab://topology" in resources
        body = await session.read_resource("lab://topology")
        payload = json.loads(body.contents[0].text)
        assert {n["name"] for n in payload["nodes"]} == {"r1", "r2", "r3", "h1"}


async def test_show_command_round_trip(app: TwinLab) -> None:
    async with connected(app) as session:
        result = await session.call_tool(
            "run_show_command", {"node": "r1", "cmd": "show ip ospf neighbor"}
        )
        assert isinstance(result, CallToolResult) and not result.is_error
        assert result.structured_content["stdout"] == "r1: output of show ip ospf neighbor"
        assert result.structured_content["command"] == "vtysh -c show ip ospf neighbor"


async def test_show_command_rejections_are_tool_errors(app: TwinLab) -> None:
    async with connected(app) as session:
        unknown = await session.call_tool(
            "run_show_command", {"node": "r9", "cmd": "show ip route"}
        )
        assert unknown.is_error and "unknown node" in _text(unknown)
        denied = await session.call_tool(
            "run_show_command", {"node": "r1", "cmd": "configure terminal"}
        )
        assert denied.is_error and "Allowed forms" in _text(denied)


async def test_snapshot_list_and_rollback(app: TwinLab) -> None:
    async with connected(app) as session:
        snap = await session.call_tool("snapshot", {})
        assert not snap.is_error
        snapshot_id = snap.structured_content["id"]
        assert len(snapshot_id) == 64
        listed = await session.call_tool("list_snapshots", {})
        assert listed.structured_content["snapshots"] == [snapshot_id]
        rolled = await session.call_tool("rollback", {"snapshot_id": snapshot_id[:12]})
        assert not rolled.is_error, _text(rolled)
        assert rolled.structured_content["matches_target"] is True
        assert rolled.structured_content["changed_nodes"] == {}
        missing = await session.call_tool("rollback", {"snapshot_id": "0" * 12})
        assert missing.is_error and "unknown snapshot" in _text(missing)


async def test_apply_config_records_a_change(app: TwinLab) -> None:
    async with connected(app) as session:
        applied = await session.call_tool(
            "apply_config",
            {
                "node": "r1",
                "ops": [{"kind": "set_mtu", "iface": "eth1", "mtu": 1400}],
                "rationale": "test",
            },
        )
        assert not applied.is_error, _text(applied)
        change_id = applied.structured_content["change_id"]
        assert applied.structured_content["node"] == "r1"
        listed = await session.call_tool("list_changes", {})
        assert listed.structured_content["changes"] == [change_id]
        bad = await session.call_tool(
            "apply_config", {"node": "r1", "ops": [{"kind": "shell", "cmd": "id"}]}
        )
        assert bad.is_error
