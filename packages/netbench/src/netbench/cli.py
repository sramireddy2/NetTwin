"""`netbench` command line: run a matrix against the live servers, render a report."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import typer

from netbench.admin import HttpAdmin, read_admin_token
from netbench.clients import ToolClient, http_session
from netbench.harness import Harness, RunConfig
from netbench.report import load_records, render
from netbench.runner import FakeAgentRunner
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
    runner: str = typer.Option("fake", help="fake (more runners arrive with later milestones)"),
    matrix: str = typer.Option("v0", help="Results are appended to results/<matrix>/runs.jsonl"),
    scenarios: str = typer.Option("all", help="'all' or comma-separated ids or prefixes"),
    trials: int = typer.Option(1, min=1),
    max_runs: int | None = typer.Option(None, help="Stop after this many new runs"),
    no_verifier: bool = typer.Option(
        False, "--no-verifier", help="Skip netverify in the runner (ablation)"
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
    if runner != "fake":
        raise typer.BadParameter("only the fake runner exists yet")
    config = RunConfig(
        name=f"{runner}{'-noverify' if no_verifier else ''}",
        runner=runner,
        verifier=not no_verifier,
    )
    token = read_admin_token(token_file)

    async def main() -> int:
        async with http_session(twinlab) as twin_s, http_session(netverify) as verify_s:
            harness = Harness(
                twin=ToolClient(twin_s, "twinlab"),
                verify=ToolClient(verify_s, "netverify"),
                admin=HttpAdmin(admin, token),
                results_dir=results,
                matrix=matrix,
            )
            records = await harness.run_matrix(
                chosen, config, FakeAgentRunner(chosen), trials=trials, max_runs=max_runs
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
