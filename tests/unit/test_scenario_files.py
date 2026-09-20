"""Every shipped scenario must be internally consistent with the topology and the policy."""

from __future__ import annotations

from pathlib import Path

import pytest

from nettwin_core.ops import render
from nettwin_core.policy import load_policy
from nettwin_core.scenario import Scenario, load_scenarios
from nettwin_core.topology import load_topology
from twinlab.allowlist import check

LAB = Path(__file__).resolve().parents[2] / "lab"
SCENARIOS = load_scenarios(LAB / "scenarios")
TOPOLOGY = load_topology(LAB / "topology.clab.yml")
POLICY = load_policy(LAB / "policy" / "intent.yaml")
RULE_IDS = {r.id for r in POLICY.rules}


def test_scenario_ids_are_unique_and_sequential() -> None:
    ids = [s.id for s in SCENARIOS]
    assert len(ids) == len(set(ids))
    numbers = [int(i.split("-", 1)[0]) for i in ids]
    assert numbers == list(range(1, len(ids) + 1))


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.id)
def test_scenario_is_consistent(scenario: Scenario) -> None:
    for node in (*scenario.inject, *scenario.expected_fix):
        assert node in TOPOLOGY.nodes, node
    assert "NOC ticket" in scenario.symptom
    assert set(scenario.expected_failed_rules) <= RULE_IDS, scenario.expected_failed_rules
    if scenario.tier == "control":
        assert not scenario.inject and not scenario.expected_fix and not scenario.causes
        assert scenario.probe is None and not scenario.expected_failed_rules
        return
    assert scenario.causes and scenario.inject and scenario.expected_fix
    assert scenario.expected_failed_rules
    for cause in scenario.causes:
        assert cause.node in TOPOLOGY.nodes
        assert cause.node in scenario.expected_fix, "every planted fault gets fixed"
        assert cause.component not in scenario.symptom
    assert len(scenario.extra_causes) == (1 if scenario.tier == "stretch" else 0)
    for ops in (*scenario.inject.values(), *scenario.expected_fix.values()):
        for op in ops:
            if (
                getattr(op, "kind", "") == "nft_rule"
                and op.action == "delete"
                and op.handle is None
            ):
                continue  # handle is resolved on the node at apply time
            assert render(op)
    assert scenario.probe is not None
    assert scenario.probe.node in TOPOLOGY.nodes
    check(scenario.probe.cmd)
    assert (scenario.probe.contains is None) or (scenario.probe.absent is None)


def test_tiers_are_all_represented() -> None:
    tiers = {s.tier for s in SCENARIOS}
    assert tiers == {"A", "B", "C", "stretch", "control"}
    assert len(SCENARIOS) == 22
