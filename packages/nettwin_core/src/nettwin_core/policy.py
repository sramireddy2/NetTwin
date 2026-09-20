"""Declarative intent policy.

`netverify.intent_check` evaluates these rules against the live twin. Rules reference
named targets (hosts) so scenarios and policies stay readable.
"""

from __future__ import annotations

import hashlib
import ipaddress
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator


class Target(BaseModel):
    node: str
    ip: str

    @field_validator("ip")
    @classmethod
    def _validate_ip(cls, value: str) -> str:
        return str(ipaddress.ip_address(value))


class ReachRule(BaseModel):
    """`src` can (or must not) reach `dst` with the given protocol."""

    kind: Literal["reach"]
    id: str
    src: str
    dst: str
    proto: Literal["icmp", "tcp"] = "icmp"
    port: int | None = Field(default=None, ge=1, le=65535)
    expect: Literal["allow", "deny"] = "allow"

    @model_validator(mode="after")
    def _port_for_tcp(self) -> ReachRule:
        if self.proto == "tcp" and self.port is None:
            raise ValueError("tcp reach rules need a port")
        return self


class PathMtuRule(BaseModel):
    """Packets of `min_mtu` bytes with DF set must get through from `src` to `dst`."""

    kind: Literal["path_mtu"]
    id: str
    src: str
    dst: str
    min_mtu: int = Field(default=1500, ge=576, le=9216)


class OspfFullRule(BaseModel):
    """`node` must have an OSPF neighbour in state Full on `iface`."""

    kind: Literal["ospf_full"]
    id: str
    node: str
    iface: str


class BgpEstablishedRule(BaseModel):
    """`node` must have an Established BGP session with `peer`."""

    kind: Literal["bgp_established"]
    id: str
    node: str
    peer: str

    @field_validator("peer")
    @classmethod
    def _validate_peer(cls, value: str) -> str:
        return str(ipaddress.ip_address(value))


class NoRouteLeakRule(BaseModel):
    """None of `prefixes` (or more specifics of them) may appear in `node`'s routing table."""

    kind: Literal["no_route_leak"]
    id: str
    node: str
    prefixes: list[str] = Field(min_length=1)

    @field_validator("prefixes")
    @classmethod
    def _validate_prefixes(cls, value: list[str]) -> list[str]:
        return [str(ipaddress.ip_network(p, strict=True)) for p in value]


class NoSpofRule(BaseModel):
    """No single link failure may partition the given set of nodes."""

    kind: Literal["no_spof"]
    id: str
    nodes: list[str] = Field(min_length=2)


Rule = Annotated[
    ReachRule | PathMtuRule | OspfFullRule | BgpEstablishedRule | NoRouteLeakRule | NoSpofRule,
    Field(discriminator="kind"),
]


class Policy(BaseModel):
    version: int = 1
    targets: dict[str, Target]
    rules: list[Rule]

    @model_validator(mode="after")
    def _check_references(self) -> Policy:
        seen: set[str] = set()
        for rule in self.rules:
            if rule.id in seen:
                raise ValueError(f"duplicate rule id: {rule.id}")
            seen.add(rule.id)
            if isinstance(rule, ReachRule | PathMtuRule):
                for name in (rule.src, rule.dst):
                    if name not in self.targets:
                        raise ValueError(f"rule {rule.id} references unknown target {name!r}")
        return self

    def rule(self, rule_id: str) -> Rule:
        for rule in self.rules:
            if rule.id == rule_id:
                return rule
        raise KeyError(rule_id)


def load_policy(path: Path) -> Policy:
    with path.open("rb") as fh:
        data = yaml.safe_load(fh)
    return Policy.model_validate(data)


def policy_sha256(path: Path) -> str:
    """Hash of the policy file bytes; bound into netverify attestations."""
    return hashlib.sha256(path.read_bytes()).hexdigest()
