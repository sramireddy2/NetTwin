"""The real lab files must parse with the core schemas, so CI catches a typo before WSL does."""

from __future__ import annotations

from pathlib import Path

import pytest

from nettwin_core.policy import Policy, load_policy
from nettwin_core.topology import Topology, load_topology

LAB = Path(__file__).resolve().parents[2] / "lab"


@pytest.fixture(scope="module")
def topology() -> Topology:
    return load_topology(LAB / "topology.clab.yml")


@pytest.fixture(scope="module")
def policy() -> Policy:
    return load_policy(LAB / "policy" / "intent.yaml")


def test_topology_nodes_and_links(topology: Topology) -> None:
    assert topology.name == "nettwin"
    routers = {n for n, node in topology.nodes.items() if node.role == "router"}
    hosts = {n for n, node in topology.nodes.items() if node.role == "host"}
    assert routers == {"r1", "r2", "r3", "r4", "isp"}
    assert hosts == {"h10", "h20", "srv", "inet"}
    assert topology.nodes["sw1"].role == "switch"
    assert len(topology.links) == 11
    assert topology.neighbors("r3") == {"r1", "r2", "sw1"}


def test_core_has_no_single_point_of_failure(topology: Topology) -> None:
    assert topology.single_link_partitions(["r1", "r2", "r3", "r4"]) == []


def test_policy_targets_exist_in_topology(topology: Topology, policy: Policy) -> None:
    for target in policy.targets.values():
        assert target.node in topology.nodes
    for rule in policy.rules:
        node = getattr(rule, "node", None)
        if node is not None:
            assert node in topology.nodes, rule.id
        for n in getattr(rule, "nodes", []):
            assert n in topology.nodes, rule.id


def test_every_node_has_a_setup_script(topology: Topology) -> None:
    for name in topology.nodes:
        assert (LAB / "configs" / name / "setup.sh").is_file(), name
    for name, node in topology.nodes.items():
        if node.role == "router":
            assert (LAB / "configs" / name / "frr.conf").is_file(), name
