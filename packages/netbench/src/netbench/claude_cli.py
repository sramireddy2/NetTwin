"""Headless Claude Code runner: `claude -p "/diagnose <symptom>"` with stream-json output.

The agent team, the skills, the role allowlists and the MCP servers all come from the
repository's `.claude/` and `.mcp.json`, exactly as in an interactive session. This module
launches the CLI as a subprocess with the prompt on stdin, records every stream event to a
transcript, parses tool calls, usage and cost, and turns the run into a `RunOutput` the
harness can score. The export bundle (read through the admin route, as the manual runner
does) is the source of truth for change ids and the verification report; the skill's final
JSON line is the fallback when nothing was exported.
"""

from __future__ import annotations

import asyncio
import json
import os
import platform
import re
import shutil
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from netbench.admin import AdminClient
from netbench.runner import RunContext, RunOutput, ToolCall
from nettwin_core.models import RootCause, VerificationReport

#: Tools the headless commander may use without a permission prompt. Bare MCP server names
#: allow every tool of that server; the role files still narrow each subagent.
DEFAULT_ALLOWED_TOOLS = (
    "mcp__twinlab",
    "mcp__netverify",
    "Agent",
    "ReadMcpResourceTool",
    "ListMcpResourcesTool",
)
STREAM_LINE_LIMIT = 16 * 1024 * 1024
_REPORT_RE = re.compile(r"\{\s*\"root_cause\"\s*:.*\}", re.DOTALL)

Spawn = Callable[[list[str], str, Path, float], Awaitable[tuple[int, list[str], str]]]


def find_claude(explicit: str | None = None) -> str:
    """Path of the Claude Code CLI: explicit, `NETTWIN_CLAUDE`, PATH, or the npm folder."""
    for candidate in (explicit, os.environ.get("NETTWIN_CLAUDE")):
        if candidate:
            return candidate
    if platform.system() == "Windows":
        npm = Path(os.environ.get("APPDATA", "")) / "npm"
        exe = npm / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe"
        if exe.exists():
            return str(exe)  # the real binary; skips the cmd.exe shim and its quoting
    found = shutil.which("claude")
    if found:
        return found
    raise FileNotFoundError(
        "claude CLI not found; install @anthropic-ai/claude-code or set NETTWIN_CLAUDE"
    )


class ToolUse(BaseModel):
    id: str
    name: str
    parent_tool_use_id: str | None = None
    input: dict[str, Any] = Field(default_factory=dict)

    @property
    def in_subagent(self) -> bool:
        return self.parent_tool_use_id is not None


class StreamSummary(BaseModel):
    """What the harness keeps from a stream-json transcript."""

    session_id: str | None = None
    model: str | None = None
    subtype: str | None = None
    is_error: bool = False
    error: str | None = None
    num_turns: int = 0
    duration_ms: int = 0
    total_cost_usd: float | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_creation_tokens: int | None = None
    final_text: str = ""
    tool_uses: list[ToolUse] = Field(default_factory=list)
    tool_results: dict[str, bool] = Field(default_factory=dict)
    agents_launched: list[str] = Field(default_factory=list)
    events: int = 0
    skipped_lines: int = 0

    @property
    def tool_calls(self) -> list[ToolCall]:
        calls: list[ToolCall] = []
        for use in self.tool_uses:
            server, tool = split_tool_name(use.name)
            calls.append(
                ToolCall(
                    server=server,
                    tool=tool,
                    ok=self.tool_results.get(use.id, True),
                    duration_ms=0,
                )
            )
        return calls


def split_tool_name(name: str) -> tuple[str, str]:
    """`mcp__twinlab__snapshot` -> (twinlab, snapshot); builtin tools -> (claude, name)."""
    if name.startswith("mcp__"):
        rest = name[len("mcp__") :]
        server, sep, tool = rest.partition("__")
        if sep:
            return server, tool
    return "claude", name


def parse_stream(lines: Iterable[str]) -> StreamSummary:
    summary = StreamSummary()
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            summary.skipped_lines += 1
            continue
        if not isinstance(event, dict):
            summary.skipped_lines += 1
            continue
        summary.events += 1
        kind = event.get("type")
        if kind == "system" and event.get("subtype") == "init":
            summary.session_id = event.get("session_id")
            summary.model = event.get("model")
        elif kind == "assistant":
            if event.get("error"):
                summary.is_error = True
                summary.error = str(event["error"])
            message = event.get("message") or {}
            parent = event.get("parent_tool_use_id")
            for block in message.get("content") or []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use":
                    use = ToolUse(
                        id=str(block.get("id", "")),
                        name=str(block.get("name", "")),
                        parent_tool_use_id=parent,
                        input=block.get("input") or {},
                    )
                    summary.tool_uses.append(use)
                    if use.name == "Agent" and use.input.get("subagent_type"):
                        summary.agents_launched.append(str(use.input["subagent_type"]))
                elif block.get("type") == "text" and parent is None and block.get("text"):
                    summary.final_text = str(block["text"])
        elif kind == "user":
            message = event.get("message") or {}
            content = message.get("content")
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        summary.tool_results[str(block.get("tool_use_id", ""))] = not bool(
                            block.get("is_error", False)
                        )
        elif kind == "result":
            summary.subtype = event.get("subtype")
            summary.is_error = summary.is_error or bool(event.get("is_error"))
            summary.num_turns = int(event.get("num_turns") or 0)
            summary.duration_ms = int(event.get("duration_ms") or 0)
            summary.total_cost_usd = event.get("total_cost_usd")
            usage = event.get("usage") or {}
            summary.input_tokens = usage.get("input_tokens")
            summary.output_tokens = usage.get("output_tokens")
            summary.cache_read_tokens = usage.get("cache_read_input_tokens")
            summary.cache_creation_tokens = usage.get("cache_creation_input_tokens")
            if isinstance(event.get("result"), str) and event["result"].strip():
                summary.final_text = event["result"]
            if event.get("is_error") and not summary.error:
                summary.error = str(event.get("result") or event.get("subtype") or "error")
    return summary


def extract_report(text: str) -> dict[str, Any] | None:
    """The skill ends with one JSON object holding root_cause and change_ids; find it."""
    best: dict[str, Any] | None = None
    for line in text.splitlines():
        candidate = line.strip().strip("`")
        if '"root_cause"' not in candidate:
            continue
        match = _REPORT_RE.search(candidate)
        if not match:
            continue
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            best = parsed
    return best


async def spawn_cli(
    argv: list[str], stdin: str, cwd: Path, timeout: float
) -> tuple[int, list[str], str]:
    """Run the CLI, feed the prompt on stdin, collect stdout lines; kill it on timeout."""
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
            lines.append(raw.decode("utf-8", "replace").rstrip("\r\n"))

    async def drain_stderr() -> str:
        assert proc.stderr is not None
        return (await proc.stderr.read()).decode("utf-8", "replace")

    try:
        _, _, stderr = await asyncio.wait_for(
            asyncio.gather(feed(), drain_stdout(), drain_stderr()), timeout
        )
        rc = await proc.wait()
    except TimeoutError:
        proc.kill()
        await proc.wait()
        rc, stderr = -1, f"killed after {timeout:.0f}s"
    return rc, lines, stderr


@dataclass
class ClaudeCliRunner:
    """Runs one scenario through `claude -p /<skill> <symptom>` and scores the outcome."""

    admin: AdminClient
    model: str = "sonnet"
    skill: str = "diagnose"
    max_turns: int = 60
    timeout: float = 1500.0
    cwd: Path = field(default_factory=Path.cwd)
    transcripts_dir: Path | None = None
    cli: str | None = None
    allowed_tools: tuple[str, ...] = DEFAULT_ALLOWED_TOOLS
    spawn: Spawn | None = None
    name: str = "claude"

    def prompt(self, symptom: str, use_verifier: bool) -> str:
        text = f"/{self.skill} {' '.join(symptom.split())}"
        return text if use_verifier else f"{text} --no-verifier"

    def argv(self) -> list[str]:
        return [
            find_claude(self.cli),
            "-p",
            "--output-format",
            "stream-json",
            "--verbose",
            "--model",
            self.model,
            "--mcp-config",
            ".mcp.json",
            "--strict-mcp-config",
            "--allowedTools",
            ",".join(self.allowed_tools),
            "--max-turns",
            str(self.max_turns),
        ]

    async def run(self, ctx: RunContext) -> RunOutput:
        seen = {e["export_id"] for e in await self.admin.exports()}
        prompt = self.prompt(ctx.symptom, ctx.use_verifier)
        started = time.monotonic()
        rc, lines, stderr = await (self.spawn or spawn_cli)(
            self.argv(), prompt, self.cwd, self.timeout
        )
        wall = time.monotonic() - started
        summary = parse_stream(lines)
        if self.transcripts_dir is not None:
            self.transcripts_dir.mkdir(parents=True, exist_ok=True)
            safe = re.sub(r"[^A-Za-z0-9._-]+", "_", ctx.scenario_id)
            path = self.transcripts_dir / f"{safe}.jsonl"
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        report = extract_report(summary.final_text)

        new = [e for e in await self.admin.exports() if e["export_id"] not in seen]
        bundle = await self.admin.export(new[-1]["export_id"]) if new else None
        root_cause = _root_cause_from(bundle, report)
        if bundle is not None:
            change_ids = [c["change_id"] for c in bundle["changes"]]
            verification: VerificationReport | None = VerificationReport.model_validate(
                bundle["verification"]
            )
            export: dict[str, Any] | None = {
                "export_id": bundle["export_id"],
                "status": bundle["status"],
                "decided_by": bundle.get("decided_by"),
            }
        else:
            change_ids = [str(c) for c in (report or {}).get("change_ids") or []]
            verification, export = None, None

        notes = (
            f"claude -p /{self.skill} model={summary.model or self.model} "
            f"turns={summary.num_turns} agents={len(summary.agents_launched)} rc={rc} "
            f"subtype={summary.subtype} wall={wall:.0f}s"
        )
        if summary.error:
            notes += f" error={summary.error}"
        if rc == -1:
            notes += " timed_out"
        elif rc != 0 and stderr.strip():
            notes += f" stderr={stderr.strip()[:300]}"
        return RunOutput(
            root_cause=root_cause,
            change_ids=change_ids,
            verification=verification,
            export=export,
            tool_calls=summary.tool_calls,
            input_tokens=summary.input_tokens,
            output_tokens=summary.output_tokens,
            cost_usd=summary.total_cost_usd,
            notes=notes,
        )


def _root_cause_from(
    bundle: dict[str, Any] | None, report: dict[str, Any] | None
) -> RootCause | None:
    candidates = []
    if bundle is not None:
        candidates.append(bundle.get("root_cause"))
    if report is not None:
        candidates.append(report.get("root_cause"))
    for candidate in candidates:
        if isinstance(candidate, dict):
            try:
                return RootCause.model_validate(candidate)
            except ValidationError:
                continue
    return None
