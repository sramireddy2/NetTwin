from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from nettwin_core.policy import (
    NoSpofRule,
    Policy,
    ReachRule,
    load_policy,
    policy_sha256,
)


def test_load_policy_fixture(fixtures: Path) -> None:
    policy = load_policy(fixtures / "intent.yaml")
    assert set(policy.targets) == {"h1", "srv"}
    kinds = [r.kind for r in policy.rules]
    assert kinds == [
        "reach",
        "reach",
        "path_mtu",
        "ospf_full",
        "bgp_established",
        "no_route_leak",
        "no_spof",
    ]
    assert isinstance(policy.rule("core-spof"), NoSpofRule)
    assert isinstance(policy.rule("reach-h1-srv"), ReachRule)
    with pytest.raises(KeyError):
        policy.rule("nope")


def test_policy_rejects_unknown_target_and_duplicate_ids() -> None:
    targets = {"h1": {"node": "h1", "ip": "10.0.0.1"}}
    with pytest.raises(ValidationError):
        Policy(targets=targets, rules=[{"kind": "reach", "id": "a", "src": "h1", "dst": "srv"}])
    with pytest.raises(ValidationError):
        Policy(
            targets=targets,
            rules=[
                {"kind": "no_spof", "id": "a", "nodes": ["r1", "r2"]},
                {"kind": "no_spof", "id": "a", "nodes": ["r1", "r3"]},
            ],
        )
    with pytest.raises(ValidationError):
        Policy(
            targets=targets,
            rules=[{"kind": "reach", "id": "a", "src": "h1", "dst": "h1", "proto": "tcp"}],
        )


def test_policy_sha256_is_stable(fixtures: Path) -> None:
    path = fixtures / "intent.yaml"
    assert policy_sha256(path) == policy_sha256(path)
    assert len(policy_sha256(path)) == 64
