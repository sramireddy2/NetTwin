"""Inject every scenario, confirm its probe sees the symptom, roll back, converge (marker: lab)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from nettwin_core.executor import DockerExecutor
from nettwin_core.scenario import Probe, Scenario, load_scenarios
from nettwin_core.settings import Settings
from twinlab.app import TwinLab

pytestmark = pytest.mark.lab

LAB = Path(__file__).resolve().parents[2] / "lab"
SCENARIOS = load_scenarios(LAB / "scenarios")
FULL = {"r1": 3, "r2": 3, "r3": 2, "r4": 2}


async def _converged(executor: DockerExecutor, timeout: float = 60) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        ok = True
        for node, count in FULL.items():
            res = await executor.exec(node, ["vtysh", "-c", "show ip ospf neighbor"])
            ok = ok and res.stdout.count("Full") >= count
        bgp = await executor.exec("r4", ["vtysh", "-c", "show bgp neighbors 203.0.113.2 json"])
        ok = ok and '"bgpState":"Established"' in bgp.stdout
        if ok:
            return
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("lab did not converge")
        await asyncio.sleep(2)


def _matches(probe: Probe, ok: bool, stdout: str) -> bool:
    if probe.expect == "fail" and ok:
        return False
    if probe.expect == "ok" and not ok and probe.contains is None and probe.absent is None:
        return False
    if probe.contains is not None and probe.contains not in stdout:
        return False
    return not (probe.absent is not None and probe.absent in stdout)


async def _probe_until(app: TwinLab, probe: Probe, timeout: float = 25) -> str:
    deadline = asyncio.get_running_loop().time() + timeout
    last = ""
    while True:
        res = await app.show(probe.node, probe.cmd)
        last = res.stdout + res.stderr
        if _matches(probe, res.ok, res.stdout):
            return last
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"probe never matched: {probe}\n{last}")
        await asyncio.sleep(1)


@pytest.fixture(scope="module")
def app(settings: Settings, executor: DockerExecutor) -> TwinLab:
    return TwinLab.from_settings(settings, executor=executor)


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.id)
async def test_scenario_symptom_and_rollback(
    app: TwinLab, executor: DockerExecutor, scenario: Scenario
) -> None:
    await _converged(executor)
    golden = await app.snapshot()
    assert scenario.probe is not None
    try:
        injected = await app.inject(scenario.id)
        assert injected["after"] != golden.id, "injection changed nothing"
        await _probe_until(app, scenario.probe)
    finally:
        result = await app.rollback(golden.id)
    assert result["matches_target"] is True, golden.diff(app.store.load(result["after"]))
    await _converged(executor)
