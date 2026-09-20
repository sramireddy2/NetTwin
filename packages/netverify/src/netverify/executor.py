"""A structurally read-only executor.

netverify never mutates the twin. Rather than trusting every call site, the executor it is
given wraps the real one and refuses any argv that is not on the shared read-only allowlist,
and refuses stdin outright. A bug in a rule cannot turn into a write.
"""

from __future__ import annotations

from collections.abc import Sequence

from nettwin_core.allowlist import CommandNotAllowed, check
from nettwin_core.executor import DEFAULT_TIMEOUT, ExecResult, Executor


class ReadOnlyViolation(PermissionError):
    pass


class ReadOnlyExecutor:
    def __init__(self, inner: Executor) -> None:
        self.inner = inner

    @staticmethod
    def command_text(argv: Sequence[str]) -> str:
        items = list(argv)
        if len(items) == 3 and items[:2] == ["vtysh", "-c"]:
            return items[2]
        return " ".join(items)

    async def exec(
        self,
        node: str,
        argv: Sequence[str],
        *,
        timeout: float = DEFAULT_TIMEOUT,
        stdin: bytes | None = None,
    ) -> ExecResult:
        if stdin is not None:
            raise ReadOnlyViolation("netverify never sends input to a node")
        try:
            allowed = check(self.command_text(argv))
        except CommandNotAllowed as exc:
            raise ReadOnlyViolation(f"netverify refused {' '.join(argv)}: {exc}") from exc
        if list(allowed.argv) != list(argv):
            raise ReadOnlyViolation(f"netverify refused {' '.join(argv)}: not in canonical form")
        return await self.inner.exec(node, argv, timeout=timeout)
