from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from nettwin_core.executor import FakeExecutor
from nettwin_core.models import ChangeResult, RootCause, RuleResult, VerificationReport
from nettwin_core.ops import SetMtu
from nettwin_core.settings import Settings
from twinlab.admin import load_or_create_token
from twinlab.app import TwinLab
from twinlab.export import new_bundle
from twinlab.server import build_server

LAB = Path(__file__).resolve().parents[2] / "lab"


def _scripted(node: str, argv: tuple[str, ...]) -> str | None:
    if argv[:2] == ("vtysh", "-c") and argv[2] == "show running-config":
        return f"hostname {node}\nend\n"
    if argv[0] == "ip":
        return "[]"
    if argv[0] in ("nft", "bridge", "vtysh"):
        return ""
    return None


@pytest.fixture
def client(tmp_path: Path) -> httpx.AsyncClient:
    settings = Settings.from_env(
        {
            "NETTWIN_STATE_DIR": str(tmp_path),
            "NETTWIN_TOPOLOGY": str(LAB / "topology.clab.yml"),
            "NETTWIN_SCENARIOS": str(LAB / "scenarios"),
        }
    )
    app = TwinLab.from_settings(settings, executor=FakeExecutor().on(_scripted))
    server = build_server(app, admin_token="secret")
    asgi = server.streamable_http_app()
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=asgi), base_url="http://localhost")


AUTH = {"Authorization": "Bearer secret"}


def test_token_file_is_created_once(tmp_path: Path) -> None:
    path = tmp_path / "admin.token"
    first = load_or_create_token(path)
    assert len(first) > 30 and path.exists()
    assert load_or_create_token(path) == first


async def test_admin_routes_require_bearer_token(client: httpx.AsyncClient) -> None:
    async with client:
        assert (await client.get("/admin/status")).status_code == 401
        wrong = await client.get("/admin/status", headers={"Authorization": "Bearer nope"})
        assert wrong.status_code == 401
        ok = await client.get("/admin/status", headers=AUTH)
        assert ok.status_code == 200
        assert ok.json()["nodes"] == 10
        assert "001-ospf-area-mismatch" in ok.json()["scenarios"]


async def test_admin_inject_and_scenarios(client: httpx.AsyncClient) -> None:
    async with client:
        listed = await client.get("/admin/scenarios", headers=AUTH)
        assert listed.status_code == 200
        assert len(listed.json()) == 14
        injected = await client.post("/admin/inject/001", headers=AUTH)
        assert injected.status_code == 200, injected.text
        body = injected.json()
        assert body["scenario"] == "001-ospf-area-mismatch"
        assert "NOC ticket" in body["symptom"]
        missing = await client.post("/admin/inject/999", headers=AUTH)
        assert missing.status_code == 404
        golden = await client.post("/admin/golden", headers=AUTH)
        assert golden.status_code == 409
        assert "golden script not found" in golden.json()["error"]


async def test_server_without_token_has_no_admin_routes(tmp_path: Path) -> None:
    settings = Settings.from_env(
        {"NETTWIN_STATE_DIR": str(tmp_path), "NETTWIN_TOPOLOGY": str(LAB / "topology.clab.yml")}
    )
    app = TwinLab.from_settings(settings, executor=FakeExecutor().on(_scripted))
    asgi = build_server(app).streamable_http_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=asgi), base_url="http://localhost"
    ) as client:
        assert (await client.get("/admin/status", headers=AUTH)).status_code == 404


async def test_admin_export_detail_returns_the_whole_bundle(tmp_path: Path) -> None:
    settings = Settings.from_env(
        {"NETTWIN_STATE_DIR": str(tmp_path), "NETTWIN_TOPOLOGY": str(LAB / "topology.clab.yml")}
    )
    app = TwinLab.from_settings(settings, executor=FakeExecutor().on(_scripted))
    change = ChangeResult(
        change_id="c1",
        node="r2",
        before_snapshot_id="a" * 64,
        after_snapshot_id="b" * 64,
        diff="",
        ops=[SetMtu(iface="eth4", mtu=1500)],
    )
    report = VerificationReport(
        passed=True,
        snapshot_id="b" * 64,
        policy_sha256="c" * 64,
        rules=[RuleResult(rule_id="x", kind="reach", passed=True)],
    )
    cause = RootCause(node="r2", layer="L2", component="link.mtu", summary="s")
    bundle = new_bundle([change], report, cause, "summary")
    app.exports.save(bundle)
    asgi = build_server(app, admin_token="secret").streamable_http_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=asgi), base_url="http://localhost"
    ) as client:
        listed = await client.get("/admin/exports", headers=AUTH)
        assert [e["export_id"] for e in listed.json()] == [bundle.export_id]
        detail = await client.get(f"/admin/exports/{bundle.export_id}", headers=AUTH)
        assert detail.status_code == 200, detail.text
        body = detail.json()
        assert body["status"] == "pending"
        assert body["changes"][0]["change_id"] == "c1"
        assert body["root_cause"]["component"] == "link.mtu"
        assert body["verification"]["passed"] is True
        assert (await client.get("/admin/exports/nope", headers=AUTH)).status_code == 404
