"""Score one run: did it find the cause, did the fix verify, did it break anything else.

Root cause is a structured match against the scenario's ground truth (node and component),
not an LLM judgement. Collateral compares the reachability matrix after the run with the
golden matrix taken before any fault was planted.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from netbench.runner import RunOutput
from nettwin_core.models import ChangeResult
from nettwin_core.scenario import Scenario


class Score(BaseModel):
    root_cause_node: bool
    root_cause_component: bool
    root_cause: bool = Field(description="node and component both match the ground truth")
    verified: bool = Field(description="the verifier passed every intent rule after the fix")
    collateral: list[str] = Field(
        default_factory=list, description="golden probes whose result changed after the run"
    )
    collateral_free: bool
    nodes_touched: list[str] = Field(default_factory=list)
    ops_applied: int
    ops_expected: int
    minimal: bool = Field(
        description="touched only expected nodes with no more ops than the expected fix"
    )
    exported: bool


def probe_map(probes: list[dict[str, Any]]) -> dict[str, bool]:
    return {p["rule_id"]: bool(p["ok"]) for p in probes}


def score_run(
    scenario: Scenario,
    output: RunOutput,
    changes: list[ChangeResult],
    golden_probes: list[dict[str, Any]],
    after_probes: list[dict[str, Any]],
) -> Score:
    causes = scenario.causes
    rc = output.root_cause
    if not causes:
        # Control scenario: the right answer is that nothing is wrong.
        node_ok = component_ok = rc is None
    else:
        node_ok = rc is not None and any(rc.node == c.node for c in causes)
        component_ok = rc is not None and any(
            rc.node == c.node and rc.component == c.component for c in causes
        )
    golden, after = probe_map(golden_probes), probe_map(after_probes)
    collateral = sorted(rule for rule, ok in golden.items() if rule in after and after[rule] != ok)
    nodes = sorted({c.node for c in changes})
    ops_applied = sum(len(c.ops) for c in changes)
    ops_expected = sum(len(ops) for ops in scenario.expected_fix.values())
    expected_nodes = set(scenario.expected_fix)
    if expected_nodes:
        minimal = bool(changes) and set(nodes) <= expected_nodes and ops_applied <= ops_expected
    else:
        minimal = not changes
    verified = bool(output.verification and output.verification.passed)
    exported = bool(output.export and output.export.get("status") in ("approved", "pending"))
    return Score(
        root_cause_node=node_ok,
        root_cause_component=component_ok,
        root_cause=node_ok and component_ok,
        verified=verified,
        collateral=collateral,
        collateral_free=not collateral,
        nodes_touched=nodes,
        ops_applied=ops_applied,
        ops_expected=ops_expected,
        minimal=minimal,
        exported=exported,
    )
