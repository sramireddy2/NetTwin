"""Record one live NetTwin incident for the README.

Plants a NetBench scenario on the running twin, lets the Claude Code agent team work it
headlessly, prints every agent step the moment it happens, pings the broken path before and
after, and prints the harness's own score. Everything printed is also saved with its time
offset to a JSON recording that `scripts/render_demo.py` turns into the animated terminal in
`docs/media/`.

    uv run python scripts/record_demo.py 019 --out docs/media/demo-019.json

Needs the servers in bench mode (`make -C lab serve-bench` in WSL) and a logged-in `claude`
CLI. It spends one headless Claude run. The run is scored exactly as a NetBench run, into a
scratch results directory, and the twin is reset to golden afterwards.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import tempfile
import textwrap
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import typer

from netbench.admin import HttpAdmin, read_admin_token
from netbench.claude_cli import KILL_GRACE, STREAM_LINE_LIMIT, ClaudeCliRunner, kill_tree
from netbench.clients import HttpToolClient
from netbench.harness import Harness, RunConfig
from netbench.runner import RunContext, RunOutput
from nettwin_core.scenario import Scenario, load_scenarios

COLS = 100
PALETTE = {
    "bg": "#0d1117",
    "text": "#c9d1d9",
    "dim": "#8b949e",
    "bright": "#f0f6fc",
    "prompt": "#7ee787",
    "ok": "#3fb950",
    "bad": "#f85149",
    "warn": "#d29922",
    "node": "#a5d6ff",
    "commander": "#58a6ff",
    "l2-investigator": "#d2a8ff",
    "l3-investigator": "#ffa657",
    "policy-investigator": "#f778ba",
    "change-agent": "#e3b341",
    "verifier": "#39c5cf",
}
#: Plumbing calls that are not part of the incident.
SKIPPED = ("ToolSearch", "ListMcpResourcesTool")
AGENT_WIDTH = 21
#: Width of "  +m:ss  " plus the agent column: where a step's text starts.
STEP_COL = 9 + AGENT_WIDTH


class Recorder:
    """Prints styled lines to the terminal and keeps them, with time offsets, for the SVG."""

    def __init__(self) -> None:
        self.t0 = time.monotonic()
        self.events: list[dict[str, Any]] = []

    def line(self, *segments: tuple[str, str]) -> None:
        self.events.append(
            {"t": round(time.monotonic() - self.t0, 3), "segments": [list(s) for s in segments]}
        )
        sys.stdout.write("".join(_ansi(style, text) for style, text in segments) + "\n")
        sys.stdout.flush()

    def blank(self) -> None:
        self.line(("text", ""))


def _ansi(style: str, text: str) -> str:
    color = PALETTE.get(style, PALETTE["text"])
    r, g, b = (int(color[i : i + 2], 16) for i in (1, 3, 5))
    weight = "1;" if style in ("bright", "ok", "bad") else ""
    return f"\x1b[{weight}38;2;{r};{g};{b}m{text}\x1b[0m"


def clip(text: Any, width: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= width else text[: width - 3] + "..."


class LiveSteps:
    """Turns the CLI's stream-json lines into one printed row per agent step, as they arrive."""

    def __init__(self, rec: Recorder) -> None:
        self.rec = rec
        self.start = time.monotonic()
        self.agents: dict[str, str] = {}  # Agent tool_use id -> subagent type
        self.pending: dict[str, tuple[str, str, dict[str, Any]]] = {}

    def feed(self, line: str) -> None:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return
        if not isinstance(event, dict):
            return
        message = event.get("message")
        blocks = message.get("content") if isinstance(message, dict) else None
        if not isinstance(blocks, list):
            return
        parent = event.get("parent_tool_use_id")
        actor = self.agents.get(parent, "subagent") if parent else "commander"
        for block in blocks:
            if not isinstance(block, dict):
                continue
            if event.get("type") == "assistant" and block.get("type") == "tool_use":
                self.on_call(actor, block)
            elif event.get("type") == "user" and block.get("type") == "tool_result":
                self.on_result(block)

    def row(self, actor: str, *segments: tuple[str, str]) -> None:
        minutes, seconds = divmod(int(time.monotonic() - self.start), 60)
        self.rec.line(
            ("dim", f"  +{minutes}:{seconds:02d}  "),
            (actor if actor in PALETTE else "text", actor.ljust(AGENT_WIDTH)),
            *segments,
        )

    def more(self, style: str, text: str, max_lines: int = 2) -> None:
        """Continuation lines under the previous step's text, wrapped rather than clipped."""
        lines = textwrap.wrap(" ".join(text.split()), COLS - STEP_COL) or [""]
        if len(lines) > max_lines:
            lines = [
                *lines[: max_lines - 1],
                clip(" ".join(lines[max_lines - 1 :]), COLS - STEP_COL),
            ]
        for line in lines:
            self.rec.line(("dim", " " * STEP_COL), (style, line))

    def on_call(self, actor: str, block: dict[str, Any]) -> None:
        name = str(block.get("name", ""))
        tool = name.split("__")[-1]
        args = block.get("input") or {}
        room = COLS - STEP_COL
        if tool in SKIPPED:
            return
        if name == "Agent":
            agent = str(args.get("subagent_type", "subagent"))
            self.agents[str(block.get("id"))] = agent
            desc = clip(args.get("description", ""), room - len(agent) - 12)
            self.row(actor, ("dim", "launches "), (agent, agent), ("dim", f" · {desc}"))
        elif tool == "run_show_command":
            node = str(args.get("node", ""))
            self.row(actor, ("node", node.ljust(6)), ("text", clip(args.get("cmd", ""), room - 6)))
        elif tool == "ReadMcpResourceTool":
            self.row(actor, ("dim", "reads "), ("text", str(args.get("uri", ""))))
        else:
            self.pending[str(block.get("id"))] = (actor, tool, args)

    def on_result(self, block: dict[str, Any]) -> None:
        pending = self.pending.pop(str(block.get("tool_use_id")), None)
        if pending is None:
            return
        actor, tool, args = pending
        result = _result_value(block)
        res = result if isinstance(result, dict) else {}
        if tool == "snapshot":
            self.row(actor, ("text", "snapshot "), ("dim", str(res.get("id", ""))[:12]))
        elif tool == "apply_config":
            self.on_apply(actor, args, result)
        elif tool == "intent_check":
            rules = res.get("rules", [])
            if res.get("passed"):
                self.row(
                    actor,
                    ("text", "intent_check  "),
                    ("ok", f"passed {len(rules)}/{len(rules)} rules"),
                )
            else:
                failed = ", ".join(str(r.get("rule_id")) for r in rules if not r.get("passed"))
                self.row(actor, ("text", "intent_check  "), ("bad", f"failed {failed}"))
        elif tool == "reachability_matrix":
            probes = [p for p in res.get("probes", []) if isinstance(p, dict)]
            answered = sum(1 for p in probes if p.get("ok"))
            self.row(
                actor, ("text", f"reachability_matrix  {len(probes)} probes, {answered} answered")
            )
        elif tool == "wait_converged":
            state = "converged" if res.get("converged") else "not converged"
            self.row(actor, ("text", f"wait_converged  {state}"))
        elif tool == "route_diff":
            nodes = [n for n in (res.get("nodes") or {}).values() if isinstance(n, dict)]
            moved = sum(len(n.get("added", [])) + len(n.get("removed", [])) for n in nodes)
            text = "no route changes" if not moved else f"{moved} route changes"
            self.row(actor, ("text", f"route_diff before → after  {text}"))
        elif tool == "export_change":
            status = res.get("status") or clip(result, 40)
            who = res.get("decided_by") or "pending"
            style = "ok" if status in ("approved", "pending") else "bad"
            self.row(
                actor, ("bright", "export_change "), (style, f"→ {status}"), ("dim", f" ({who})")
            )
        elif tool == "rollback":
            self.row(actor, ("warn", "rollback "), ("dim", str(args.get("snapshot_id", ""))[:12]))
        else:
            self.row(actor, ("text", f"{tool} {clip(json.dumps(args), 50)}"))

    def on_apply(self, actor: str, args: dict[str, Any], result: Any) -> None:
        """The change itself: the diff twinlab applied, or what it refused and why."""
        room = COLS - STEP_COL
        head = ("bright", f"apply_config {args.get('node')}  ")
        ops = [op for op in args.get("ops", []) if isinstance(op, dict)]
        if isinstance(result, dict) and result.get("change_id"):
            self.row(
                actor,
                head,
                ("ok", "→ applied"),
                ("dim", f", change {str(result['change_id'])[:12]}"),
            )
            diff = str(result.get("diff", "")).splitlines()
            changed = [
                (("ok" if line[0] == "+" else "bad"), f"{line[0]} {line[1:].strip()}")
                for line in diff
                if line[:1] in "+-" and not line.startswith(("+++", "---"))
            ]
            for style, text in changed[:3] or [("text", _op_text(op)) for op in ops[:3]]:
                self.more(style, text)
        else:
            reason = re.search(r"Value error, ([^\[\n]+)", str(result))
            why = reason.group(1).strip() if reason else "invalid operation"
            self.row(actor, head, ("warn", clip(f"→ refused: {why}", room - len(head[1]))))
            for op in ops[:3]:
                self.more("dim", _op_text(op))


def _result_value(block: dict[str, Any]) -> Any:
    content = block.get("content")
    if isinstance(content, list):
        content = "".join(c.get("text", "") for c in content if isinstance(c, dict))
    if isinstance(content, str):
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            return content
    return content


def _op_text(op: dict[str, Any]) -> str:
    """One typed operation as the command it stands for, e.g. `nft add rule ip nat ...`."""
    if op.get("lines"):
        return " / ".join(str(line).strip() for line in op["lines"])
    if op.get("kind") == "nft_rule":
        parts = ("action", "family", "table", "chain", "rule")
        words = [str(op[k]) for k in parts if op.get(k) not in (None, "")]
        return "nft " + " ".join([words[0], "rule", *words[1:]] if words else [])
    keys = ("kind", "action", "iface", "mtu", "table", "chain", "rule")
    return " ".join(str(op[k]) for k in keys if op.get(k) not in (None, ""))


def live_spawn(on_line):
    """`spawn_cli` from netbench.claude_cli, with every stdout line also handed to `on_line`."""

    async def spawn(argv: list[str], stdin: str, cwd: Path, timeout: float):
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(cwd),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=STREAM_LINE_LIMIT,
        )
        lines: list[str] = []

        async def feed() -> None:
            assert proc.stdin is not None
            proc.stdin.write(stdin.encode("utf-8"))
            await proc.stdin.drain()
            proc.stdin.close()

        async def drain_stdout() -> None:
            assert proc.stdout is not None
            async for raw in proc.stdout:
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                lines.append(line)
                on_line(line)

        async def drain_stderr() -> str:
            assert proc.stderr is not None
            return (await proc.stderr.read()).decode("utf-8", "replace")

        work = asyncio.ensure_future(asyncio.gather(feed(), drain_stdout(), drain_stderr()))
        done, _ = await asyncio.wait({work}, timeout=timeout)
        if work in done:
            return await proc.wait(), lines, work.result()[2]
        await kill_tree(proc)
        await asyncio.wait({work}, timeout=KILL_GRACE)
        return -1, list(lines), f"killed after {timeout:.0f}s"

    return spawn


class DemoRunner:
    """The headless Claude runner, with the scenario's probe pinged before and after it."""

    def __init__(
        self, inner: ClaudeCliRunner, rec: Recorder, steps: LiveSteps, scenario: Scenario
    ) -> None:
        self.inner = inner
        self.rec = rec
        self.steps = steps
        self.probe = scenario.probe

    async def ping(self, ctx: RunContext, when: str) -> None:
        if self.probe is None:
            return
        cmd = re.sub(r"-c \d+", "-c 3", self.probe.cmd)
        got = await ctx.twin.call("run_show_command", {"node": self.probe.node, "cmd": cmd})
        data = got.data or {}
        stats = re.search(r"(\d+) packets transmitted, (\d+) received", data.get("stdout", ""))
        sent, back = (int(stats.group(1)), int(stats.group(2))) if stats else (0, 0)
        verdict = (
            ("ok", f"✓ {back}/{sent} replies") if back else ("bad", f"✗ {back}/{sent} replies")
        )
        self.rec.line(
            ("dim", f"  {when:<7}"),
            ("node", f"{self.probe.node} ▸ "),
            ("text", f"{cmd}   "),
            verdict,
        )

    async def run(self, ctx: RunContext) -> RunOutput:
        await self.ping(ctx, "before")
        self.rec.blank()
        self.rec.line(("bright", "  The agent team takes the ticket (claude -p /diagnose, Sonnet)"))
        self.steps.start = time.monotonic()
        output = await self.inner.run(ctx)
        self.rec.blank()
        await self.ping(ctx, "after")
        return output


def main(
    scenario: str = typer.Argument("019", help="Scenario id or prefix"),
    out: Path = typer.Option(Path("docs/media/demo-019.json"), help="Where the recording goes"),
    model: str = typer.Option("sonnet"),
    results: Path | None = typer.Option(None, help="Scratch results dir (default: a temp dir)"),
) -> None:
    sys.stdout.reconfigure(encoding="utf-8")  # a redirected Windows stdout is cp1252
    chosen = [s for s in load_scenarios(Path("lab/scenarios")) if s.id.startswith(scenario)]
    if len(chosen) != 1:
        raise typer.BadParameter(f"{scenario!r} matches {len(chosen)} scenarios")
    scen = chosen[0]
    rec = Recorder()
    rec.line(("prompt", "$ "), ("bright", f"uv run python scripts/record_demo.py {scenario}"))
    rec.blank()
    rec.line(
        ("bright", "  NetTwin "),
        ("dim", "· twin: 5 FRR routers, a VLAN switch, 4 hosts (containerlab on WSL2)"),
    )
    rec.line(("dim", "  planting fault "), ("warn", scen.id), ("dim", " through the admin route"))
    ticket = textwrap.wrap(" ".join(scen.symptom.split()), COLS - 12)
    for i, text in enumerate(ticket):
        rec.line(("dim", "  ticket: " if i == 0 else " " * 10), ("text", text))

    admin = HttpAdmin("http://localhost:8001", read_admin_token(None))
    scratch = results or Path(tempfile.mkdtemp(prefix="nettwin-demo-"))
    steps = LiveSteps(rec)
    inner = ClaudeCliRunner(
        admin,
        model=model,
        skill="diagnose",
        max_turns=60,
        timeout=1500.0,
        cwd=Path.cwd(),
        transcripts_dir=scratch / "transcripts",
        spawn=live_spawn(steps.feed),
    )
    runner = DemoRunner(inner, rec, steps, scen)
    config = RunConfig(name=f"demo-claude-diagnose-{model}", runner="claude", model=model)

    async def go() -> int:
        twin = HttpToolClient("http://localhost:8001/mcp", "twinlab")
        verify = HttpToolClient("http://localhost:8002/mcp", "netverify")
        await twin.connect()
        await verify.connect()
        harness = Harness(twin=twin, verify=verify, admin=admin, results_dir=scratch, matrix="demo")
        await harness.baseline()
        rec.line(("dim", "  golden snapshot "), ("text", (harness.golden_snapshot or "")[:12]))
        record = await harness.run_one(scen, config, 1, runner)
        score, output = record.score, record.output
        cause = output.root_cause
        truth = scen.ground_truth
        rec.blank()
        rec.line(
            ("bright", "  Scored by the harness from the twin's own state, not the agents' report")
        )
        checks = [
            (
                "root cause",
                score.root_cause,
                f"{cause.node} {cause.component}" if cause else "none reported",
                f"planted: {truth.node} {truth.component}" if truth else "planted: nothing",
            ),
            ("fix", score.fix_correct, "intent check after the run", "every rule passes again"),
            ("verified", score.verified, "isolated verifier", "judged before the export"),
            ("collateral", score.collateral_free, "reachability matrix", "nothing else broke"),
            (
                "minimal",
                score.minimal,
                f"{score.ops_applied} op(s)",
                f"on {', '.join(score.nodes_touched) or '-'}",
            ),
        ]
        for label, passed, what, note in checks:
            mark = ("ok", "✓") if passed else ("bad", "✗")
            rec.line(("dim", f"  {label:<12}"), mark, ("text", f"  {what:<28}"), ("dim", note))
        exported = (output.export or {}).get("status", "not exported")
        minutes, seconds = divmod(int(record.duration_s), 60)
        rec.line(
            ("dim", "  export      "),
            ("ok" if exported in ("approved", "pending") else "bad", str(exported)),
            ("dim", f"   ·   wall clock {minutes} min {seconds:02d} s   ·   twin reset to golden"),
        )
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(
                {
                    "version": 1,
                    "title": f"nettwin · live run · {scen.id}",
                    "scenario": scen.id,
                    "model": model,
                    "recorded_at": datetime.now(UTC).isoformat(timespec="seconds"),
                    "cols": COLS,
                    "palette": PALETTE,
                    "score": score.model_dump(mode="json"),
                    "events": rec.events,
                },
                indent=1,
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
            newline="\n",
        )
        print(f"recording saved to {out} ({len(rec.events)} lines); run record in {scratch}")
        # Closing a wedged streamable-HTTP session can hang (see netbench.cli): exit hard.
        sys.stdout.flush()
        os._exit(0 if record.error is None else 1)

    asyncio.run(go())


if __name__ == "__main__":
    typer.run(main)
