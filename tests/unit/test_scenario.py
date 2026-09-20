from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from nettwin_core.ops import FrrLines
from nettwin_core.scenario import Scenario, load_scenario, load_scenarios


def test_load_scenario_fixture(fixtures: Path) -> None:
    scenario = load_scenario(fixtures / "001-ospf-area-mismatch.yaml")
    assert scenario.id == "001-ospf-area-mismatch"
    assert scenario.tier == "A"
    assert isinstance(scenario.inject["r3"][0], FrrLines)
    assert scenario.ground_truth.component == "ospf.area"
    assert scenario.expected_failed_rules == ["core-ospf-r1-eth2", "reach-h1-srv"]


def test_scenario_id_pattern_and_node_names() -> None:
    base = {
        "title": "t",
        "tier": "A",
        "layer": "L3",
        "symptom": "s",
        "inject": {},
        "ground_truth": {"node": "r1", "layer": "L3", "component": "x", "summary": "s"},
        "expected_fix": {},
    }
    Scenario(id="002-two", **base)
    with pytest.raises(ValidationError):
        Scenario(id="two", **base)
    with pytest.raises(ValidationError):
        Scenario(id="003-x", **{**base, "inject": {"R1": []}})


def test_load_scenarios_skips_replay_files(tmp_path: Path, fixtures: Path) -> None:
    src = (fixtures / "001-ospf-area-mismatch.yaml").read_text()
    (tmp_path / "001-ospf-area-mismatch.yaml").write_text(src)
    (tmp_path / "001-ospf-area-mismatch.replay.yaml").write_text("steps: []\n")
    loaded = load_scenarios(tmp_path)
    assert [s.id for s in loaded] == ["001-ospf-area-mismatch"]
