"""ManualRunner turns an export bundle into scorable output and waits for the decision."""

from __future__ import annotations

from typing import Any

from netbench.admin import CallableAdmin
from netbench.runner import ManualRunner, RunContext
from nettwin_core.models import ChangeResult, RootCause, RuleResult, VerificationReport
from nettwin_core.ops import SetMtu


def _bundle(status: str) -> dict[str, Any]:
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
    return {
        "export_id": "abc123",
        "status": status,
        "decided_by": None if status == "pending" else "operator",
        "changes": [change.model_dump(mode="json")],
        "verification": report.model_dump(mode="json"),
        "root_cause": RootCause(
            node="r2", layer="L2", component="link.mtu", summary="s"
        ).model_dump(),
        "summary": "s",
    }


async def _inject(scenario_id: str) -> dict[str, Any]:
    return {}


def _ctx() -> RunContext:
    return RunContext(scenario_id="004", symptom="srv unreachable", twin=None, verify=None)  # type: ignore[arg-type]


async def test_manual_runner_waits_for_a_new_export_and_its_decision() -> None:
    listings = [
        [{"export_id": "old"}],
        [{"export_id": "old"}],
        [{"export_id": "old"}, {"export_id": "abc123"}],
    ]
    details = [_bundle("pending"), _bundle("approved")]

    async def exports() -> list[dict[str, Any]]:
        return listings.pop(0) if len(listings) > 1 else listings[0]

    async def export(export_id: str) -> dict[str, Any]:
        assert export_id == "abc123"
        return details.pop(0) if len(details) > 1 else details[0]

    admin = CallableAdmin(_inject, dict, exports=exports, export=export)
    messages: list[str] = []
    runner = ManualRunner(admin, timeout=5, poll=0.01, decision_wait=5, notify=messages.append)
    out = await runner.run(_ctx())
    assert out.root_cause is not None and out.root_cause.component == "link.mtu"
    assert out.change_ids == ["c1"]
    assert out.verification is not None and out.verification.passed
    assert out.export == {"export_id": "abc123", "status": "approved", "decided_by": "operator"}
    assert "srv unreachable" in messages[0] and "approved" in messages[-1]


async def test_manual_runner_gives_up_after_the_timeout() -> None:
    async def exports() -> list[dict[str, Any]]:
        return []

    admin = CallableAdmin(_inject, dict, exports=exports)
    runner = ManualRunner(admin, timeout=0.05, poll=0.01, notify=lambda _: None)
    out = await runner.run(_ctx())
    assert out.root_cause is None and out.change_ids == []
    assert "no export" in out.notes


async def test_callable_admin_defaults_have_no_exports() -> None:
    admin = CallableAdmin(_inject, dict)
    assert await admin.exports() == []
    try:
        await admin.export("nope")
    except KeyError as exc:
        assert "nope" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected KeyError")
