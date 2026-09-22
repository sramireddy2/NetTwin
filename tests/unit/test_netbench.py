"""Harness, fake agent, scoring and report on scripted twins, through real MCP sessions."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from netbench.admin import CallableAdmin
from netbench.clients import ToolClient, memory_session
from netbench.harness import Harness, RunConfig
from netbench.report import load_records, render
from netbench.runner import FakeAgentRunner, RunOutput
from netbench.scoring import score_run
from nettwin_core.executor import FakeExecutor
from nettwin_core.models import ChangeResult, RootCause, RuleResult, VerificationReport
from nettwin_core.ops import SetMtu
from nettwin_core.scenario import load_scenarios
from nettwin_core.settings import Settings
from netverify.app import NetVerify
from netverify.server import build_server as build_netverify
from tests.unit.test_netverify import make_fake
from twinlab.app import TwinLab
from twinlab.server import build_server as build_twinlab

LAB = Path(__file__).resolve().parents[2] / "lab"
SCENARIOS = load_scenarios(LAB / "scenarios")


def _twin_scripted(node: str, argv: tuple[str, ...]) -> str | None:
    if argv[:2] == ("vtysh", "-c") and argv[2] == "show running-config":
        return f"hostname {node}\nend\n"
    if argv[0] == "ip":
        return "[]"
    if argv[0] in ("nft", "bridge", "vtysh"):
        return ""
    return None


def _env(tmp_path: Path) -> dict[str, str]:
    return {
        "NETTWIN_STATE_DIR": str(tmp_path),
        "NETTWIN_TOPOLOGY": str(LAB / "topology.clab.yml"),
        "NETTWIN_POLICY": str(LAB / "policy" / "intent.yaml"),
        "NETTWIN_SCENARIOS": str(LAB / "scenarios"),
        "NETTWIN_BENCH": "1",
    }


async def test_fake_agent_scores_perfectly_on_a_scripted_twin(tmp_path: Path) -> None:
    settings = Settings.from_env(_env(tmp_path))
    twin_app = TwinLab.from_settings(settings, executor=FakeExecutor().on(_twin_scripted))
    verify_app = NetVerify.from_settings(settings, executor=make_fake())
    scenario = SCENARIOS[0]
    async with (
        memory_session(build_twinlab(twin_app)) as twin_s,
        memory_session(build_netverify(verify_app)) as verify_s,
    ):
        harness = Harness(
            twin=ToolClient(twin_s, "twinlab"),
            verify=ToolClient(verify_s, "netverify"),
            admin=CallableAdmin(twin_app.inject, twin_app.status),
            results_dir=tmp_path / "results",
            matrix="unit",
        )
        config = RunConfig(name="fake", runner="fake")
        records = await harness.run_matrix([scenario], config, FakeAgentRunner(SCENARIOS))
        assert len(records) == 1
        record = records[0]
        assert record.error is None
        assert record.score.root_cause and record.score.verified
        assert record.score.collateral_free and record.score.minimal and record.score.exported
        assert record.output.export["decided_by"] == "bench-auto"
        tools = [c.tool for c in record.output.tool_calls]
        assert tools[:3] == ["run_show_command", "snapshot", "apply_config"]
        assert "intent_check" in tools and "export_change" in tools

        # Resumable: the same run id is skipped.
        again = await harness.run_matrix([scenario], config, FakeAgentRunner(SCENARIOS))
        assert again == []
        assert harness.existing_run_ids() == {f"{scenario.id}/fake/1"}

        # A runner crash is recorded, not raised.
        class Crashing:
            name = "crash"

            async def run(self, ctx: object) -> RunOutput:
                raise RuntimeError("boom")

        crashed = await harness.run_one(
            scenario, RunConfig(name="crash", runner="crash"), 1, Crashing()
        )
        assert crashed.error == "RuntimeError: boom"
        assert not crashed.score.root_cause and not crashed.score.verified

    loaded = load_records(harness.results_path)
    assert [r.run_id for r in loaded] == [f"{scenario.id}/fake/1", f"{scenario.id}/crash/1"]
    text = render(loaded, "unit")
    assert "| fake | 1 | 100% | 100% | 100% | 100% | 100% | 0 |" in text
    assert "| crash | 1 | 0% | 100% | 0% |" in text  # the scripted twin always checks out
    assert f"| {scenario.id} | rFvC | RFVC |" in text  # columns are sorted by config name


class _FlakyVerify(ToolClient):
    """Times out on `reachability_matrix` the first `failures` times, as a dropped MCP
    event stream does after the harness has idled through a run."""

    def __init__(self, session: object, failures: int) -> None:
        super().__init__(session, "netverify")  # type: ignore[arg-type]
        self.failures = failures
        self.timeouts = 0
        self.reconnects = 0

    async def reconnect(self) -> None:
        self.reconnects += 1

    async def call(self, tool: str, args: dict | None = None, *, timeout: float = 180):
        if tool == "reachability_matrix" and self.timeouts < self.failures:
            self.timeouts += 1
            raise RuntimeError("Request 'tools/call' timed out")
        return await super().call(tool, args, timeout=timeout)


async def test_post_run_check_retries_once_then_records_the_failure(tmp_path: Path) -> None:
    settings = Settings.from_env(_env(tmp_path))
    twin_app = TwinLab.from_settings(settings, executor=FakeExecutor().on(_twin_scripted))
    verify_app = NetVerify.from_settings(settings, executor=make_fake())
    scenario = SCENARIOS[0]
    async with (
        memory_session(build_twinlab(twin_app)) as twin_s,
        memory_session(build_netverify(verify_app)) as verify_s,
    ):
        for failures, expect_error in ((1, False), (99, True)):
            verify = _FlakyVerify(verify_s, failures=0)
            harness = Harness(
                twin=ToolClient(twin_s, "twinlab"),
                verify=verify,
                admin=CallableAdmin(twin_app.inject, twin_app.status),
                results_dir=tmp_path / f"results{failures}",
                matrix="unit",
            )
            harness.retry_delay = 0
            await harness.baseline()  # takes its own matrix; the drop happens after the run
            verify.failures = failures
            record = await harness.run_one(
                scenario, RunConfig(name="fake", runner="fake"), 1, FakeAgentRunner(SCENARIOS)
            )
            assert verify.timeouts == min(failures, 2)
            assert verify.reconnects == 1  # the session is reopened once, between the attempts
            assert record.score.root_cause and record.score.verified
            if expect_error:  # the record carries the gap; the batch goes on
                assert record.error == "HarnessCheckFailed: netverify did not answer"
            else:
                assert record.error is None and record.score.collateral_free
            assert harness.existing_run_ids() == {f"{scenario.id}/fake/1"}


def _report(passed: bool) -> VerificationReport:
    return VerificationReport(
        passed=passed,
        snapshot_id="a" * 64,
        policy_sha256="b" * 64,
        rules=[RuleResult(rule_id="x", kind="reach", passed=passed)],
    )


def test_score_run_root_cause_collateral_and_minimality() -> None:
    scenario = SCENARIOS[3]  # 004: r2 link.mtu, expected fix = one set_mtu on r2
    golden = [
        {"rule_id": "a", "ok": True},
        {"rule_id": "b", "ok": False},
        {"rule_id": "c", "ok": True},
    ]
    good_change = ChangeResult(
        change_id="1",
        node="r2",
        before_snapshot_id="x",
        after_snapshot_id="y",
        diff="",
        ops=[SetMtu(iface="eth1", mtu=1500)],
    )
    output = RunOutput(
        root_cause=RootCause(node="r2", layer="L2", component="link.mtu", summary="s"),
        change_ids=["1"],
        verification=_report(True),
        export={"status": "approved"},
    )
    score = score_run(scenario, output, [good_change], golden, golden)
    assert score.root_cause and score.verified and score.collateral_free and score.minimal
    assert score.exported and score.ops_applied == 1 and score.ops_expected == 1

    wrong = output.model_copy(
        update={"root_cause": RootCause(node="r2", layer="L2", component="ospf.mtu", summary="s")}
    )
    partial = score_run(scenario, wrong, [good_change], golden, golden)
    assert partial.root_cause_node and not partial.root_cause_component and not partial.root_cause

    after = [
        {"rule_id": "a", "ok": False},
        {"rule_id": "b", "ok": False},
        {"rule_id": "c", "ok": True},
    ]
    damaged = score_run(scenario, output, [good_change], golden, after)
    assert damaged.collateral == ["a"] and not damaged.collateral_free

    sprawl = [
        good_change,
        ChangeResult(
            change_id="2",
            node="r1",
            before_snapshot_id="x",
            after_snapshot_id="y",
            diff="",
            ops=[SetMtu(iface="eth1", mtu=1500)],
        ),
    ]
    assert not score_run(scenario, output, sprawl, golden, golden).minimal
    assert not score_run(scenario, RunOutput(), [], golden, golden).minimal


def test_results_jsonl_is_one_record_per_line(tmp_path: Path) -> None:
    path = tmp_path / "runs.jsonl"
    path.write_text("", encoding="utf-8")
    assert load_records(path) == []
    assert "No runs recorded" in render([], "empty")
    assert json.loads('{"a": 1}') == {"a": 1}


async def test_tool_call_is_abandoned_past_its_hard_bound() -> None:
    import asyncio

    class Hanging:
        async def call_tool(self, *args: object, **kwargs: object) -> None:
            await asyncio.sleep(3600)

    client = ToolClient(Hanging(), "netverify")  # type: ignore[arg-type]
    client.hard_margin = 0.2
    with pytest.raises(TimeoutError):
        await client.call("intent_check", timeout=0.1)
    assert client.calls == []


async def test_http_tool_client_reopens_its_session_and_retries_once() -> None:
    import asyncio

    from mcp.types import CallToolResult, TextContent

    from netbench.clients import HttpToolClient

    class Hanging:
        async def call_tool(self, *args: object, **kwargs: object) -> None:
            await asyncio.sleep(3600)

    class Working:
        async def call_tool(self, name: str, args: dict, **kwargs: object) -> CallToolResult:
            return CallToolResult(content=[TextContent(type="text", text="ok")], isError=False)

    class Fake(HttpToolClient):
        def __init__(self) -> None:
            super().__init__("http://unused/mcp", "twinlab")
            self.sessions: list[object] = [Hanging(), Working()]
            self.reconnects = 0

        async def connect(self) -> None:
            self.session = self.sessions.pop(0)  # type: ignore[assignment]

        async def aclose(self) -> None:
            self.reconnects += 1

    client = Fake()
    client.hard_margin = 0.2
    await client.connect()
    result = await client.call("snapshot", timeout=0.1)
    assert result.ok and result.text == "ok"
    assert client.reconnects == 1 and client.sessions == []
    assert [c.tool for c in client.calls] == ["snapshot"]  # the abandoned attempt is not a call
