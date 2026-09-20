"""The netverify application: judgement over the twin, with no way to change it."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from nettwin_core.executor import DockerExecutor, Executor
from nettwin_core.models import RuleResult, Snapshot, VerificationReport
from nettwin_core.policy import Policy, load_policy, policy_sha256
from nettwin_core.settings import Settings
from nettwin_core.snapshot import SnapshotStore, capture
from nettwin_core.topology import Topology, load_topology
from netverify.attest import issue, load_or_create_key
from netverify.converge import ConvergeResult, wait_converged
from netverify.executor import ReadOnlyExecutor
from netverify.probes import ProbeResult, reachability_matrix
from netverify.rules import evaluate


class NodeRouteDiff(BaseModel):
    added: list[str] = Field(default_factory=list)
    removed: list[str] = Field(default_factory=list)


class RouteDiff(BaseModel):
    before: str
    after: str
    nodes: dict[str, NodeRouteDiff]
    changed_nodes: int


def route_key(route: dict[str, Any]) -> str:
    gateway = route.get("gateway")
    if gateway is None and route.get("nexthops"):
        gateway = ",".join(str(nh.get("gateway", nh.get("dev", "?"))) for nh in route["nexthops"])
    parts = [str(route.get("dst", "?"))]
    if gateway:
        parts.append(f"via {gateway}")
    if route.get("dev"):
        parts.append(f"dev {route['dev']}")
    if route.get("protocol"):
        parts.append(f"proto {route['protocol']}")
    if route.get("metric") is not None:
        parts.append(f"metric {route['metric']}")
    return " ".join(parts)


def diff_snapshots(before: Snapshot, after: Snapshot) -> RouteDiff:
    nodes: dict[str, NodeRouteDiff] = {}
    for name in sorted(set(before.nodes) | set(after.nodes)):
        b = {route_key(r) for r in (before.nodes.get(name).routes if name in before.nodes else [])}
        a = {route_key(r) for r in (after.nodes.get(name).routes if name in after.nodes else [])}
        if a != b:
            nodes[name] = NodeRouteDiff(added=sorted(a - b), removed=sorted(b - a))
    return RouteDiff(before=before.id, after=after.id, nodes=nodes, changed_nodes=len(nodes))


@dataclass
class NetVerify:
    settings: Settings
    topology: Topology
    executor: ReadOnlyExecutor
    store: SnapshotStore
    key: bytes

    @classmethod
    def from_settings(cls, settings: Settings, executor: Executor | None = None) -> NetVerify:
        settings.ensure_dirs()
        return cls(
            settings=settings,
            topology=load_topology(settings.topology_path),
            executor=ReadOnlyExecutor(executor or DockerExecutor(settings.lab_name)),
            store=SnapshotStore(settings.snapshots_dir),
            key=load_or_create_key(settings.attest_key_path),
        )

    # --- policy ----------------------------------------------------------------------------

    def resolve_policy(self, path: str | None) -> Path:
        base = self.settings.policy_path.resolve().parent
        candidate = (Path(path) if path else self.settings.policy_path).resolve()
        if candidate.parent != base:
            raise ValueError(f"policy files must live in {base}")
        if not candidate.is_file():
            raise FileNotFoundError(f"policy {candidate} does not exist")
        return candidate

    def load_policy(self, path: str | None = None) -> tuple[Policy, str]:
        resolved = self.resolve_policy(path)
        return load_policy(resolved), policy_sha256(resolved)

    # --- checks ----------------------------------------------------------------------------

    async def wait_converged(
        self, timeout: float = 60.0, policy_path: str | None = None, interval: float = 2.0
    ) -> ConvergeResult:
        policy, _ = self.load_policy(policy_path)
        return await wait_converged(
            self.executor, self.topology, policy, timeout=timeout, interval=interval
        )

    async def reachability(self, policy_path: str | None = None) -> list[ProbeResult]:
        policy, _ = self.load_policy(policy_path)
        return await reachability_matrix(self.executor, policy)

    async def intent_check(
        self,
        policy_path: str | None = None,
        *,
        converge_timeout: float = 45.0,
        interval: float = 2.0,
    ) -> VerificationReport:
        policy, sha = self.load_policy(policy_path)
        convergence = await wait_converged(
            self.executor, self.topology, policy, timeout=converge_timeout, interval=interval
        )
        snapshot = await capture(self.executor, self.topology, self.settings.lab_name)
        self.store.save(snapshot)
        probes = await reachability_matrix(self.executor, policy)
        rules = await evaluate(self.executor, self.topology, policy, probes)
        if not convergence.converged:
            rules.insert(
                0,
                RuleResult(
                    rule_id="converged",
                    kind="converge",
                    passed=False,
                    detail=(
                        f"not converged after {convergence.seconds}s: "
                        f"ospf missing {convergence.ospf_missing}, "
                        f"bgp down {convergence.bgp_down}, "
                        f"rib stable {convergence.rib_stable}"
                    ),
                ),
            )
        passed = all(r.passed for r in rules)
        return VerificationReport(
            passed=passed,
            snapshot_id=snapshot.id,
            policy_sha256=sha,
            rules=rules,
            attestation=issue(self.key, snapshot.id, sha) if passed else None,
        )

    def route_diff(self, before_id: str, after_id: str) -> RouteDiff:
        return diff_snapshots(self.store.load(before_id), self.store.load(after_id))
