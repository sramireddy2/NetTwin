"""intent_check on the live lab: golden passes, each scenario fails exactly its rules."""

from __future__ import annotations

from pathlib import Path

import pytest

from nettwin_core.executor import DockerExecutor
from nettwin_core.scenario import Scenario, load_scenarios
from nettwin_core.settings import Settings
from netverify.app import NetVerify
from netverify.attest import verify
from twinlab.app import TwinLab

pytestmark = pytest.mark.lab

LAB = Path(__file__).resolve().parents[2] / "lab"
SCENARIOS = load_scenarios(LAB / "scenarios")


@pytest.fixture(scope="module")
def twin(settings: Settings, executor: DockerExecutor) -> TwinLab:
    return TwinLab.from_settings(settings, executor=executor)


@pytest.fixture(scope="module")
def verifier(settings: Settings, executor: DockerExecutor) -> NetVerify:
    return NetVerify.from_settings(settings, executor=executor)


async def test_golden_passes_every_rule(verifier: NetVerify) -> None:
    report = await verifier.intent_check()
    assert report.passed, report.failed_rules
    assert report.attestation is not None and verify(verifier.key, report.attestation)


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.id)
async def test_scenario_fails_exactly_its_rules(
    twin: TwinLab, verifier: NetVerify, scenario: Scenario
) -> None:
    golden = await twin.snapshot()
    try:
        await twin.inject(scenario.id)
        report = await verifier.intent_check(converge_timeout=20)
    finally:
        result = await twin.rollback(golden.id)
    failed = set(report.failed_rules) - {"converged"}
    assert failed == set(scenario.expected_failed_rules), report.rules
    # A planted fault must not be attested; the no-fault control must be.
    assert (report.attestation is None) == bool(scenario.expected_failed_rules)
    assert result["matches_target"] is True
    after = await verifier.wait_converged(timeout=60)
    assert after.converged, after
