"""How the harness and runners talk to the servers: MCP sessions, in memory or over HTTP.

Every runner sees only `ToolClient`, so the same fake agent drives an in-process server in
CI and the real servers on the lab host without knowing which.
"""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator, Callable
from contextlib import AsyncExitStack, asynccontextmanager, suppress
from dataclasses import dataclass
from typing import Any

import anyio
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.server.mcpserver import MCPServer
from mcp.shared.exceptions import MCPError
from mcp.shared.memory import create_client_server_memory_streams
from mcp.types import CallToolResult, Tool

log = logging.getLogger("netbench.clients")


@dataclass(frozen=True)
class CallResult:
    tool: str
    args: dict[str, Any]
    ok: bool
    data: dict[str, Any] | None
    text: str
    duration_ms: int

    def require(self) -> dict[str, Any]:
        if not self.ok or self.data is None:
            raise ToolCallFailed(f"{self.tool} failed: {self.text or 'no output'}")
        return self.data


class ToolCallFailed(RuntimeError):
    pass


class ToolClient:
    """Thin wrapper over an MCP `ClientSession` that records every call."""

    #: Seconds past the request timeout before a call is abandoned outright.
    hard_margin: float = 30.0

    def __init__(self, session: ClientSession, name: str = "server") -> None:
        self.session = session
        self.name = name
        self.calls: list[CallResult] = []

    async def call(
        self, tool: str, args: dict[str, Any] | None = None, *, timeout: float = 180
    ) -> CallResult:
        args = args or {}
        started = time.monotonic()
        # The request-level timeout has been seen to let a call block for 26 minutes once the
        # transport's event stream had dropped; the outer bound is what actually returns.
        with anyio.fail_after(timeout + self.hard_margin):
            result = await self.session.call_tool(tool, args, read_timeout_seconds=timeout)
        duration = int((time.monotonic() - started) * 1000)
        if isinstance(result, CallToolResult):
            call = CallResult(
                tool=tool,
                args=args,
                ok=not result.is_error,
                data=result.structured_content,
                text="".join(getattr(b, "text", "") for b in result.content),
                duration_ms=duration,
            )
        else:  # pragma: no cover - input-required or claimed results are not used here
            call = CallResult(
                tool, args, False, None, f"unexpected result {type(result).__name__}", duration
            )
        self.calls.append(call)
        return call

    async def read_resource(self, uri: str) -> str:
        body = await self.session.read_resource(uri)
        return "".join(getattr(c, "text", "") for c in body.contents)

    async def list_tools(self) -> list[Tool]:
        """The server's tool definitions, so a runner can hand them to a model as functions."""
        return (await self.session.list_tools()).tools


@asynccontextmanager
async def memory_session(
    server: MCPServer, elicitation_callback: Callable[..., Any] | None = None
) -> AsyncIterator[ClientSession]:
    """Connect to an `MCPServer` in-process (no transport), as the unit tests do."""
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
        async with ClientSession(*client_streams, elicitation_callback=elicitation_callback) as s:
            await s.initialize()
            yield s
        tg.cancel_scope.cancel()


class HttpToolClient(ToolClient):
    """A `ToolClient` over streamable HTTP that owns its session and can reopen it.

    The harness's sessions idle while an agent works; the server-to-client event stream
    drops after about ten minutes of that and the next call can hang. `reconnect()` throws
    the session away and opens a fresh one, bounded so a wedged transport cannot hold it.
    """

    def __init__(self, url: str, name: str = "server") -> None:
        super().__init__(session=None, name=name)  # type: ignore[arg-type]
        self.url = url
        self._stack: AsyncExitStack | None = None

    async def connect(self) -> None:
        stack = AsyncExitStack()
        self.session = await stack.enter_async_context(http_session(self.url))
        self._stack = stack

    async def aclose(self) -> None:
        stack, self._stack = self._stack, None
        if stack is not None:
            with anyio.move_on_after(15), suppress(Exception):
                await stack.aclose()

    async def reconnect(self) -> None:
        await self.aclose()
        await self.connect()

    async def call(
        self, tool: str, args: dict[str, Any] | None = None, *, timeout: float = 180
    ) -> CallResult:
        """One call, and on a wedged transport one more over a fresh session.

        Every caller gets this: the local runner's tool calls, the harness's reset and
        change lookups, not only the post-run checks. A second failure is the caller's.
        """
        try:
            return await super().call(tool, args, timeout=timeout)
        except (TimeoutError, MCPError) as exc:
            log.warning(
                "%s %s failed (%s); reopening the session and retrying", self.name, tool, exc
            )
            await self.reconnect()
            return await super().call(tool, args, timeout=timeout)


@asynccontextmanager
async def http_session(url: str) -> AsyncIterator[ClientSession]:
    """Connect to a running server over streamable HTTP, e.g. http://localhost:8001/mcp."""
    async with streamable_http_client(url) as streams:
        read, write = streams[0], streams[1]
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session
