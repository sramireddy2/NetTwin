"""Render a Claude Code stream-json transcript as an incident timeline.

The stream interleaves the commander and every subagent; each subagent event carries the
id of the Agent call that launched it (`parent_tool_use_id`). Pairing tool calls with their
results and attributing both to the right agent is enough to replay an incident step by
step: who read what, who changed what, what the verifier saw, what was exported.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from netbench.claude_cli import extract_report

#: Calls that are plumbing, not part of the incident.
SKIPPED = ("ToolSearch", "ListMcpResourcesTool")


@dataclass
class Step:
    at: float  # seconds since the first event
    actor: str
    tool: str
    args: dict[str, Any]
    ok: bool | None = None
    result: Any = None
    nodes: list[str] = field(default_factory=list)
    count: int = 1


@dataclass
class Timeline:
    steps: list[Step]
    report: dict[str, Any] | None
    duration: float


def _ts(value: str | None) -> float | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


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


def build_timeline(lines: Iterable[str]) -> Timeline:
    agents: dict[str, str] = {}  # Agent tool_use id -> subagent type
    pending: dict[str, Step] = {}
    steps: list[Step] = []
    start: float | None = None
    last = 0.0
    report: dict[str, Any] | None = None
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        ts = _ts(event.get("timestamp"))
        if ts is not None:
            start = ts if start is None else start
            last = ts - start
        if event.get("type") == "result":
            report = extract_report(str(event.get("result") or "")) or report
            continue
        message = event.get("message") or {}
        blocks = message.get("content") if isinstance(message, dict) else None
        if not isinstance(blocks, list):
            continue
        parent = event.get("parent_tool_use_id")
        actor = agents.get(parent, "subagent") if parent else "commander"
        for block in blocks:
            if not isinstance(block, dict):
                continue
            if event.get("type") == "assistant" and block.get("type") == "tool_use":
                name = str(block.get("name", ""))
                args = block.get("input") or {}
                if name == "Agent":
                    agents[block["id"]] = str(args.get("subagent_type", "subagent"))
                step = Step(last, actor, name.split("__")[-1], args)
                pending[block["id"]] = step
                steps.append(step)
            elif event.get("type") == "user" and block.get("type") == "tool_result":
                step = pending.pop(block.get("tool_use_id"), None)
                if step is not None:
                    step.ok = not block.get("is_error", False)
                    step.result = _result_value(block)
    return Timeline(_collapse(steps), report, last)


def _collapse(steps: list[Step]) -> list[Step]:
    """Runs of show commands by one agent become one row: they are the reading, not the plot."""
    out: list[Step] = []
    for step in steps:
        if step.tool in SKIPPED:
            continue
        node = step.args.get("node")
        prev = out[-1] if out else None
        if (
            step.tool == "run_show_command"
            and prev is not None
            and prev.tool == "run_show_command"
            and prev.actor == step.actor
        ):
            prev.count += 1
            if node and str(node) not in prev.nodes:
                prev.nodes.append(str(node))
            continue
        if step.tool == "run_show_command" and node:
            step.nodes = [str(node)]
        out.append(step)
    return out


def _clip(text: Any, width: int = 110) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= width else text[: width - 3] + "..."


def _op_text(op: dict[str, Any]) -> str:
    if op.get("lines"):
        return " / ".join(str(line).strip() for line in op["lines"])
    keys = ("kind", "action", "iface", "mtu", "family", "table", "chain", "rule")
    return " ".join(str(op[k]) for k in keys if op.get(k) not in (None, ""))


def describe(step: Step) -> str:
    args, res = step.args, step.result
    if step.tool == "Agent":
        return f"launches **{args.get('subagent_type')}**: {_clip(args.get('description', ''), 70)}"
    if step.tool == "run_show_command":
        if step.count > 1:
            return f"{step.count} show commands on {', '.join(step.nodes)}"
        return f"show on {', '.join(step.nodes)}: `{_clip(args.get('cmd', ''), 50)}`"
    if step.tool == "ReadMcpResourceTool":
        return f"reads `{args.get('uri')}`"
    if step.tool == "snapshot":
        sid = res.get("id", "") if isinstance(res, dict) else ""
        return f"snapshot `{str(sid)[:12]}`"
    if step.tool == "apply_config":
        ops = "; ".join(_op_text(op) for op in args.get("ops", []) if isinstance(op, dict))
        if isinstance(res, dict) and res.get("change_id"):
            outcome = f"change `{res['change_id']}`"
        else:
            outcome = f"refused: {_clip(res, 60)}"
        return f"apply_config on {args.get('node')}: `{_clip(ops, 80)}` -> {outcome}"
    if step.tool == "intent_check" and isinstance(res, dict):
        rules = res.get("rules", [])
        failed = [str(r.get("rule_id")) for r in rules if not r.get("passed")]
        if res.get("passed"):
            return f"intent_check: **passed** {len(rules)}/{len(rules)} rules"
        return f"intent_check: **failed** {', '.join(failed)}"
    if step.tool == "export_change":
        if isinstance(res, dict):
            return f"export_change -> {res.get('status')} ({res.get('decided_by') or 'pending'})"
        return f"export_change -> {_clip(res, 60)}"
    if step.tool == "wait_converged" and isinstance(res, dict):
        state = "converged" if res.get("converged") else "not converged"
        return f"wait_converged: {state}"
    if step.tool == "route_diff" and isinstance(res, dict):
        nodes = [n for n in (res.get("nodes") or {}).values() if isinstance(n, dict)]
        added = sum(len(n.get("added", [])) for n in nodes)
        removed = sum(len(n.get("removed", [])) for n in nodes)
        if not (added or removed):
            return "route_diff S0 to S1: no route changes"
        return (
            f"route_diff S0 to S1: +{added} / -{removed} routes "
            f"on {res.get('changed_nodes', 0)} nodes"
        )
    if step.tool == "rollback":
        return f"rollback to `{str(args.get('snapshot_id', ''))[:12]}`"
    return f"{step.tool} {_clip(json.dumps(args), 60)}"


def render_markdown(timeline: Timeline, title: str) -> str:
    rows = [
        f"| +{int(s.at // 60)}:{int(s.at % 60):02d} | {s.actor} | {describe(s)} |"
        for s in timeline.steps
    ]
    parts = [f"### {title}", "", "| Time | Agent | Step |", "|---|---|---|", *rows, ""]
    rc = (timeline.report or {}).get("root_cause") or {}
    if rc:
        parts.append(
            f"Reported root cause: **{rc.get('node')} {rc.get('component')}**. "
            f"{_clip(rc.get('summary', ''), 300)}"
        )
        parts.append("")
    minutes, seconds = divmod(int(timeline.duration), 60)
    parts.append(f"Wall clock {minutes} min {seconds:02d} s.")
    return "\n".join(parts) + "\n"
