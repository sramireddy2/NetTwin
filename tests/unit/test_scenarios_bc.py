"""Tier B/C machinery: nft delete by rule text, multi-fault and control scoring, and the
fake agent on the control scenario."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from netbench.admin import CallableAdmin
from netbench.clients import ToolClient, memory_session
from netbench.harness import Harness, RunConfig
from netbench.runner import FakeAgentRunner, RunOutput
from netbench.scoring import score_run
from nettwin_core.executor import FakeExecutor
from nettwin_core.models import ChangeResult, RootCause, RuleResult, VerificationReport
from nettwin_core.ops import FrrLines, NftRule, SetMtu, render
from nettwin_core.scenario import load_scenarios
from nettwin_core.settings import Settings
from netverify.app import NetVerify
from netverify.server import build_server as build_netverify
from tests.unit.test_netbench import _env, _twin_scripted
from tests.unit.test_netverify import make_fake
from twinlab.app import TwinLab
from twinlab.apply import ApplyError, apply_ops, normalise_nft_rule, resolve_nft_handle
from twinlab.server import build_server as build_twinlab

LAB = Path(__file__).resolve().parents[2] / "lab"
SCENARIOS = {s.id: s for s in load_scenarios(LAB / "scenarios")}
TWO_FAULTS = SCENARIOS["021-two-faults-mtu-and-bgp"]
CONTROL = SCENARIOS["022-no-fault-control"]

LISTED_CHAIN = """table inet fw {
\tchain forward { # handle 1
\t\ttype filter hook forward priority filter; policy accept;
\t\tip daddr 10.0.40.0/24 drop # handle 7
\t\tip saddr 10.0.10.0/24 ip daddr 10.0.40.0/24 accept # handle 2
\t\tip saddr 10.0.20.0/24 ip daddr 10.0.0.0/16 drop # handle 3
\t}
}
"""


def test_normalise_nft_rule_strips_quotes_handles_and_protocol_names() -> None:
    assert normalise_nft_rule('iifname "eth2" ip protocol ospf drop # handle 4') == (
        "iifname eth2 ip protocol 89 drop"
    )
    assert normalise_nft_rule("  iifname eth2   ip protocol 89 drop ") == (
        "iifname eth2 ip protocol 89 drop"
    )


def test_nft_delete_by_rule_validates_but_cannot_render_unresolved() -> None:
    op = NftRule(action="delete", table="fw", chain="forward", rule="ip daddr 10.0.40.0/24 drop")
    assert op.handle is None
    with pytest.raises(ValueError, match="resolve the handle"):
        render(op)
    with pytest.raises(ValidationError):
        NftRule(action="delete", table="fw", chain="forward")


async def test_apply_resolves_the_handle_from_the_rule_text() -> None:
    fake = FakeExecutor()
    fake.script("r3", ["nft", "-a", "list", "chain", "inet", "fw", "forward"], LISTED_CHAIN)
    fake.on(lambda node, argv: "" if tuple(argv[:3]) == ("nft", "delete", "rule") else None)
    op = NftRule(action="delete", table="fw", chain="forward", rule="ip daddr 10.0.40.0/24 drop")
    assert await resolve_nft_handle(fake, "r3", op) == 7
    await apply_ops(fake, "r3", [op])
    assert fake.calls[-1] == (
        "r3",
        ("nft", "delete", "rule", "inet", "fw", "forward", "handle", "7"),
    )
    missing = NftRule(action="delete", table="fw", chain="forward", rule="ip daddr 1.2.3.0/24 drop")
    with pytest.raises(ApplyError, match="no rule matching"):
        await apply_ops(fake, "r3", [missing])


def _report(passed: bool = True) -> VerificationReport:
    return VerificationReport(
        passed=passed,
        snapshot_id="a" * 64,
        policy_sha256="b" * 64,
        rules=[RuleResult(rule_id="x", kind="reach", passed=passed)],
    )


def _change(change_id: str, node: str, ops: list) -> ChangeResult:
    return ChangeResult(
        change_id=change_id,
        node=node,
        before_snapshot_id="x",
        after_snapshot_id="y",
        diff="",
        ops=ops,
    )


def test_two_fault_scenario_scores_either_cause_and_needs_both_fixes() -> None:
    golden = [{"rule_id": "a", "ok": True}]
    fixes = [
        _change("1", "r2", [SetMtu(iface="eth1", mtu=1500)]),
        _change("2", "r4", [FrrLines(lines=["router bgp 65001", " network 10.0.40.0/24"])]),
    ]
    for node, component in (("r2", "link.mtu"), ("r4", "bgp.network")):
        out = RunOutput(
            root_cause=RootCause(node=node, layer="L3", component=component, summary="s"),
            change_ids=["1", "2"],
            verification=_report(True),
            export={"status": "approved"},
        )
        score = score_run(TWO_FAULTS, out, fixes, golden, golden)
        assert score.root_cause and score.verified and score.minimal, (node, component)
    wrong = RunOutput(
        root_cause=RootCause(node="r2", layer="L3", component="bgp.network", summary="s"),
        change_ids=["1"],
        verification=_report(False),
    )
    score = score_run(TWO_FAULTS, wrong, fixes[:1], golden, golden)
    assert score.root_cause_node and not score.root_cause_component and not score.root_cause
    assert not score.verified and score.minimal


def test_control_scenario_scores_no_cause_and_no_change() -> None:
    golden = [{"rule_id": "a", "ok": True}]
    honest = RunOutput(root_cause=None, change_ids=[], verification=_report(True))
    score = score_run(CONTROL, honest, [], golden, golden)
    assert score.root_cause and score.verified and score.collateral_free and score.minimal
    assert not score.exported and score.ops_expected == 0
    invented = RunOutput(
        root_cause=RootCause(node="r2", layer="L2", component="link.mtu", summary="s"),
        change_ids=["1"],
        verification=_report(True),
    )
    score = score_run(
        CONTROL, invented, [_change("1", "r2", [SetMtu(iface="eth1", mtu=1500)])], golden, golden
    )
    assert not score.root_cause and not score.minimal


async def test_fake_agent_is_honest_on_the_control_scenario(tmp_path: Path) -> None:
    settings = Settings.from_env(_env(tmp_path))
    twin_app = TwinLab.from_settings(settings, executor=FakeExecutor().on(_twin_scripted))
    verify_app = NetVerify.from_settings(settings, executor=make_fake())
    async with (
        memory_session(build_twinlab(twin_app)) as twin_s,
        memory_session(build_netverify(verify_app)) as verify_s,
    ):
        harness = Harness(
            twin=ToolClient(twin_s, "twinlab"),
            verify=ToolClient(verify_s, "netverify"),
            admin=CallableAdmin(twin_app.inject, twin_app.status),
            results_dir=tmp_path / "results",
            matrix="unit",
        )
        records = await harness.run_matrix(
            [CONTROL], RunConfig(name="fake", runner="fake"), FakeAgentRunner([CONTROL])
        )
    record = records[0]
    assert record.error is None
    assert record.output.root_cause is None and record.output.change_ids == []
    assert record.output.export is None
    score = record.score
    assert score.root_cause and score.verified and score.collateral_free and score.minimal
    assert not score.exported
    tools = [c.tool for c in record.output.tool_calls]
    assert "apply_config" not in tools and "export_change" not in tools
    assert "intent_check" in tools


def test_root_cause_node_accepts_prose_around_the_node_name() -> None:
    from netbench.scoring import node_token

    assert node_token("r2 (also r4)") == "r2"
    assert node_token("  R4  ") == "r4"
    assert node_token("sw1: access port") == "sw1"
    golden = [{"rule_id": "a", "ok": True}]
    out = RunOutput(
        root_cause=RootCause(node="r2 (also r4)", layer="L2", component="link.mtu", summary="s"),
        change_ids=[],
        verification=_report(True),
    )
    score = score_run(TWO_FAULTS, out, [], golden, golden)
    assert score.root_cause_node and score.root_cause_component and score.root_cause
