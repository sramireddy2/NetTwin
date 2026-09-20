"""The export gate: attestation checks, elicitation outcomes, pending approvals, bench mode."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import anyio
import httpx
import pytest
from mcp import types
from mcp.client.session import ClientSession
from mcp.shared.memory import create_client_server_memory_streams

from nettwin_core.attest import issue, load_or_create_key
from nettwin_core.executor import FakeExecutor
from nettwin_core.models import RootCause, RuleResult, VerificationReport
from nettwin_core.ops import SetMtu
from nettwin_core.policy import policy_sha256
from nettwin_core.settings import Settings
from twinlab.app import TwinLab
from twinlab.export import ExportError
from twinlab.server import build_server

LAB = Path(__file__).resolve().parents[2] / "lab"
ROOT_CAUSE = RootCause(node="r2", layer="L2", component="link.mtu", summary="eth1 MTU was 1400")


def _scripted(node: str, argv: tuple[str, ...]) -> str | None:
    if argv[:2] == ("vtysh", "-c") and argv[2] == "show running-config":
        return f"hostname {node}\nend\n"
    if argv[0] == "ip":
        return "[]"
    if argv[0] in ("nft", "bridge", "vtysh"):
        return ""
    return None


def make_app(tmp_path: Path, *, bench: bool = False) -> TwinLab:
    env = {
        "NETTWIN_STATE_DIR": str(tmp_path),
        "NETTWIN_TOPOLOGY": str(LAB / "topology.clab.yml"),
        "NETTWIN_POLICY": str(LAB / "policy" / "intent.yaml"),
    }
    if bench:
        env["NETTWIN_BENCH"] = "1"
    return TwinLab.from_settings(Settings.from_env(env), executor=FakeExecutor().on(_scripted))


def report_for(app: TwinLab, snapshot_id: str, *, passed: bool = True, sha: str | None = None):
    sha = sha or policy_sha256(app.settings.policy_path)
    key = load_or_create_key(app.settings.attest_key_path)
    return VerificationReport(
        passed=passed,
        snapshot_id=snapshot_id,
        policy_sha256=sha,
        rules=[RuleResult(rule_id="corp-to-srv", kind="reach", passed=passed, detail="ok")],
        attestation=issue(key, snapshot_id, sha) if passed else None,
    )


async def applied_change(app: TwinLab):
    return await app.apply("r2", [SetMtu(iface="eth1", mtu=1500)], rationale="restore MTU")


@asynccontextmanager
async def connected(app: TwinLab, elicitation_callback=None) -> AsyncIterator[ClientSession]:
    server = build_server(app)
    low = server._lowlevel_server
    async with (
        create_client_server_memory_streams() as (client_streams, server_streams),
        anyio.create_task_group() as tg,
    ):
        tg.start_soon(
            low.run, server_streams[0], server_streams[1], low.create_initialization_options(), True
        )
        async with ClientSession(*client_streams, elicitation_callback=elicitation_callback) as s:
            await s.initialize()
            yield s
        tg.cancel_scope.cancel()


def _text(result: Any) -> str:
    return "".join(getattr(block, "text", "") for block in result.content)


async def _export(session: ClientSession, change_id: str, report: VerificationReport):
    return await session.call_tool(
        "export_change",
        {
            "change_ids": [change_id],
            "verification": json.loads(report.model_dump_json()),
            "root_cause": ROOT_CAUSE.model_dump(),
            "summary": "Set r2 eth1 back to 1500.",
        },
    )


async def test_prepare_export_rejects_bad_evidence(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    change = await applied_change(app)
    good = report_for(app, change.after_snapshot_id)
    with pytest.raises(ExportError, match="nothing to export"):
        app.prepare_export([], good, ROOT_CAUSE)
    with pytest.raises(ExportError, match="did not pass"):
        app.prepare_export(
            [change.change_id], report_for(app, change.after_snapshot_id, passed=False), ROOT_CAUSE
        )
    tampered = good.model_copy(deep=True)
    tampered.attestation.mac = "0" * 64
    with pytest.raises(ExportError, match="signature"):
        app.prepare_export([change.change_id], tampered, ROOT_CAUSE)
    wrong_state = report_for(app, "f" * 64)
    with pytest.raises(ExportError, match="state after the last change"):
        app.prepare_export([change.change_id], wrong_state, ROOT_CAUSE)
    wrong_policy = report_for(app, change.after_snapshot_id, sha="a" * 64)
    with pytest.raises(ExportError, match="different intent policy"):
        app.prepare_export([change.change_id], wrong_policy, ROOT_CAUSE)
    with pytest.raises(KeyError):
        app.prepare_export(["nope"], good, ROOT_CAUSE)
    assert app.exports.ids() == []


async def test_operator_accepts_via_elicitation(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    change = await applied_change(app)
    report = report_for(app, change.after_snapshot_id)
    seen: dict[str, str] = {}

    async def accept(context, params):
        seen["message"] = params.message
        return types.ElicitResult(action="accept", content={"approve": True, "note": "lgtm"})

    async with connected(app, accept) as session:
        result = await _export(session, change.change_id, report)
    assert not result.is_error, _text(result)
    body = result.structured_content
    assert body["status"] == "approved" and body["decided_by"] == "operator"
    assert "Export verified change" in seen["message"] and "link.mtu on r2" in seen["message"]
    folder = app.exports.path(body["export_id"])
    assert (folder / "diff.patch").exists() and (folder / "root_cause.md").exists()
    assert (folder / "verification.json").exists()
    assert app.exports.load(body["export_id"]).note == "lgtm"


async def test_operator_declines_and_nothing_is_written(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    change = await applied_change(app)
    report = report_for(app, change.after_snapshot_id)

    async def decline(context, params):
        return types.ElicitResult(action="decline")

    async with connected(app, decline) as session:
        result = await _export(session, change.change_id, report)
    body = result.structured_content
    assert body["status"] == "declined"
    folder = app.exports.path(body["export_id"])
    assert (folder / "bundle.json").exists() and not (folder / "diff.patch").exists()


async def test_client_without_elicitation_leaves_export_pending_until_admin_approves(
    tmp_path: Path,
) -> None:
    app = make_app(tmp_path)
    change = await applied_change(app)
    report = report_for(app, change.after_snapshot_id)
    async with connected(app) as session:
        result = await _export(session, change.change_id, report)
    body = result.structured_content
    assert body["status"] == "pending" and "nettwin approve" in body["message"]
    asgi = build_server(app, admin_token="secret").streamable_http_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=asgi), base_url="http://localhost"
    ) as client:
        headers = {"Authorization": "Bearer secret"}
        listed = await client.get("/admin/exports", headers=headers)
        assert listed.json()[0]["status"] == "pending"
        approved = await client.post(f"/admin/approve/{body['export_id']}", headers=headers)
        assert approved.status_code == 200 and approved.json()["status"] == "approved"
        again = await client.post(f"/admin/approve/{body['export_id']}", headers=headers)
        assert again.status_code == 409
    assert (app.exports.path(body["export_id"]) / "diff.patch").exists()


async def test_bench_mode_auto_approves(tmp_path: Path) -> None:
    app = make_app(tmp_path, bench=True)
    change = await applied_change(app)
    report = report_for(app, change.after_snapshot_id)
    async with connected(app) as session:
        result = await _export(session, change.change_id, report)
    body = result.structured_content
    assert body["status"] == "approved" and body["decided_by"] == "bench-auto"


async def test_bad_attestation_is_a_tool_error_over_mcp(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    change = await applied_change(app)
    report = report_for(app, change.after_snapshot_id)
    report.attestation.mac = "0" * 64
    async with connected(app) as session:
        result = await _export(session, change.change_id, report)
        assert result.is_error and "signature" in _text(result)
        prompts = {p.name for p in (await session.list_prompts()).prompts}
        assert prompts == {"diagnose", "propose-change"}
