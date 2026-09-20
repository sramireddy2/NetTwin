"""Runner contract plus the fake agent.

A runner gets the symptom and two tool clients and must come back with a `RunOutput`. The
fake agent knows every answer (it holds the scenario catalog) and replays each scenario's
expected fix through the real tool surface: it proves the servers, the harness and the
scoring end to end at zero token cost, and it is the ceiling every real agent is measured
against.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from pydantic import BaseModel, Field

from netbench.clients import ToolClient
from nettwin_core.models import RootCause, VerificationReport
from nettwin_core.scenario import Scenario


class ToolCall(BaseModel):
    server: str
    tool: str
    ok: bool
    duration_ms: int


class RunOutput(BaseModel):
    root_cause: RootCause | None = None
    change_ids: list[str] = Field(default_factory=list)
    verification: VerificationReport | None = None
    export: dict[str, Any] | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    notes: str = ""


@dataclass
class RunContext:
    scenario_id: str
    symptom: str
    twin: ToolClient
    verify: ToolClient
    use_verifier: bool = True


class Runner(Protocol):
    name: str

    async def run(self, ctx: RunContext) -> RunOutput: ...


def collect_calls(*clients: ToolClient) -> list[ToolCall]:
    calls: list[ToolCall] = []
    for client in clients:
        calls.extend(
            ToolCall(server=client.name, tool=c.tool, ok=c.ok, duration_ms=c.duration_ms)
            for c in client.calls
        )
    return calls


class FakeAgentRunner:
    """Replays the scenario's expected fix. Reads ground truth; real agents never do."""

    name = "fake"

    def __init__(self, scenarios: list[Scenario]) -> None:
        self.catalog = {s.id: s for s in scenarios}

    async def run(self, ctx: RunContext) -> RunOutput:
        scenario = self.catalog[ctx.scenario_id]
        gt = scenario.ground_truth
        root_cause = RootCause(
            node=gt.node, layer=gt.layer, component=gt.component, summary=gt.summary
        )

        await ctx.twin.read_resource("lab://topology")
        if scenario.probe is not None:
            await ctx.twin.call(
                "run_show_command", {"node": scenario.probe.node, "cmd": scenario.probe.cmd}
            )
        await ctx.twin.call("snapshot")

        change_ids: list[str] = []
        for node, ops in scenario.expected_fix.items():
            applied = await ctx.twin.call(
                "apply_config",
                {
                    "node": node,
                    "ops": [op.model_dump() for op in ops],
                    "rationale": f"fix {gt.component} on {node}",
                },
            )
            change_ids.append(applied.require()["change_id"])

        verification: VerificationReport | None = None
        export: dict[str, Any] | None = None
        if ctx.use_verifier:
            await ctx.verify.call("wait_converged", {"timeout": 60})
            report = await ctx.verify.call("intent_check", timeout=300)
            verification = VerificationReport.model_validate(report.require())
            if verification.passed:
                exported = await ctx.twin.call(
                    "export_change",
                    {
                        "change_ids": change_ids,
                        "verification": verification.model_dump(mode="json"),
                        "root_cause": root_cause.model_dump(),
                        "summary": scenario.title,
                    },
                )
                export = exported.data if exported.ok else {"error": exported.text}

        return RunOutput(
            root_cause=root_cause,
            change_ids=change_ids,
            verification=verification,
            export=export,
            tool_calls=collect_calls(ctx.twin, ctx.verify),
            notes="fake agent replayed expected_fix",
        )
