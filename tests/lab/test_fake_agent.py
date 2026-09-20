"""The fake agent must score 100% on every scenario against the live lab (marker: lab)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from netbench.admin import CallableAdmin
from netbench.clients import ToolClient, memory_session
from netbench.harness import Harness, RunConfig
from netbench.report import render
from netbench.runner import FakeAgentRunner
from nettwin_core.executor import DockerExecutor
from nettwin_core.scenario import load_scenarios
from nettwin_core.settings import Settings
from netverify.app import NetVerify
from netverify.server import build_server as build_netverify
from twinlab.app import TwinLab
from twinlab.server import build_server as build_twinlab

pytestmark = pytest.mark.lab

LAB = Path(__file__).resolve().parents[2] / "lab"
SCENARIOS = load_scenarios(LAB / "scenarios")


async def test_fake_agent_is_perfect_on_every_scenario(
    settings: Settings, executor: DockerExecutor, tmp_path: Path
) -> None:
    bench = Settings.from_env({**os.environ, "NETTWIN_BENCH": "1"})
    twin_app = TwinLab.from_settings(bench, executor=executor)
    verify_app = NetVerify.from_settings(settings, executor=executor)
    async with (
        memory_session(build_twinlab(twin_app)) as twin_s,
        memory_session(build_netverify(verify_app)) as verify_s,
    ):
        harness = Harness(
            twin=ToolClient(twin_s, "twinlab"),
            verify=ToolClient(verify_s, "netverify"),
            admin=CallableAdmin(twin_app.inject, twin_app.status),
            results_dir=tmp_path / "results",
            matrix="lab-fake",
        )
        records = await harness.run_matrix(
            SCENARIOS, RunConfig(name="fake", runner="fake"), FakeAgentRunner(SCENARIOS)
        )
    assert len(records) == len(SCENARIOS)
    bad = [
        (r.run_id, r.error, r.score.model_dump())
        for r in records
        if r.error
        or not (
            r.score.root_cause and r.score.verified and r.score.collateral_free and r.score.minimal
        )
    ]
    assert not bad, bad
    print(render(records, "lab-fake"))
