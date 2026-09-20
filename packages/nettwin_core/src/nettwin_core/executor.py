"""The only way code reaches a container: argv in, text out.

`DockerExecutor` runs `docker exec <container> <argv...>` without a shell. `FakeExecutor`
plays back scripted output so both MCP servers can be tested in-process without Docker.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from nettwin_core.ops import NODE_RE

DEFAULT_TIMEOUT = 10.0
DEFAULT_OUTPUT_CAP = 16_000
TRUNCATION_MARKER = "\n...[output truncated by nettwin]..."


@dataclass(frozen=True)
class ExecResult:
    node: str
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    duration_ms: int
    truncated: bool = False
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out


@runtime_checkable
class Executor(Protocol):
    async def exec(
        self,
        node: str,
        argv: Sequence[str],
        *,
        timeout: float = DEFAULT_TIMEOUT,
        stdin: bytes | None = None,
    ) -> ExecResult: ...


def cap_output(text: str, cap: int) -> tuple[str, bool]:
    if len(text) <= cap:
        return text, False
    return text[:cap] + TRUNCATION_MARKER, True


def validate_node(node: str) -> str:
    if not NODE_RE.fullmatch(node):
        raise ValueError(f"invalid node name: {node!r}")
    return node


class DockerExecutor:
    """Executes commands inside containerlab containers named `<prefix>-<lab>-<node>`."""

    def __init__(
        self,
        lab: str,
        *,
        prefix: str = "clab",
        docker: str = "docker",
        output_cap: int = DEFAULT_OUTPUT_CAP,
    ) -> None:
        self.lab = lab
        self.prefix = prefix
        self.docker = docker
        self.output_cap = output_cap

    def container(self, node: str) -> str:
        return f"{self.prefix}-{self.lab}-{validate_node(node)}"

    def command(self, node: str, argv: Sequence[str], *, with_stdin: bool = False) -> list[str]:
        if not argv:
            raise ValueError("argv must not be empty")
        flags = ["-i"] if with_stdin else []
        return [self.docker, "exec", *flags, self.container(node), *argv]

    async def exec(
        self,
        node: str,
        argv: Sequence[str],
        *,
        timeout: float = DEFAULT_TIMEOUT,
        stdin: bytes | None = None,
    ) -> ExecResult:
        cmd = self.command(node, argv, with_stdin=stdin is not None)
        started = time.monotonic()
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE if stdin is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(stdin), timeout)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            return ExecResult(
                node=node,
                argv=tuple(argv),
                returncode=124,
                stdout="",
                stderr=f"timed out after {timeout}s",
                duration_ms=int((time.monotonic() - started) * 1000),
                timed_out=True,
            )
        stdout, truncated = cap_output(out.decode("utf-8", "replace"), self.output_cap)
        stderr, _ = cap_output(err.decode("utf-8", "replace"), self.output_cap)
        return ExecResult(
            node=node,
            argv=tuple(argv),
            returncode=proc.returncode if proc.returncode is not None else -1,
            stdout=stdout,
            stderr=stderr,
            duration_ms=int((time.monotonic() - started) * 1000),
            truncated=truncated,
        )


Handler = Callable[[str, Sequence[str]], ExecResult | str | None]


class FakeExecutor:
    """Scripted executor for tests.

    Lookup order: exact (node, argv) match, then longest scripted prefix, then handlers in
    registration order. Unknown commands return exit 127 so tests fail loudly.
    """

    def __init__(self, *, output_cap: int = DEFAULT_OUTPUT_CAP) -> None:
        self.calls: list[tuple[str, tuple[str, ...]]] = []
        self.stdin_log: list[tuple[str, tuple[str, ...], bytes]] = []
        self.output_cap = output_cap
        self._exact: dict[tuple[str, tuple[str, ...]], tuple[str, str, int]] = {}
        self._prefix: list[tuple[str, tuple[str, ...], tuple[str, str, int]]] = []
        self._handlers: list[Handler] = []

    def script(
        self, node: str, argv: Sequence[str], stdout: str = "", *, stderr: str = "", rc: int = 0
    ) -> FakeExecutor:
        self._exact[(node, tuple(argv))] = (stdout, stderr, rc)
        return self

    def script_prefix(
        self,
        node: str,
        argv_prefix: Sequence[str],
        stdout: str = "",
        *,
        stderr: str = "",
        rc: int = 0,
    ) -> FakeExecutor:
        self._prefix.append((node, tuple(argv_prefix), (stdout, stderr, rc)))
        self._prefix.sort(key=lambda item: len(item[1]), reverse=True)
        return self

    def on(self, handler: Handler) -> FakeExecutor:
        self._handlers.append(handler)
        return self

    def calls_for(self, node: str) -> list[tuple[str, ...]]:
        return [argv for n, argv in self.calls if n == node]

    def _lookup(self, node: str, argv: tuple[str, ...]) -> tuple[str, str, int]:
        if (node, argv) in self._exact:
            return self._exact[(node, argv)]
        for p_node, prefix, result in self._prefix:
            if p_node == node and argv[: len(prefix)] == prefix:
                return result
        for handler in self._handlers:
            handled = handler(node, argv)
            if handled is None:
                continue
            if isinstance(handled, ExecResult):
                return handled.stdout, handled.stderr, handled.returncode
            return handled, "", 0
        return "", f"FakeExecutor: no script for {node} {' '.join(argv)}", 127

    async def exec(
        self,
        node: str,
        argv: Sequence[str],
        *,
        timeout: float = DEFAULT_TIMEOUT,
        stdin: bytes | None = None,
    ) -> ExecResult:
        validate_node(node)
        key = tuple(argv)
        self.calls.append((node, key))
        if stdin is not None:
            self.stdin_log.append((node, key, stdin))
        stdout, stderr, rc = self._lookup(node, key)
        stdout, truncated = cap_output(stdout, self.output_cap)
        return ExecResult(
            node=node,
            argv=key,
            returncode=rc,
            stdout=stdout,
            stderr=stderr,
            duration_ms=0,
            truncated=truncated,
        )
