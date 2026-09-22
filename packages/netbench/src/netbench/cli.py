"""`netbench` command line: run a matrix against the live servers, render a report."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import typer

from netbench.admin import HttpAdmin, read_admin_token
from netbench.claude_cli import ClaudeCliRunner
from netbench.clients import HttpToolClient
from netbench.harness import Harness, RunConfig
from netbench.local_agent import (
    DEFAULT_NUM_CTX,
    DEFAULT_OLLAMA,
    RESULT_CAP,
    LocalRunner,
    OllamaChat,
)
from netbench.report import load_records, render
from netbench.runner import FakeAgentRunner, ManualRunner, Runner
from nettwin_core.scenario import load_scenarios

app = typer.Typer(no_args_is_help=True, help="NetBench: scenarios in, scores out.")

RUNNERS = ("fake", "manual", "claude", "local")
#: Runners that drive a model through the skill; the others have no model or skill to label.
AGENTIC = ("claude", "local")
DEFAULT_MODEL = {"claude": "sonnet", "local": "qwen3:14b"}


def row_label(runner: str, skill: str, model: str) -> str:
    """`claude-diagnose-sonnet`, `local-diagnose-solo-qwen3-14b`, or just the runner name."""
    if runner == "claude":
        return f"claude-{skill}-{model}"
    if runner == "local":
        return f"local-{skill}-{model.replace(':', '-').replace('/', '-')}"
    return runner


def config_for(
    runner: str, skill: str, model: str, no_verifier: bool, name: str | None = None
) -> RunConfig:
    return RunConfig(
        name=name or f"{row_label(runner, skill, model)}{'-noverify' if no_verifier else ''}",
        runner=runner,
        model=model if runner in AGENTIC else None,
        verifier=not no_verifier,
        multi_agent=(skill == "diagnose") if runner in AGENTIC else None,
    )


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
        "headlessly with `claude -p`; local drives an Ollama model through the same tools "
        "and role files",
    ),
    model: str | None = typer.Option(
        None,
        help="claude runner: Claude Code model alias or id (default sonnet); local runner: "
        "Ollama tag such as qwen3:14b (the default) or qwen2.5-coder:7b",
    ),
    skill: str = typer.Option(
        "diagnose", help="claude/local runners: diagnose (team) or diagnose-solo (single agent)"
    ),
    max_turns: int = typer.Option(60, help="claude/local runners: turn budget for the commander"),
    run_timeout: float = typer.Option(
        1500.0, help="claude runner: kill the CLI after this many seconds"
    ),
    ollama: str = typer.Option(DEFAULT_OLLAMA, help="local runner: Ollama base URL"),
    num_ctx: int = typer.Option(DEFAULT_NUM_CTX, help="local runner: Ollama context window"),
    result_cap: int = typer.Option(
        RESULT_CAP, help="local runner: characters of a tool result the model gets to see"
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
    if runner not in RUNNERS:
        raise typer.BadParameter(f"runner must be one of {', '.join(RUNNERS)}")
    if skill not in ("diagnose", "diagnose-solo"):
        raise typer.BadParameter("skill must be diagnose or diagnose-solo")
    model = model or DEFAULT_MODEL.get(runner, "sonnet")
    config = config_for(runner, skill, model, no_verifier, name)
    token = read_admin_token(token_file)
    admin_client = HttpAdmin(admin, token)
    agent: Runner
    if runner == "fake":
        agent = FakeAgentRunner(chosen)
    elif runner == "manual":
        agent = ManualRunner(admin_client, timeout=timeout, notify=typer.echo)
    elif runner == "local":
        agent = LocalRunner(
            OllamaChat(model, base_url=ollama, num_ctx=num_ctx),
            roles_dir=Path(".claude") / "agents",
            skills_dir=Path(".claude") / "skills",
            skill=skill,
            max_turns=max_turns,
            transcripts_dir=results / matrix / "transcripts",
            result_cap=result_cap,
        )
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
        twin_client = HttpToolClient(twinlab, "twinlab")
        verify_client = HttpToolClient(netverify, "netverify")
        await twin_client.connect()
        await verify_client.connect()
        try:
            harness = Harness(
                twin=twin_client,
                verify=verify_client,
                admin=admin_client,
                results_dir=results,
                matrix=matrix,
            )
            records = await harness.run_matrix(
                chosen, config, agent, trials=trials, max_runs=max_runs
            )
        finally:
            await verify_client.aclose()
            await twin_client.aclose()
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
