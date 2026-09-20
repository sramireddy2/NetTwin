"""Drive the netverify MCP server in-process through a real MCP client session."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import anyio
import pytest
from mcp.client.session import ClientSession
from mcp.shared.memory import create_client_server_memory_streams
from mcp.types import CallToolResult

from nettwin_core.models import NodeState, Snapshot
from netverify.app import NetVerify
from netverify.server import build_server
from tests.unit.test_netverify import make_app, make_fake


@pytest.fixture
def app(tmp_path: Path) -> NetVerify:
    return make_app(tmp_path, make_fake())


@asynccontextmanager
async def connected(app: NetVerify) -> AsyncIterator[ClientSession]:
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


async def test_tools_are_all_read_only(app: NetVerify) -> None:
    async with connected(app) as session:
        tools = (await session.list_tools()).tools
        assert {t.name for t in tools} == {
            "wait_converged",
            "reachability_matrix",
            "route_diff",
            "intent_check",
        }
        assert all(t.annotations and t.annotations.read_only_hint for t in tools)
        resources = {str(r.uri) for r in (await session.list_resources()).resources}
        assert "policy://intent" in resources


async def test_intent_check_and_route_diff_over_mcp(app: NetVerify) -> None:
    # Routes are derived state and excluded from the id, so vary a configured field too.
    before = Snapshot.build(
        "t", {"r1": NodeState(running_config="a", routes=[{"dst": "10.0.0.0/24", "dev": "eth1"}])}
    )
    after = Snapshot.build(
        "t", {"r1": NodeState(running_config="b", routes=[{"dst": "10.0.1.0/24", "dev": "eth1"}])}
    )
    app.store.save(before)
    app.store.save(after)
    async with connected(app) as session:
        matrix = await session.call_tool("reachability_matrix", {})
        assert not matrix.is_error and matrix.structured_content["failed"] == 2
        diff = await session.call_tool(
            "route_diff", {"before_id": before.id[:12], "after_id": after.id[:12]}
        )
        assert not diff.is_error, _text(diff)
        assert diff.structured_content["nodes"]["r1"]["added"] == ["10.0.1.0/24 dev eth1"]
        missing = await session.call_tool(
            "route_diff", {"before_id": "0" * 12, "after_id": after.id}
        )
        assert missing.is_error
        report = await session.call_tool("intent_check", {}, read_timeout_seconds=30)
        assert not report.is_error, _text(report)
        assert report.structured_content["passed"] is True
        assert (
            report.structured_content["attestation"]["snapshot_id"]
            == report.structured_content["snapshot_id"]
        )
