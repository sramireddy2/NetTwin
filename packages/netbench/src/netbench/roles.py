"""Read the Claude Code role files in `.claude/agents` as data.

Claude Code enforces each subagent's tool allowlist from the YAML frontmatter of these
files. The benchmark reads the same files so a test can prove the allowlists match the
servers, and so the local runner (M10) can give a local model exactly the tools each role
has, from one source of truth.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

MCP_PREFIX = "mcp__"


@dataclass(frozen=True)
class Role:
    name: str
    description: str
    model: str
    tools: tuple[str, ...]
    disallowed_tools: tuple[str, ...]
    mcp_servers: tuple[str, ...]
    max_turns: int | None
    body: str
    path: Path

    @property
    def mcp_tools(self) -> tuple[str, ...]:
        return tuple(t for t in self.tools if t.startswith(MCP_PREFIX))

    @property
    def builtin_tools(self) -> tuple[str, ...]:
        return tuple(t for t in self.tools if not t.startswith(MCP_PREFIX))

    @property
    def servers(self) -> set[str]:
        """Server names this role's MCP tools belong to."""
        return {server_of(t) for t in self.mcp_tools}


@dataclass(frozen=True)
class SkillInfo:
    name: str
    description: str
    body: str
    path: Path


def server_of(tool: str) -> str:
    """`mcp__twinlab__snapshot` -> `twinlab`."""
    if not tool.startswith(MCP_PREFIX):
        raise ValueError(f"not an MCP tool name: {tool}")
    rest = tool[len(MCP_PREFIX) :]
    server, sep, _ = rest.partition("__")
    if not sep:
        raise ValueError(f"MCP tool name has no tool part: {tool}")
    return server


def split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    if not text.startswith("---"):
        raise ValueError("file does not start with a YAML frontmatter block")
    parts = text.split("---", 2)
    if len(parts) < 3:
        raise ValueError("frontmatter block is not closed")
    meta = yaml.safe_load(parts[1]) or {}
    if not isinstance(meta, dict):
        raise ValueError("frontmatter is not a mapping")
    return meta, parts[2].lstrip()


def as_list(value: Any) -> tuple[str, ...]:
    """Claude Code accepts a comma-separated string or a YAML list; normalise to a tuple."""
    if value is None:
        return ()
    if isinstance(value, str):
        return tuple(v.strip() for v in value.split(",") if v.strip())
    return tuple(str(v).strip() for v in value if str(v).strip())


def parse_role(path: Path) -> Role:
    meta, body = split_frontmatter(path.read_text(encoding="utf-8"))
    name = str(meta.get("name") or path.stem)
    max_turns = meta.get("maxTurns")
    return Role(
        name=name,
        description=str(meta.get("description", "")),
        model=str(meta.get("model", "inherit")),
        tools=as_list(meta.get("tools")),
        disallowed_tools=as_list(meta.get("disallowedTools")),
        mcp_servers=as_list(meta.get("mcpServers")),
        max_turns=int(max_turns) if max_turns is not None else None,
        body=body,
        path=path,
    )


def load_roles(directory: Path) -> dict[str, Role]:
    roles = [parse_role(p) for p in sorted(directory.glob("*.md"))]
    return {r.name: r for r in roles}


def parse_skill(path: Path) -> SkillInfo:
    meta, body = split_frontmatter(path.read_text(encoding="utf-8"))
    return SkillInfo(
        name=str(meta.get("name") or path.parent.name),
        description=str(meta.get("description", "")),
        body=body,
        path=path,
    )


def load_skills(directory: Path) -> dict[str, SkillInfo]:
    skills = [parse_skill(p) for p in sorted(directory.glob("*/SKILL.md"))]
    return {s.name: s for s in skills}
