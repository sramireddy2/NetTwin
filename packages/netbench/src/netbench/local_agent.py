"""Local model runner: an Ollama model behind the same tools and role files as Claude Code.

With the Claude runner, Claude Code runs the skill and enforces each role's allowlist. Here
the harness does both itself: it turns the twinlab and netverify MCP tools into Ollama
function definitions, filters them through the same `.claude/agents` frontmatter, and drives
a plain chat loop with the skill body as the system prompt. Team mode replaces the Agent tool
with a synthetic `launch_agent` that runs a nested loop over that role's tools only. The point
is a fair comparison: a local model gets exactly what Sonnet gets, no more and no less.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Awaitable, Callable, Iterable
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from netbench.claude_cli import coerce_root_cause, extract_report
from netbench.clients import CallResult, ToolClient
from netbench.roles import MCP_PREFIX, Role, load_roles, load_skills
from netbench.runner import RunContext, RunnerUnavailable, RunOutput, collect_calls
from nettwin_core.models import VerificationReport

DEFAULT_OLLAMA = "http://localhost:11434"
DEFAULT_NUM_CTX = 16384
RESULT_CAP = 6000
READ_TOPOLOGY = "read_topology"
LAUNCH_AGENT = "launch_agent"
EXPORT_TOOL = f"{MCP_PREFIX}twinlab__export_change"
NETVERIFY_PREFIX = f"{MCP_PREFIX}netverify__"
#: What the incident commander may call itself in team mode; everything else goes through a
#: subagent, as in the Claude Code team.
COMMANDER_TOOLS = (
    READ_TOPOLOGY,
    f"{MCP_PREFIX}twinlab__snapshot",
    f"{MCP_PREFIX}twinlab__rollback",
    EXPORT_TOOL,
)
#: Server calls that legitimately take minutes on the real twin.
_TIMEOUTS = {"intent_check": 300.0, "rollback": 300.0}

SOLO_PREAMBLE = """\
You work inside the NetTwin digital twin through function calls. The twinlab and netverify
MCP tools are the functions named mcp__twinlab__<tool> and mcp__netverify__<tool>;
`read_topology` returns the lab://topology resource (use it wherever the instructions mention
ReadMcpResourceTool). Call functions instead of describing commands, and read every result
before the next call. Your final message must end with the JSON line the instructions ask
for, on its own line.
"""

TEAM_PREAMBLE = (
    SOLO_PREAMBLE
    + """
Subagents are launched with the `launch_agent(subagent_type, prompt)` function instead of
the Agent tool; it runs that agent to completion and returns its final report as text.
Several launches in one message run one after another.
"""
)

ROLE_PREAMBLE = """\
You are a subagent inside the NetTwin digital twin. The MCP tools you may use are the
functions named mcp__<server>__<tool>; call them instead of describing commands. When you
are done, answer with exactly the output your instructions ask for.
"""

Transport = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]
ToolHandler = Callable[[dict[str, Any]], Awaitable[str]]


class OllamaChat:
    """Non-streaming `POST /api/chat` with function calling; counts tokens across calls.

    `transport` takes the request payload and returns the parsed response body; the default
    posts it with httpx. Tests inject a scripted transport or an httpx mock client.
    """

    def __init__(
        self,
        model: str,
        *,
        base_url: str = DEFAULT_OLLAMA,
        num_ctx: int = DEFAULT_NUM_CTX,
        timeout: float = 900.0,
        client: httpx.AsyncClient | None = None,
        transport: Transport | None = None,
    ) -> None:
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.num_ctx = num_ctx
        self.timeout = timeout
        self.client = client
        self.transport = transport or self._post
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.requests = 0
        # qwen3's thinking is far too slow on CPU; None once the server rejected the field.
        self._think: bool | None = False

    def payload(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "tools": tools,
            "stream": False,
            "options": {"temperature": 0, "num_ctx": self.num_ctx},
        }
        if self._think is not None:
            body["think"] = self._think
        return body

    async def chat(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """One model turn: the assistant message, with `content` and maybe `tool_calls`."""
        body = await self.transport(self.payload(messages, tools))
        message = body.get("message") if isinstance(body, dict) else None
        if not isinstance(message, dict):
            raise RunnerUnavailable(f"Ollama answered without a message: {str(body)[:200]}")
        self.requests += 1
        self.prompt_tokens += int(body.get("prompt_eval_count") or 0)
        self.completion_tokens += int(body.get("eval_count") or 0)
        return message

    async def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.base_url}/api/chat"
        try:
            async with (
                httpx.AsyncClient(timeout=self.timeout)
                if self.client is None
                else nullcontext(self.client)
            ) as client:
                response = await client.post(url, json=payload)
        except httpx.HTTPError as exc:
            raise RunnerUnavailable(f"cannot reach Ollama at {self.base_url}: {exc}") from exc
        try:
            body = response.json()
        except ValueError as exc:
            raise RunnerUnavailable(
                f"Ollama at {self.base_url} did not answer JSON (HTTP {response.status_code}): "
                f"{response.text[:200]}"
            ) from exc
        if response.status_code == 404:
            raise RunnerUnavailable(
                f"Ollama has no model {self.model!r} ({_error_of(body)}); "
                f"run `ollama pull {self.model}`"
            )
        if response.status_code >= 400:
            error = _error_of(body)
            if "think" in payload and "think" in error.lower():
                self._think = None
                return await self._post({k: v for k, v in payload.items() if k != "think"})
            raise RunnerUnavailable(f"Ollama HTTP {response.status_code}: {error}")
        return body


def _error_of(body: Any) -> str:
    if isinstance(body, dict) and body.get("error"):
        return str(body["error"])
    return str(body)[:200]


@dataclass(frozen=True)
class BoundTool:
    """An Ollama function definition wired to the coroutine that executes it."""

    name: str
    description: str
    parameters: dict[str, Any]
    handler: ToolHandler

    @property
    def definition(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class Toolset:
    """The functions one loop may call; anything else comes back as an error message."""

    def __init__(self, tools: Iterable[BoundTool], *, result_cap: int = RESULT_CAP) -> None:
        self.tools = {t.name: t for t in tools}
        self.result_cap = result_cap

    @property
    def names(self) -> list[str]:
        return list(self.tools)

    @property
    def definitions(self) -> list[dict[str, Any]]:
        return [t.definition for t in self.tools.values()]

    async def dispatch(self, name: str, args: Any) -> str:
        tool = self.tools.get(name)
        if tool is None:
            return (
                f"error: {name!r} is not a tool this agent may call; "
                f"available: {', '.join(self.names)}"
            )
        if not isinstance(args, dict):
            return f"error: arguments for {name} must be a JSON object"
        return truncate(await tool.handler(args), self.result_cap)


def truncate(text: str, cap: int) -> str:
    if len(text) <= cap:
        return text
    return f"{text[:cap]}\n... [truncated {len(text) - cap} chars]"


def format_result(result: CallResult) -> str:
    text = result.text or (json.dumps(result.data) if result.data is not None else "")
    if not result.ok:
        return f"error: {text or 'tool call failed'}"
    return text or "(no output)"


async def mcp_tools(client: ToolClient) -> list[BoundTool]:
    """Every tool of one server, named `mcp__<server>__<tool>` as Claude Code names them."""
    return [
        BoundTool(
            name=f"{MCP_PREFIX}{client.name}__{tool.name}",
            description=tool.description or "",
            parameters=tool.input_schema,
            handler=_mcp_handler(client, tool.name),
        )
        for tool in await client.list_tools()
    ]


def _mcp_handler(client: ToolClient, tool: str) -> ToolHandler:
    async def handler(args: dict[str, Any]) -> str:
        return format_result(await client.call(tool, args, timeout=_TIMEOUTS.get(tool, 180.0)))

    return handler


def topology_tool(twin: ToolClient) -> BoundTool:
    """MCP resources are not tools; expose lab://topology as one so the skill's step 1 works."""

    async def handler(args: dict[str, Any]) -> str:
        return await twin.read_resource("lab://topology")

    return BoundTool(
        name=READ_TOPOLOGY,
        description=(
            "Return the twinlab resource lab://topology: nodes with roles, links with both "
            "interface names, subnets and recent snapshot ids. Use it wherever the "
            "instructions mention ReadMcpResourceTool."
        ),
        parameters={"type": "object", "properties": {}},
        handler=handler,
    )


def role_tools(role: Role, available: Iterable[BoundTool]) -> list[BoundTool]:
    """The role file's allowlist applied to what the servers offer."""
    allowed = set(role.tools) - set(role.disallowed_tools)
    return [t for t in available if t.name in allowed]


def without_verifier(tools: Iterable[BoundTool]) -> list[BoundTool]:
    """The ablation, enforced by the harness rather than trusted to the model."""
    return [t for t in tools if t.name != EXPORT_TOOL and not t.name.startswith(NETVERIFY_PREFIX)]


def parse_call(call: Any) -> tuple[str, Any]:
    """Ollama sends `{"function": {"name", "arguments": {...}}}`; some models send a string."""
    function = call.get("function") if isinstance(call, dict) else None
    if not isinstance(function, dict):
        return "", None
    name = str(function.get("name") or "")
    args = function.get("arguments")
    if isinstance(args, str):
        try:
            args = json.loads(args) if args.strip() else {}
        except json.JSONDecodeError:
            return name, args  # not an object: dispatch answers with an error message
    return name, {} if args is None else args


@dataclass
class LoopResult:
    final_text: str
    turns: int
    hit_max_turns: bool
    messages: list[dict[str, Any]]


class ToolLoop:
    """chat -> run the tool calls -> append their results, until the model answers in text."""

    def __init__(self, chat: OllamaChat, tools: Toolset, *, max_turns: int) -> None:
        self.chat = chat
        self.tools = tools
        self.max_turns = max_turns

    async def run(self, system: str, user: str) -> LoopResult:
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        text = ""
        for turn in range(1, self.max_turns + 1):
            message = await self.chat.chat(messages, self.tools.definitions)
            messages.append(message)
            text = str(message.get("content") or "")
            calls = message.get("tool_calls") or []
            if not calls:
                return LoopResult(text, turn, False, messages)
            for call in calls:
                name, args = parse_call(call)
                result = await self.tools.dispatch(name, args)
                messages.append({"role": "tool", "tool_name": name, "content": result})
        return LoopResult(text, self.max_turns, True, messages)


class LocalRunner:
    """Runs one scenario through an Ollama model with the skill body as its system prompt."""

    name = "local"

    def __init__(
        self,
        chat: OllamaChat,
        *,
        roles_dir: Path,
        skills_dir: Path,
        skill: str = "diagnose-solo",
        max_turns: int = 60,
        transcripts_dir: Path | None = None,
        result_cap: int = RESULT_CAP,
    ) -> None:
        self.chat = chat
        self.roles = load_roles(roles_dir)
        skills = load_skills(skills_dir)
        if skill not in skills:
            raise ValueError(f"unknown skill {skill!r}; have {sorted(skills)}")
        self.skill = skills[skill]
        self.max_turns = max_turns
        self.transcripts_dir = transcripts_dir
        self.result_cap = result_cap

    @property
    def team(self) -> bool:
        return self.skill.name == "diagnose"

    def arguments(self, ctx: RunContext) -> str:
        text = " ".join(ctx.symptom.split())
        return text if ctx.use_verifier else f"{text} --no-verifier"

    def system_prompt(self, ctx: RunContext) -> str:
        preamble = TEAM_PREAMBLE if self.team else SOLO_PREAMBLE
        return f"{preamble}\n{self.skill.body.replace('$ARGUMENTS', self.arguments(ctx))}"

    def user_prompt(self, ctx: RunContext) -> str:
        return f"/{self.skill.name} {self.arguments(ctx)}"

    async def run(self, ctx: RunContext) -> RunOutput:
        started = time.monotonic()
        tokens_before = (self.chat.prompt_tokens, self.chat.completion_tokens)
        available = [
            topology_tool(ctx.twin),
            *await mcp_tools(ctx.twin),
            *await mcp_tools(ctx.verify),
        ]
        if not ctx.use_verifier:
            available = without_verifier(available)
        launches: list[dict[str, Any]] = []
        if self.team:
            tools = [t for t in available if t.name in COMMANDER_TOOLS]
            tools.append(self.launch_tool(ctx, available, launches))
        else:
            tools = available
        loop = ToolLoop(
            self.chat, Toolset(tools, result_cap=self.result_cap), max_turns=self.max_turns
        )
        result = await loop.run(self.system_prompt(ctx), self.user_prompt(ctx))
        self.save_transcript(ctx, result, launches)
        return self.output(ctx, result, launches, tokens_before, time.monotonic() - started)

    def launch_tool(
        self, ctx: RunContext, available: list[BoundTool], launches: list[dict[str, Any]]
    ) -> BoundTool:
        """The Agent tool's stand-in: a nested loop over the role's own allowlist."""

        async def handler(args: dict[str, Any]) -> str:
            name = str(args.get("subagent_type") or "")
            prompt = str(args.get("prompt") or "")
            role = self.roles.get(name)
            if role is None:
                return (
                    f"error: unknown subagent_type {name!r}; "
                    f"available: {', '.join(sorted(self.roles))}"
                )
            if not ctx.use_verifier and "netverify" in role.servers:
                return (
                    f"error: {name} is disabled in this configuration (--no-verifier); "
                    "report the change unverified and do not export"
                )
            nested = ToolLoop(
                self.chat,
                Toolset(role_tools(role, available), result_cap=self.result_cap),
                max_turns=role.max_turns or self.max_turns,
            )
            result = await nested.run(f"{ROLE_PREAMBLE}\n{role.body}", prompt)
            launches.append(
                {
                    "subagent_type": name,
                    "prompt": prompt,
                    "turns": result.turns,
                    "hit_max_turns": result.hit_max_turns,
                    "messages": result.messages,
                }
            )
            text = result.final_text or "(the subagent returned no text)"
            if result.hit_max_turns:
                text += f"\n[{name} stopped after {result.turns} turns without a final answer]"
            return text

        return BoundTool(
            name=LAUNCH_AGENT,
            description=(
                "Launch one subagent from .claude/agents with the given prompt and return its "
                "final report. Stands in for the Agent tool."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "subagent_type": {
                        "type": "string",
                        "enum": sorted(self.roles),
                        "description": "The role to launch",
                    },
                    "prompt": {
                        "type": "string",
                        "description": "Everything the subagent needs; it sees nothing else",
                    },
                },
                "required": ["subagent_type", "prompt"],
            },
            handler=handler,
        )

    def save_transcript(
        self, ctx: RunContext, result: LoopResult, launches: list[dict[str, Any]]
    ) -> None:
        if self.transcripts_dir is None:
            return
        self.transcripts_dir.mkdir(parents=True, exist_ok=True)
        label = f"{ctx.scenario_id}.{ctx.config_name or self.name}.{ctx.trial}"
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", label)
        transcript = {
            "scenario_id": ctx.scenario_id,
            "config": ctx.config_name,
            "trial": ctx.trial,
            "model": self.chat.model,
            "skill": self.skill.name,
            "turns": result.turns,
            "hit_max_turns": result.hit_max_turns,
            "messages": result.messages,
            "subagents": launches,
        }
        (self.transcripts_dir / f"{safe}.json").write_text(
            json.dumps(transcript, indent=1, default=str), encoding="utf-8"
        )

    def output(
        self,
        ctx: RunContext,
        result: LoopResult,
        launches: list[dict[str, Any]],
        tokens_before: tuple[int, int],
        wall: float,
    ) -> RunOutput:
        """What the scorer reads comes from the recorded tool calls, not the model's claims."""
        report = extract_report(result.final_text)
        change_ids = [
            str(c.data["change_id"])
            for c in ctx.twin.calls
            if c.tool == "apply_config" and c.ok and c.data
        ]
        checks = [c for c in ctx.verify.calls if c.tool == "intent_check" and c.ok and c.data]
        verification = VerificationReport.model_validate(checks[-1].data) if checks else None
        exports = [c for c in ctx.twin.calls if c.tool == "export_change"]
        export: dict[str, Any] | None = None
        if exports:
            export = exports[-1].data if exports[-1].ok else {"error": exports[-1].text}
        root_cause = coerce_root_cause((report or {}).get("root_cause"))
        if root_cause is None and exports:
            root_cause = coerce_root_cause(exports[-1].args.get("root_cause"))

        notes = (
            f"ollama {self.chat.model} /{self.skill.name} turns={result.turns} "
            f"agents={len(launches)} wall={wall:.0f}s"
        )
        if launches:
            notes += " launched=" + ",".join(str(launch["subagent_type"]) for launch in launches)
        if result.hit_max_turns:
            notes += " max_turns_hit"
        if report is None:
            notes += " no_final_json"
        return RunOutput(
            root_cause=root_cause,
            change_ids=change_ids,
            verification=verification,
            export=export,
            tool_calls=collect_calls(ctx.twin, ctx.verify),
            input_tokens=self.chat.prompt_tokens - tokens_before[0],
            output_tokens=self.chat.completion_tokens - tokens_before[1],
            notes=notes,
        )
