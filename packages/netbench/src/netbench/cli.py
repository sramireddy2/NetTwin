"""`netbench` command line: run a matrix against the live servers, render a report."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import typer

from netbench.admin import HttpAdmin, read_admin_token
from netbench.claude_cli import ClaudeCliRunner
from netbench.clients import ToolClient, http_session
from netbench.harness import Harness, RunConfig
from netbench.report import load_records, render
from netbench.runner import FakeAgentRunner, ManualRunner, Runner
from nettwin_core.scenario import load_scenarios

app = typer.Typer(no_args_is_help=True, help="NetBench: scenarios in, scores out.")


def _select(scenario_ids: str, scenarios_dir: Path):
    scenarios = load_scenarios(scenarios_dir)
    if scenario_ids.strip().lower() == "all":
        return scenarios
    wanted = [s.strip() for s in scenario_ids.split(",") if s.strip()]
    chosen = [s for s in scenarios if any(s.id == w or s.id.startswith(w) for w in wanted)]
    if len(chosen) != len(wanted):
        raise typer.BadParameter(f"could not match every scenario in {wanted}")
    return chosen


@app.command()
def run(
    runner: str = typer.Option(
        "fake",
        help="fake replays each scenario's expected fix; manual waits for an interactive "
        "Claude Code /diagnose run and scores its export bundle; claude runs the skill "
        "headlessly with `claude -p`",
    ),
    model: str = typer.Option("sonnet", help="claude runner: Claude Code model alias or id"),
    skill: str = typer.Option(
        "diagnose", help="claude runner: diagnose (team) or diagnose-solo (single agent)"
    ),
    max_turns: int = typer.Option(60, help="claude runner: --max-turns for the commander"),
    run_timeout: float = typer.Option(
        1500.0, help="claude runner: kill the CLI after this many seconds"
    ),
    matrix: str = typer.Option("v0", help="Results are appended to results/<matrix>/runs.jsonl"),
    scenarios: str = typer.Option("all", help="'all' or comma-separated ids or prefixes"),
    trials: int = typer.Option(1, min=1),
    max_runs: int | None = typer.Option(None, help="Stop after this many new runs"),
    name: str | None = typer.Option(
        None, help="Row label in the report; defaults to the runner name"
    ),
    no_verifier: bool = typer.Option(
        False, "--no-verifier", help="Skip netverify in the runner (ablation)"
    ),
    timeout: float = typer.Option(
        1800.0, help="manual runner: seconds to wait for an export bundle per scenario"
    ),
    twinlab: str = typer.Option("http://localhost:8001/mcp"),
    netverify: str = typer.Option("http://localhost:8002/mcp"),
    admin: str = typer.Option("http://localhost:8001"),
    token_file: Path | None = typer.Option(None, help="Admin token; read from WSL when omitted"),
    scenarios_dir: Path = typer.Option(Path("lab/scenarios")),
    results: Path = typer.Option(Path("results")),
) -> None:
    """Run one configuration over the selected scenarios against the live servers."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    chosen = _select(scenarios, scenarios_dir)
    if runner not in ("fake", "manual", "claude"):
        raise typer.BadParameter("runner must be fake, manual or claude")
    if skill not in ("diagnose", "diagnose-solo"):
        raise typer.BadParameter("skill must be diagnose or diagnose-solo")
    label = f"claude-{skill}-{model}" if runner == "claude" else runner
    config = RunConfig(
        name=name or f"{label}{'-noverify' if no_verifier else ''}",
        runner=runner,
        model=model if runner == "claude" else None,
        verifier=not no_verifier,
        multi_agent=(skill == "diagnose") if runner == "claude" else None,
    )
    token = read_admin_token(token_file)
    admin_client = HttpAdmin(admin, token)
    agent: Runner
    if runner == "fake":
        agent = FakeAgentRunner(chosen)
    elif runner == "manual":
        agent = ManualRunner(admin_client, timeout=timeout, notify=typer.echo)
    else:
        agent = ClaudeCliRunner(
            admin_client,
            model=model,
            skill=skill,
            max_turns=max_turns,
            timeout=run_timeout,
            cwd=Path.cwd(),
            transcripts_dir=results / matrix / "transcripts",
        )

    async def main() -> int:
        async with http_session(twinlab) as twin_s, http_session(netverify) as verify_s:
            harness = Harness(
                twin=ToolClient(twin_s, "twinlab"),
                verify=ToolClient(verify_s, "netverify"),
                admin=admin_client,
                results_dir=results,
                matrix=matrix,
            )
            records = await harness.run_matrix(
                chosen, config, agent, trials=trials, max_runs=max_runs
            )
        typer.echo(f"{len(records)} new runs recorded in {harness.results_path}")
        return 0

    raise typer.Exit(asyncio.run(main()))


@app.command()
def report(
    matrix: str = typer.Option("v0"),
    results: Path = typer.Option(Path("results")),
    write: Path | None = typer.Option(None, help="Also write the markdown here"),
) -> None:
    """Render summary and per-scenario tables from results/<matrix>/runs.jsonl."""
    text = render(load_records(results / matrix / "runs.jsonl"), matrix)
    typer.echo(text)
    if write is not None:
        write.parent.mkdir(parents=True, exist_ok=True)
        write.write_text(text, encoding="utf-8")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
