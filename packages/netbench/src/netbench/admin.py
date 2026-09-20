"""The harness's side door: fault injection and status through twinlab's admin route.

Agents never see this. It is a bearer-token HTTP API, or a pair of callables when the
harness runs in-process against a `TwinLab` object in tests.
"""

from __future__ import annotations

import platform
import subprocess
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Protocol

import httpx


class AdminClient(Protocol):
    async def inject(self, scenario_id: str) -> dict[str, Any]: ...

    async def status(self) -> dict[str, Any]: ...

    async def exports(self) -> list[dict[str, Any]]: ...

    async def export(self, export_id: str) -> dict[str, Any]: ...


class HttpAdmin:
    def __init__(self, base_url: str, token: str, *, timeout: float = 120) -> None:
        self.base_url = base_url.rstrip("/")
        self.headers = {"Authorization": f"Bearer {token}"}
        self.timeout = timeout

    async def _request(self, method: str, path: str) -> Any:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.request(method, f"{self.base_url}{path}", headers=self.headers)
        if response.status_code >= 400:
            raise RuntimeError(f"admin {method} {path} -> {response.status_code}: {response.text}")
        return response.json()

    async def inject(self, scenario_id: str) -> dict[str, Any]:
        return await self._request("POST", f"/admin/inject/{scenario_id}")

    async def status(self) -> dict[str, Any]:
        return await self._request("GET", "/admin/status")

    async def exports(self) -> list[dict[str, Any]]:
        return await self._request("GET", "/admin/exports")

    async def export(self, export_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/admin/exports/{export_id}")


async def _no_exports() -> list[dict[str, Any]]:
    return []


async def _no_export(export_id: str) -> dict[str, Any]:
    raise KeyError(f"unknown export {export_id!r}")


class CallableAdmin:
    """Adapter for in-process use: wire `TwinLab.inject` and `TwinLab.status` straight in."""

    def __init__(
        self,
        inject: Callable[[str], Awaitable[dict[str, Any]]],
        status: Callable[[], dict[str, Any]],
        exports: Callable[[], Awaitable[list[dict[str, Any]]]] | None = None,
        export: Callable[[str], Awaitable[dict[str, Any]]] | None = None,
    ) -> None:
        self._inject = inject
        self._status = status
        self._exports = exports or _no_exports
        self._export = export or _no_export

    async def inject(self, scenario_id: str) -> dict[str, Any]:
        return await self._inject(scenario_id)

    async def status(self) -> dict[str, Any]:
        return self._status()

    async def exports(self) -> list[dict[str, Any]]:
        return await self._exports()

    async def export(self, export_id: str) -> dict[str, Any]:
        return await self._export(export_id)


def read_admin_token(path: Path | None, *, wsl_distro: str = "Containerlab") -> str:
    """Read the admin token from a file, or from the WSL distro when running on Windows."""
    if path is not None and path.exists():
        return path.read_text(encoding="utf-8").strip()
    if platform.system() == "Windows":
        proc = subprocess.run(
            ["wsl.exe", "-d", wsl_distro, "--", "cat", "$HOME/.nettwin/admin.token"],
            capture_output=True,
            check=False,
        )
        token = proc.stdout.decode("utf-8", "ignore").replace("\x00", "").strip()
        if proc.returncode == 0 and token:
            return token
    raise FileNotFoundError(
        "admin token not found; pass --token-file or start the servers so that "
        "~/.nettwin/admin.token exists"
    )
