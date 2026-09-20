"""Contracts shared by the agents, the servers, and the benchmark.

These double as structured-output schemas for the agent roles.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, Field, computed_field

from nettwin_core.ops import Op

Layer = Literal["L2", "L3", "policy", "host"]

COMPONENT_PATTERN = r"^[a-z0-9_]+(\.[a-z0-9_]+)*$"


class Evidence(BaseModel):
    """One observation: which node, which command, and the relevant excerpt."""

    node: str
    command: str
    excerpt: str = Field(max_length=4000)


class Finding(BaseModel):
    """What one investigator concluded about one node at one layer."""

    layer: Layer
    node: str
    summary: str
    evidence: list[Evidence] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    suspects_root_cause: bool = False


class RootCause(BaseModel):
    """The single fault the team believes explains the symptom."""

    node: str
    layer: Layer
    component: str = Field(
        pattern=COMPONENT_PATTERN,
        description="Dotted tag such as ospf.area, ospf.mtu, bgp.network, nft.rule_order, "
        "link.mtu, vlan.access, ip.address, static.route",
    )
    summary: str


class ChangeProposal(BaseModel):
    """What the change agent wants to apply and why."""

    ops: dict[str, list[Op]]
    rationale: str
    expected_effect: str


class ChangeResult(BaseModel):
    """What twinlab actually applied, with the server-computed diff."""

    change_id: str
    before_snapshot_id: str
    after_snapshot_id: str
    diff: str
    ops: dict[str, list[Op]]


class NodeState(BaseModel):
    """Captured state of one node: FRR running-config plus kernel networking state.

    `addrs`, `links`, `routes` and `bridge_vlans` hold normalised `ip -j` / `bridge -j`
    output (volatile fields such as nhid and ifindex removed, management eth0 dropped).
    """

    running_config: str = ""
    addrs: Any = None
    links: Any = None
    routes: Any = None
    bridge_vlans: Any = None
    nft: str = ""


class Snapshot(BaseModel):
    """Content-addressed capture of the whole twin."""

    id: str
    lab: str
    created_at: datetime
    nodes: dict[str, NodeState]

    #: Fields that are derived from the network rather than configured on it. They are stored
    #: for diffing but excluded from the id so a snapshot taken mid-convergence still matches.
    DERIVED_FIELDS: ClassVar[frozenset[str]] = frozenset({"routes"})

    @classmethod
    def compute_id(cls, nodes: dict[str, NodeState]) -> str:
        payload = {
            name: nodes[name].model_dump(mode="json", exclude=set(cls.DERIVED_FIELDS))
            for name in sorted(nodes)
        }
        blob = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(blob).hexdigest()

    def diff(self, other: Snapshot) -> dict[str, list[str]]:
        """Configured fields that differ per node between two snapshots (for error messages)."""
        result: dict[str, list[str]] = {}
        for name in sorted(set(self.nodes) | set(other.nodes)):
            a, b = self.nodes.get(name, NodeState()), other.nodes.get(name, NodeState())
            fields = [
                f
                for f in NodeState.model_fields
                if f not in self.DERIVED_FIELDS and getattr(a, f) != getattr(b, f)
            ]
            if fields:
                result[name] = fields
        return result

    @classmethod
    def build(cls, lab: str, nodes: dict[str, NodeState]) -> Snapshot:
        return cls(id=cls.compute_id(nodes), lab=lab, created_at=datetime.now(UTC), nodes=nodes)

    @property
    def short_id(self) -> str:
        return self.id[:12]


class RuleResult(BaseModel):
    rule_id: str
    kind: str
    passed: bool
    detail: str = ""


class Attestation(BaseModel):
    """netverify's signed statement that a snapshot satisfied a policy."""

    snapshot_id: str
    policy_sha256: str
    issued_at: datetime
    mac: str


class VerificationReport(BaseModel):
    passed: bool
    snapshot_id: str
    policy_sha256: str
    rules: list[RuleResult]
    attestation: Attestation | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def failed_rules(self) -> list[str]:
        return [r.rule_id for r in self.rules if not r.passed]


class IncidentReport(BaseModel):
    """Final output of one diagnosis run."""

    symptom: str
    root_cause: RootCause | None = None
    findings: list[Finding] = Field(default_factory=list)
    change: ChangeResult | None = None
    verification: VerificationReport | None = None
    exported: bool = False
