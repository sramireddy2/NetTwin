"""Score one run: did it find the cause, did the fix verify, did it break anything else.

Root cause is a structured match against the scenario's ground truth (node and component),
not an LLM judgement. Collateral compares the reachability matrix after the run with the
golden matrix taken before any fault was planted.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field

from netbench.runner import RunOutput
from nettwin_core.models import ChangeResult, VerificationReport
from nettwin_core.scenario import Scenario


class Score(BaseModel):
    root_cause_node: bool
    root_cause_component: bool
    root_cause: bool = Field(description="node and component both match the ground truth")
    fix_correct: bool | None = Field(
        default=None,
        description="the harness's own intent_check passed after the run, whatever the agent "
        "said; None when it was not measured (records from before this column existed)",
    )
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
    restored_golden: bool | None = Field(
        default=None,
        description="the twin's content-addressed snapshot after the run equals the golden "
        "baseline, i.e. the design was restored rather than worked around; None if unmeasured",
    )
    golden_snapshot_id: str | None = None
    after_snapshot_id: str | None = None
    exported: bool


def probe_map(probes: list[dict[str, Any]]) -> dict[str, bool]:
    return {p["rule_id"]: bool(p["ok"]) for p in probes}


_NODE_TOKEN = re.compile(r"[a-z][a-z0-9-]*")


def node_token(text: str) -> str:
    """The node an agent named, even when it wrote prose such as `r2 (also r4)`."""
    match = _NODE_TOKEN.search(text.strip().lower())
    return match.group(0) if match else text.strip()


def score_run(
    scenario: Scenario,
    output: RunOutput,
    changes: list[ChangeResult],
    golden_probes: list[dict[str, Any]],
    after_probes: list[dict[str, Any]],
    after_verification: VerificationReport | None = None,
    golden_snapshot_id: str | None = None,
) -> Score:
    causes = scenario.causes
    rc = output.root_cause
    if not causes:
        # Control scenario: the right answer is that nothing is wrong.
        node_ok = component_ok = rc is None
    else:
        node = node_token(rc.node) if rc is not None else None
        node_ok = node is not None and any(node == c.node for c in causes)
        component_ok = rc is not None and any(
            node == c.node and rc.component == c.component for c in causes
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
    fix_correct = None if after_verification is None else bool(after_verification.passed)
    after_id = after_verification.snapshot_id if after_verification is not None else None
    restored_golden = after_id == golden_snapshot_id if after_id and golden_snapshot_id else None
    exported = bool(output.export and output.export.get("status") in ("approved", "pending"))
    return Score(
        root_cause_node=node_ok,
        root_cause_component=component_ok,
        root_cause=node_ok and component_ok,
        fix_correct=fix_correct,
        verified=verified,
        collateral=collateral,
        collateral_free=not collateral,
        nodes_touched=nodes,
        ops_applied=ops_applied,
        ops_expected=ops_expected,
        minimal=minimal,
        restored_golden=restored_golden,
        golden_snapshot_id=golden_snapshot_id,
        after_snapshot_id=after_id,
        exported=exported,
    )
