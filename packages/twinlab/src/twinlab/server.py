"""twinlab MCP server: the only door into the digital twin.

Tools are thin wrappers over :class:`twinlab.app.TwinLab`. Anything an agent can do to the
twin goes through here, is validated by pydantic, and is executed as argv, never a shell.
Fault injection and golden reset are admin HTTP routes with a bearer token, not tools.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from nettwin_core.models import ChangeResult
from nettwin_core.ops import Op
from nettwin_core.settings import Settings
from twinlab.admin import load_or_create_token, register_admin_routes
from twinlab.allowlist import EXAMPLES, CommandNotAllowed
from twinlab.app import TwinLab, UnknownNode
from twinlab.apply import ApplyError
from twinlab.snapshot import SnapshotError

log = logging.getLogger("twinlab")

INSTRUCTIONS = """\
twinlab controls a containerlab digital twin of the network (FRRouting routers, a VLAN
switch and Linux hosts). Read the lab://topology resource first to learn node names, roles
and links. Use run_show_command for read-only inspection on any node. Take a snapshot before
changing anything. Change the twin only through apply_config with typed operations; every
apply is bracketed by snapshots and returns the resulting diff. Roll back to a snapshot if a
change does not help. Nothing here touches a production device.
"""

SHOW_DESCRIPTION = (
    "Run a read-only command on one node of the twin and return its output. FRR commands "
    "start with 'show' and run in vtysh (show ip route, show ip ospf neighbor, show bgp "
    "summary, show running-config, ...). Kernel commands are limited to ip/bridge/nft reads, "
    "sysctl net.ipv4.*, ping (-c required, max 5) and traceroute. Prefer filtered forms such "
    "as 'show ip route 10.0.40.0/24' to keep output short. Examples: " + "; ".join(EXAMPLES)
)

APPLY_DESCRIPTION = (
    "Apply a list of typed configuration operations to ONE node of the twin. Op kinds: "
    "frr_lines {lines: [...]} (FRR configure-mode lines in order, e.g. ['interface eth1', "
    "' no ip ospf area 0.0.0.0', ' ip ospf area 0.0.0.1']; FRR requires 'no ip ospf area X' "
    "before setting a different area), set_mtu {iface, mtu}, set_addr {iface, address "
    "with prefix length, replace}, add_vlan {parent, vlan_id, name?}, del_vlan {name}, "
    "bridge_vlan {action add|del, dev, vlan_id, pvid, untagged}, nft_rule {action "
    "add|insert|delete|flush_chain, family, table, chain, rule?, handle?, index?}. The server "
    "snapshots before and after, rolls back automatically if any op fails, and returns the "
    "change id plus the running-config and kernel diff it produced."
)


class ShowResult(BaseModel):
    node: str
    command: str
    returncode: int
    stdout: str
    stderr: str
    truncated: bool
    duration_ms: int


class SnapshotInfo(BaseModel):
    id: str
    short_id: str
    created_at: datetime
    nodes: list[str]


class SnapshotList(BaseModel):
    snapshots: list[str] = Field(description="Snapshot ids, oldest first")


class ChangeList(BaseModel):
    changes: list[str] = Field(description="Change ids, oldest first")


class RollbackResult(BaseModel):
    restored: str
    before: str
    after: str
    matches_target: bool
    changed_nodes: dict[str, list[list[str]]] = Field(
        description="Commands run per node to get back to the snapshot"
    )


def build_server(app: TwinLab, *, admin_token: str | None = None) -> MCPServer:
    server = MCPServer("twinlab", instructions=INSTRUCTIONS)

    @server.resource(
        "lab://topology",
        name="topology",
        description="Nodes, roles and links of the twin, plus the most recent snapshot ids.",
        mime_type="application/json",
    )
    def topology() -> str:
        return json.dumps(app.topology_resource(), indent=1)

    @server.tool(
        name="run_show_command",
        description=SHOW_DESCRIPTION,
        annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True),
    )
    async def run_show_command(node: str, cmd: str) -> ShowResult:
        try:
            res = await app.show(node, cmd)
        except (UnknownNode, CommandNotAllowed) as exc:
            raise ToolError(str(exc)) from exc
        return ShowResult(
            node=res.node,
            command=" ".join(res.argv),
            returncode=res.returncode,
            stdout=res.stdout,
            stderr=res.stderr,
            truncated=res.truncated,
            duration_ms=res.duration_ms,
        )

    @server.tool(
        name="snapshot",
        description=(
            "Capture the whole twin (FRR running-config and kernel network state of every "
            "node) into a content-addressed snapshot and return its id. Take one before any "
            "change so you can roll back."
        ),
        annotations=ToolAnnotations(read_only_hint=True),
    )
    async def snapshot() -> SnapshotInfo:
        try:
            snap = await app.snapshot()
        except SnapshotError as exc:
            raise ToolError(str(exc)) from exc
        return SnapshotInfo(
            id=snap.id, short_id=snap.short_id, created_at=snap.created_at, nodes=sorted(snap.nodes)
        )

    @server.tool(
        name="list_snapshots",
        description="List stored snapshot ids, oldest first.",
        annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True),
    )
    async def list_snapshots() -> SnapshotList:
        return SnapshotList(snapshots=app.store.ids())

    @server.tool(
        name="rollback",
        description=(
            "Restore the twin to a stored snapshot (full id or a prefix of at least 8 "
            "characters). Diffs current state against the snapshot and applies only what "
            "differs: FRR config via frr-reload, then addresses, MTU, VLANs, bridge ports "
            "and nftables. Returns the commands run per node."
        ),
        annotations=ToolAnnotations(destructive_hint=True, idempotent_hint=True),
    )
    async def rollback(snapshot_id: str) -> RollbackResult:
        try:
            result = await app.rollback(snapshot_id)
        except KeyError as exc:
            raise ToolError(f"unknown snapshot: {exc}") from exc
        except SnapshotError as exc:
            raise ToolError(str(exc)) from exc
        return RollbackResult(**result)

    @server.tool(
        name="apply_config",
        description=APPLY_DESCRIPTION,
        annotations=ToolAnnotations(destructive_hint=True),
    )
    async def apply_config(node: str, ops: list[Op], rationale: str = "") -> ChangeResult:
        try:
            return await app.apply(node, ops, rationale)
        except (UnknownNode, ValueError) as exc:
            raise ToolError(str(exc)) from exc
        except ApplyError as exc:
            raise ToolError(str(exc)) from exc
        except SnapshotError as exc:
            raise ToolError(str(exc)) from exc

    @server.tool(
        name="list_changes",
        description="List applied change ids, oldest first.",
        annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True),
    )
    async def list_changes() -> ChangeList:
        return ChangeList(changes=app.changes.ids())

    if admin_token:
        register_admin_routes(server, app, admin_token)
    return server


def transport_security() -> TransportSecuritySettings:
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=["localhost:*", "127.0.0.1:*", "[::1]:*"],
        allowed_origins=["http://localhost:*", "http://127.0.0.1:*"],
    )


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    settings = Settings.from_env()
    app = TwinLab.from_settings(settings)
    token = settings.admin_token or load_or_create_token(settings.admin_token_path)
    log.info(
        "twinlab: lab=%s topology=%s nodes=%d state=%s port=%d admin_token=%s",
        settings.lab_name,
        settings.topology_path,
        len(app.topology.nodes),
        settings.state_dir,
        settings.twinlab_port,
        settings.admin_token_path,
    )
    build_server(app, admin_token=token).run(
        "streamable-http",
        host="0.0.0.0",
        port=settings.twinlab_port,
        transport_security=transport_security(),
    )


if __name__ == "__main__":
    main()
