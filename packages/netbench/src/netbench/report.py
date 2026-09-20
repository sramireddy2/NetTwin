"""Turn runs.jsonl into the tables that go in the README."""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

from netbench.harness import RunRecord


def load_records(path: Path) -> list[RunRecord]:
    if not path.exists():
        return []
    return [
        RunRecord.model_validate(json.loads(line))
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _pct(hits: int, total: int) -> str:
    return f"{100 * hits / total:.0f}%" if total else "-"


def summary_table(records: list[RunRecord]) -> str:
    by_config: dict[str, list[RunRecord]] = defaultdict(list)
    for r in records:
        by_config[r.config.name].append(r)
    lines = [
        "| Config | Runs | Root cause | Verified fix | No collateral | Minimal | Errors | Mean s |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for name, rows in sorted(by_config.items()):
        n = len(rows)
        lines.append(
            f"| {name} | {n} | {_pct(sum(r.score.root_cause for r in rows), n)} | "
            f"{_pct(sum(r.score.verified for r in rows), n)} | "
            f"{_pct(sum(r.score.collateral_free for r in rows), n)} | "
            f"{_pct(sum(r.score.minimal for r in rows), n)} | "
            f"{sum(r.error is not None for r in rows)} | "
            f"{sum(r.duration_s for r in rows) / n:.0f} |"
        )
    return "\n".join(lines)


def scenario_table(records: list[RunRecord]) -> str:
    configs = sorted({r.config.name for r in records})
    by_key: dict[tuple[str, str], list[RunRecord]] = defaultdict(list)
    for r in records:
        by_key[(r.scenario_id, r.config.name)].append(r)
    scenarios = sorted({r.scenario_id for r in records})
    header = "| Scenario | " + " | ".join(configs) + " |"
    lines = [header, "|---|" + "---|" * len(configs)]
    for scenario in scenarios:
        cells = []
        for config in configs:
            rows = by_key.get((scenario, config), [])
            if not rows:
                cells.append("-")
                continue
            marks = "".join(
                ("R" if r.score.root_cause else "r")
                + ("V" if r.score.verified else "v")
                + ("C" if r.score.collateral_free else "c")
                for r in rows
            )
            cells.append(marks)
        lines.append(f"| {scenario} | " + " | ".join(cells) + " |")
    lines.append("")
    lines.append(
        "Legend per trial: R/r root cause found or not, V/v fix verified or not, "
        "C/c no collateral or collateral."
    )
    return "\n".join(lines)


def render(records: list[RunRecord], matrix: str) -> str:
    if not records:
        return f"# NetBench {matrix}\n\nNo runs recorded.\n"
    return (
        f"# NetBench {matrix}\n\n"
        f"{len(records)} runs over {len({r.scenario_id for r in records})} scenarios.\n\n"
        f"## Summary\n\n{summary_table(records)}\n\n"
        f"## Per scenario\n\n{scenario_table(records)}\n"
    )
