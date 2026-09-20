"""Evaluate every intent rule against the live twin."""

from __future__ import annotations

import json

from nettwin_core.executor import Executor
from nettwin_core.models import RuleResult
from nettwin_core.policy import (
    BgpEstablishedRule,
    NoRouteLeakRule,
    NoSpofRule,
    OspfFullRule,
    PathMtuRule,
    Policy,
    ReachRule,
)
from nettwin_core.topology import Topology
from netverify.converge import bgp_established, ospf_full
from netverify.probes import ProbeResult


async def leaked_prefixes(executor: Executor, node: str, prefix: str) -> list[str]:
    res = await executor.exec(node, ["vtysh", "-c", f"show ip route {prefix} longer-prefixes json"])
    text = res.stdout.strip()
    if not res.ok or not text.startswith("{"):
        return []
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    return sorted(data) if isinstance(data, dict) else []


async def evaluate(
    executor: Executor, topology: Topology, policy: Policy, probes: list[ProbeResult]
) -> list[RuleResult]:
    by_id = {p.rule_id: p for p in probes}
    results: list[RuleResult] = []
    for rule in policy.rules:
        if isinstance(rule, ReachRule):
            probe = by_id[rule.id]
            passed = probe.ok == (rule.expect == "allow")
            state = "reachable" if probe.ok else "unreachable"
            detail = f"{rule.src} -> {rule.dst} ({probe.kind}) {state}, expected {rule.expect}"
        elif isinstance(rule, PathMtuRule):
            probe = by_id[rule.id]
            passed = probe.ok
            state = "passed" if probe.ok else "dropped"
            detail = f"{rule.src} -> {rule.dst} {rule.min_mtu}-byte DF probe {state}"
        elif isinstance(rule, OspfFullRule):
            passed = await ospf_full(executor, rule.node, rule.iface)
            detail = f"{rule.node} {rule.iface} OSPF neighbour {'Full' if passed else 'not Full'}"
        elif isinstance(rule, BgpEstablishedRule):
            passed = await bgp_established(executor, rule.node, rule.peer)
            state = "Established" if passed else "not Established"
            detail = f"{rule.node} BGP session to {rule.peer} {state}"
        elif isinstance(rule, NoRouteLeakRule):
            leaked: list[str] = []
            for prefix in rule.prefixes:
                leaked.extend(await leaked_prefixes(executor, rule.node, prefix))
            passed = not leaked
            detail = (
                f"{rule.node} routes {', '.join(leaked)}"
                if leaked
                else f"{rule.node} has no route inside {', '.join(rule.prefixes)}"
            )
        elif isinstance(rule, NoSpofRule):
            partitions = topology.single_link_partitions(rule.nodes)
            passed = not partitions
            detail = (
                "single link failure partitions the set: "
                + "; ".join(link.label() for link in partitions)
                if partitions
                else "no single link failure partitions " + ", ".join(rule.nodes)
            )
        else:  # pragma: no cover - the union is closed
            raise TypeError(f"unknown rule kind {rule.kind!r}")
        results.append(RuleResult(rule_id=rule.id, kind=rule.kind, passed=passed, detail=detail))
    return results
