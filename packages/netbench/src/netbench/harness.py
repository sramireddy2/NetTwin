"""The NetBench loop: reset, inject, run, score, reset. Resumable, one JSONL line per run."""

from __future__ import annotations

import json
import logging
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from netbench.admin import AdminClient
from netbench.clients import ToolClient
from netbench.runner import RunContext, Runner, RunOutput
from netbench.scoring import Score, score_run
from nettwin_core.models import ChangeResult
from nettwin_core.scenario import Scenario

log = logging.getLogger("netbench")


class RunConfig(BaseModel):
    name: str = Field(description="Row label in the report, e.g. fake, claude-sonnet-verifier")
    runner: str
    model: str | None = None
    verifier: bool = True
    multi_agent: bool | None = None


class RunRecord(BaseModel):
    run_id: str
    matrix: str
    scenario_id: str
    config: RunConfig
    trial: int
    started_at: datetime
    duration_s: float
    output: RunOutput
    score: Score
    error: str | None = None


def run_id(scenario_id: str, config: RunConfig, trial: int) -> str:
    return f"{scenario_id}/{config.name}/{trial}"


class Harness:
    def __init__(
        self,
        twin: ToolClient,
        verify: ToolClient,
        admin: AdminClient,
        results_dir: Path,
        matrix: str,
    ) -> None:
        self.twin = twin
        self.verify = verify
        self.admin = admin
        self.results_dir = results_dir
        self.matrix = matrix
        self.golden_snapshot: str | None = None
        self.golden_probes: list[dict[str, Any]] = []

    # --- results file ------------------------------------------------------------------------

    @property
    def results_path(self) -> Path:
        return self.results_dir / self.matrix / "runs.jsonl"

    def existing_run_ids(self) -> set[str]:
        if not self.results_path.exists():
            return set()
        ids: set[str] = set()
        for line in self.results_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                ids.add(json.loads(line)["run_id"])
        return ids

    def append(self, record: RunRecord) -> None:
        self.results_path.parent.mkdir(parents=True, exist_ok=True)
        with self.results_path.open("a", encoding="utf-8") as fh:
            fh.write(record.model_dump_json() + "\n")

    # --- twin control ------------------------------------------------------------------------

    async def baseline(self) -> None:
        """Snapshot the golden twin and record its reachability matrix."""
        converged = await self.verify.call("wait_converged", {"timeout": 90})
        if not converged.require()["converged"]:
            raise RuntimeError(f"twin is not converged before baseline: {converged.data}")
        snap = await self.twin.call("snapshot")
        self.golden_snapshot = snap.require()["id"]
        matrix = await self.verify.call("reachability_matrix")
        self.golden_probes = matrix.require()["probes"]
        log.info(
            "baseline snapshot %s, %d probes", self.golden_snapshot[:12], len(self.golden_probes)
        )

    async def reset(self) -> None:
        assert self.golden_snapshot is not None
        rolled = await self.twin.call(
            "rollback", {"snapshot_id": self.golden_snapshot}, timeout=300
        )
        if not rolled.require()["matches_target"]:
            raise RuntimeError(f"reset did not reach the golden snapshot: {rolled.data}")
        await self.verify.call("wait_converged", {"timeout": 90})

    async def changes_for(self, change_ids: list[str]) -> list[ChangeResult]:
        changes: list[ChangeResult] = []
        for change_id in change_ids:
            got = await self.twin.call("get_change", {"change_id": change_id})
            if got.ok and got.data:
                changes.append(ChangeResult.model_validate(got.data))
        return changes

    # --- one run -------------------------------------------------------------------------

    async def run_one(
        self, scenario: Scenario, config: RunConfig, trial: int, runner: Runner
    ) -> RunRecord:
        rid = run_id(scenario.id, config, trial)
        if self.golden_snapshot is None:
            await self.baseline()
        await self.reset()
        await self.admin.inject(scenario.id)
        await self.verify.call("wait_converged", {"timeout": 30})
        self.twin.calls.clear()
        self.verify.calls.clear()
        started = datetime.now(UTC)
        clock = time.monotonic()
        error: str | None = None
        try:
            output = await runner.run(
                RunContext(
                    scenario_id=scenario.id,
                    symptom=scenario.symptom,
                    twin=self.twin,
                    verify=self.verify,
                    use_verifier=config.verifier,
                )
            )
        except Exception as exc:  # noqa: BLE001 - a runner crash is a scored failure
            log.exception("run %s crashed", rid)
            error = f"{type(exc).__name__}: {exc}"
            output = RunOutput(notes="runner crashed")
        duration = round(time.monotonic() - clock, 1)
        after = await self.verify.call("reachability_matrix")
        after_probes = after.data["probes"] if after.ok and after.data else []
        changes = await self.changes_for(output.change_ids)
        score = score_run(scenario, output, changes, self.golden_probes, after_probes)
        record = RunRecord(
            run_id=rid,
            matrix=self.matrix,
            scenario_id=scenario.id,
            config=config,
            trial=trial,
            started_at=started,
            duration_s=duration,
            output=output,
            score=score,
            error=error,
        )
        self.append(record)
        await self.reset()
        log.info(
            "%s: root_cause=%s verified=%s collateral_free=%s minimal=%s (%.0fs)",
            rid,
            score.root_cause,
            score.verified,
            score.collateral_free,
            score.minimal,
            duration,
        )
        return record

    async def run_matrix(
        self,
        scenarios: list[Scenario],
        config: RunConfig,
        runner: Runner,
        *,
        trials: int = 1,
        max_runs: int | None = None,
    ) -> list[RunRecord]:
        done = self.existing_run_ids()
        records: list[RunRecord] = []
        for trial in range(1, trials + 1):
            for scenario in scenarios:
                if max_runs is not None and len(records) >= max_runs:
                    return records
                rid = run_id(scenario.id, config, trial)
                if rid in done:
                    log.info("skip %s (already recorded)", rid)
                    continue
                records.append(await self.run_one(scenario, config, trial, runner))
        return records
