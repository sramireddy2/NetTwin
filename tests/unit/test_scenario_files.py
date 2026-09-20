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
    assert scenario.ground_truth.node in TOPOLOGY.nodes
    assert scenario.inject and scenario.expected_fix
    for ops in (*scenario.inject.values(), *scenario.expected_fix.values()):
        for op in ops:
            assert render(op)
    assert set(scenario.expected_failed_rules) <= RULE_IDS, scenario.expected_failed_rules
    assert scenario.probe is not None
    assert scenario.probe.node in TOPOLOGY.nodes
    check(scenario.probe.cmd)
    assert (scenario.probe.contains is None) or (scenario.probe.absent is None)
    assert "NOC ticket" in scenario.symptom
    assert scenario.ground_truth.component not in scenario.symptom
