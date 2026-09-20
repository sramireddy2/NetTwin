"""Runner contract plus the fake agent.

A runner gets the symptom and two tool clients and must come back with a `RunOutput`. The
fake agent knows every answer (it holds the scenario catalog) and replays each scenario's
expected fix through the real tool surface: it proves the servers, the harness and the
scoring end to end at zero token cost, and it is the ceiling every real agent is measured
against.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from pydantic import BaseModel, Field

from netbench.admin import AdminClient
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
    cache_read_tokens: int | None = None
    cache_creation_tokens: int | None = None
    cost_usd: float | None = None
    notes: str = ""


class RunnerUnavailable(RuntimeError):
    """The runner cannot produce runs right now (logged out, rate limited): stop the matrix
    instead of recording failures that say nothing about the agent."""


@dataclass
class RunContext:
    scenario_id: str
    symptom: str
    twin: ToolClient
    verify: ToolClient
    use_verifier: bool = True
    config_name: str = ""
    trial: int = 1


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
        root_cause = (
            RootCause(node=gt.node, layer=gt.layer, component=gt.component, summary=gt.summary)
            if gt is not None
            else None  # control scenario: the honest answer is "no fault"
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
                    "rationale": f"fix {gt.component if gt else 'nothing'} on {node}",
                },
            )
            change_ids.append(applied.require()["change_id"])

        verification: VerificationReport | None = None
        export: dict[str, Any] | None = None
        if ctx.use_verifier:
            await ctx.verify.call("wait_converged", {"timeout": 60})
            report = await ctx.verify.call("intent_check", timeout=300)
            verification = VerificationReport.model_validate(report.require())
            if verification.passed and change_ids:
                exported = await ctx.twin.call(
                    "export_change",
                    {
                        "change_ids": change_ids,
                        "verification": verification.model_dump(mode="json"),
                        "root_cause": root_cause.model_dump() if root_cause else None,
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


class ManualRunner:
    """Scores a run made by a human or an interactive Claude Code session.

    The harness resets and injects as usual. This runner then prints the symptom and waits
    for a new export bundle to appear through twinlab's admin route while the operator runs
    /diagnose in Claude Code. The bundle carries the root cause, the change ids and the
    verification report, which is everything the scorer needs. Tool calls are not recorded
    here; the Claude Code transcript has them.
    """

    name = "manual"

    def __init__(
        self,
        admin: AdminClient,
        *,
        timeout: float = 1800,
        poll: float = 5.0,
        decision_wait: float = 180,
        notify: Callable[[str], None] = print,
    ) -> None:
        self.admin = admin
        self.timeout = timeout
        self.poll = poll
        self.decision_wait = decision_wait
        self.notify = notify

    async def run(self, ctx: RunContext) -> RunOutput:
        seen = {e["export_id"] for e in await self.admin.exports()}
        self.notify(
            f"\n=== {ctx.scenario_id} is injected. Symptom:\n{ctx.symptom}\n"
            f"Run /diagnose in Claude Code now. Waiting up to {self.timeout:.0f}s for an "
            "export bundle to appear...\n"
        )
        deadline = time.monotonic() + self.timeout
        while True:
            new = [e for e in await self.admin.exports() if e["export_id"] not in seen]
            if new:
                bundle = await self._wait_for_decision(new[-1]["export_id"])
                self.notify(f"=== export {bundle['export_id']} is {bundle['status']}\n")
                return self.output_from_bundle(bundle)
            if time.monotonic() >= deadline:
                return RunOutput(notes=f"no export appeared within {self.timeout:.0f}s")
            await asyncio.sleep(self.poll)

    async def _wait_for_decision(self, export_id: str) -> dict[str, Any]:
        """A bundle is saved as pending while the operator looks at the approval dialog."""
        deadline = time.monotonic() + self.decision_wait
        while True:
            bundle = await self.admin.export(export_id)
            if bundle["status"] != "pending" or time.monotonic() >= deadline:
                return bundle
            await asyncio.sleep(self.poll)

    @staticmethod
    def output_from_bundle(bundle: dict[str, Any]) -> RunOutput:
        return RunOutput(
            root_cause=RootCause.model_validate(bundle["root_cause"]),
            change_ids=[c["change_id"] for c in bundle["changes"]],
            verification=VerificationReport.model_validate(bundle["verification"]),
            export={
                "export_id": bundle["export_id"],
                "status": bundle["status"],
                "decided_by": bundle.get("decided_by"),
            },
            notes="interactive run; output read from the export bundle",
        )
