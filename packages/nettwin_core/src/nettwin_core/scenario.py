"""NetBench fault scenario schema.

A scenario says what to break (`inject`), what the agent is told (`symptom`), what the
truth is (`ground_truth`), and what a correct minimal fix looks like (`expected_fix`).
Injection and fix use the same typed op vocabulary as `apply_config`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator

from nettwin_core.models import COMPONENT_PATTERN, Layer
from nettwin_core.ops import NODE_RE, Op

SCENARIO_ID_PATTERN = r"^\d{3}-[a-z0-9-]+$"


class GroundTruth(BaseModel):
    node: str
    layer: Layer
    component: str = Field(pattern=COMPONENT_PATTERN)
    summary: str


class Probe(BaseModel):
    """The quickest way to see the symptom: one allowlisted show command and what to expect."""

    node: str
    cmd: str
    expect: Literal["ok", "fail"] = "ok"
    contains: str | None = None
    absent: str | None = None


class Scenario(BaseModel):
    id: str = Field(pattern=SCENARIO_ID_PATTERN)
    title: str
    tier: Literal["A", "B", "C", "stretch"]
    layer: Layer
    symptom: str
    inject: dict[str, list[Op]]
    ground_truth: GroundTruth
    expected_fix: dict[str, list[Op]]
    expected_failed_rules: list[str] = Field(default_factory=list)
    probe: Probe | None = None
    policy: str = "lab/policy/intent.yaml"
    notes: str = ""

    @field_validator("inject", "expected_fix")
    @classmethod
    def _validate_nodes(cls, value: dict[str, list[Op]]) -> dict[str, list[Op]]:
        for node in value:
            if not NODE_RE.fullmatch(node):
                raise ValueError(f"invalid node name: {node!r}")
        return value


def load_scenario(path: Path) -> Scenario:
    with path.open("rb") as fh:
        data = yaml.safe_load(fh)
    return Scenario.model_validate(data)


def load_scenarios(directory: Path) -> list[Scenario]:
    """Load every `*.yaml` scenario in a directory, skipping `*.replay.yaml` files."""
    paths = sorted(p for p in directory.glob("*.yaml") if not p.name.endswith(".replay.yaml"))
    return [load_scenario(p) for p in paths]
