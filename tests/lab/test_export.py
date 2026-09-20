"""End to end on the live lab in bench mode: inject, fix, verify, export (marker: lab)."""

from __future__ import annotations

import os

import pytest

from nettwin_core.executor import DockerExecutor
from nettwin_core.models import RootCause
from nettwin_core.settings import Settings
from netverify.app import NetVerify
from twinlab.app import TwinLab

pytestmark = pytest.mark.lab


async def test_fix_verify_export_bundle(settings: Settings, executor: DockerExecutor) -> None:
    bench = Settings.from_env({**os.environ, "NETTWIN_BENCH": "1"})
    twin = TwinLab.from_settings(bench, executor=executor)
    verifier = NetVerify.from_settings(settings, executor=executor)
    golden = await twin.snapshot()
    try:
        scenario = twin.scenario("005")
        await twin.inject(scenario.id)
        broken = await verifier.intent_check(converge_timeout=20)
        assert set(broken.failed_rules) - {"converged"} == set(scenario.expected_failed_rules)

        node, ops = next(iter(scenario.expected_fix.items()))
        change = await twin.apply(node, ops, rationale="restore server-facing MTU")
        report = await verifier.intent_check()
        assert report.passed, report.failed_rules
        assert report.snapshot_id == change.after_snapshot_id

        root_cause = RootCause(
            node=scenario.ground_truth.node,
            layer=scenario.ground_truth.layer,
            component=scenario.ground_truth.component,
            summary=scenario.ground_truth.summary,
        )
        bundle = twin.prepare_export([change.change_id], report, root_cause, "fix MTU")
        decided = twin.decide_export(bundle.export_id, approved=True, decided_by="bench-auto")
        folder = twin.exports.path(decided.export_id)
        assert (folder / "diff.patch").read_text().count("mtu") >= 1
        assert "link.mtu on r2" in (folder / "root_cause.md").read_text()
    finally:
        result = await twin.rollback(golden.id)
    assert result["matches_target"] is True
