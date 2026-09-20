"""Admin HTTP routes on the twinlab server: fault injection, golden reset, export approval.

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

    async def exports(request: Request) -> Response:
        bundles = [app.exports.load(i) for i in app.exports.ids()]
        return JSONResponse(
            [
                {
                    "export_id": b.export_id,
                    "status": b.status,
                    "nodes": b.nodes,
                    "root_cause": f"{b.root_cause.component} on {b.root_cause.node}",
                    "created_at": b.created_at.isoformat(),
                }
                for b in bundles
            ]
        )

    async def export_detail(request: Request) -> Response:
        bundle = app.exports.load(request.path_params["export_id"])
        return JSONResponse(bundle.model_dump(mode="json"))

    async def approve(request: Request) -> Response:
        export_id = request.path_params["export_id"]
        bundle = app.decide_export(export_id, approved=True, decided_by="cli")
        return JSONResponse(
            {
                "export_id": bundle.export_id,
                "status": bundle.status,
                "path": str(app.exports.path(bundle.export_id)),
            }
        )

    server.custom_route("/admin/status", methods=["GET"])(guard(status))
    server.custom_route("/admin/scenarios", methods=["GET"])(guard(scenarios))
    server.custom_route("/admin/inject/{scenario_id}", methods=["POST"])(guard(inject))
    server.custom_route("/admin/golden", methods=["POST"])(guard(golden))
    server.custom_route("/admin/exports", methods=["GET"])(guard(exports))
    server.custom_route("/admin/exports/{export_id}", methods=["GET"])(guard(export_detail))
    server.custom_route("/admin/approve/{export_id}", methods=["POST"])(guard(approve))
