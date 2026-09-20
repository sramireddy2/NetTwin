"""Admin HTTP routes on the twinlab server: fault injection and golden reset.

These are deliberately not MCP tools. They sit on the same Starlette app under `/admin`,
require a bearer token that only the benchmark harness and the `nettwin` CLI hold, and an
agent connected over MCP has no way to reach them.
"""

from __future__ import annotations

import contextlib
import hmac
import secrets
from collections.abc import Awaitable, Callable
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from twinlab.app import TwinLab
from twinlab.apply import ApplyError
from twinlab.snapshot import SnapshotError

Handler = Callable[[Request], Awaitable[Response]]


def load_or_create_token(path: Path) -> str:
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    token = secrets.token_urlsafe(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(token + "\n", encoding="utf-8")
    with contextlib.suppress(OSError):
        path.chmod(0o600)
    return token


def authorized(request: Request, token: str) -> bool:
    header = request.headers.get("authorization", "")
    if not header.startswith("Bearer "):
        return False
    return hmac.compare_digest(header[7:].strip(), token)


def register_admin_routes(server: MCPServer, app: TwinLab, token: str) -> None:
    def guard(handler: Handler) -> Handler:
        async def wrapped(request: Request) -> Response:
            if not authorized(request, token):
                return JSONResponse({"error": "unauthorized"}, status_code=401)
            try:
                return await handler(request)
            except KeyError as exc:
                return JSONResponse({"error": str(exc)}, status_code=404)
            except (ApplyError, SnapshotError, ValueError, FileNotFoundError, RuntimeError) as exc:
                return JSONResponse({"error": str(exc)}, status_code=409)

        wrapped.__name__ = handler.__name__
        return wrapped

    async def status(request: Request) -> Response:
        return JSONResponse(app.status())

    async def scenarios(request: Request) -> Response:
        return JSONResponse(
            [
                {
                    "id": s.id,
                    "title": s.title,
                    "tier": s.tier,
                    "layer": s.layer,
                    "symptom": s.symptom,
                }
                for s in app.scenarios()
            ]
        )

    async def inject(request: Request) -> Response:
        return JSONResponse(await app.inject(request.path_params["scenario_id"]))

    async def golden(request: Request) -> Response:
        return JSONResponse(await app.golden())

    server.custom_route("/admin/status", methods=["GET"])(guard(status))
    server.custom_route("/admin/scenarios", methods=["GET"])(guard(scenarios))
    server.custom_route("/admin/inject/{scenario_id}", methods=["POST"])(guard(inject))
    server.custom_route("/admin/golden", methods=["POST"])(guard(golden))
