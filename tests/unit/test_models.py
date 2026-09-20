from __future__ import annotations

import pytest
from pydantic import ValidationError

from nettwin_core.models import NodeState, RootCause, RuleResult, Snapshot, VerificationReport


def test_snapshot_id_is_deterministic_and_order_independent() -> None:
    a = {"r1": NodeState(running_config="x"), "r2": NodeState(running_config="y")}
    b = {"r2": NodeState(running_config="y"), "r1": NodeState(running_config="x")}
    assert Snapshot.compute_id(a) == Snapshot.compute_id(b)
    changed = {"r1": NodeState(running_config="x"), "r2": NodeState(running_config="z")}
    assert Snapshot.compute_id(a) != Snapshot.compute_id(changed)
    snap = Snapshot.build("nettwin", a)
    assert snap.id == Snapshot.compute_id(a)
    assert len(snap.id) == 64
    assert snap.short_id == snap.id[:12]


def test_verification_report_failed_rules_is_serialised() -> None:
    report = VerificationReport(
        passed=False,
        snapshot_id="abc",
        policy_sha256="def",
        rules=[
            RuleResult(rule_id="a", kind="reach", passed=True),
            RuleResult(rule_id="b", kind="reach", passed=False, detail="timeout"),
        ],
    )
    assert report.failed_rules == ["b"]
    assert report.model_dump()["failed_rules"] == ["b"]


def test_root_cause_component_pattern() -> None:
    RootCause(node="r1", layer="L3", component="ospf.area", summary="s")
    with pytest.raises(ValidationError):
        RootCause(node="r1", layer="L3", component="OSPF Area", summary="s")
