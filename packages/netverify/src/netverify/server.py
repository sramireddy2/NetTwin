"""netverify MCP server: judgement over the twin, kept apart from the server that changes it."""

from __future__ import annotations

import logging

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from nettwin_core.models import VerificationReport
from nettwin_core.settings import Settings
from netverify.app import NetVerify, RouteDiff
from netverify.converge import ConvergeResult
from netverify.executor import ReadOnlyViolation
from netverify.probes import ProbeResult

log = logging.getLogger("netverify")

INSTRUCTIONS = """\
netverify judges the digital twin against a declarative intent policy. It has no write
tools and its executor refuses anything but read-only commands. Typical use: call
wait_converged, then intent_check; use route_diff to compare two snapshot ids. A passing
intent_check carries a signed attestation that twinlab requires before it exports a change.
Read policy://intent to see the rules being enforced.
"""


class ReachabilityResult(BaseModel):
    probes: list[ProbeResult]
    ok: int = Field(description="Probes that reached their target")
    failed: int = Field(description="Probes that did not")


def build_server(app: NetVerify) -> MCPServer:
    server = MCPServer("netverify", instructions=INSTRUCTIONS)

    @server.resource(
        "policy://intent",
        name="intent",
        description="The intent policy in force: targets and rules.",
        mime_type="application/yaml",
    )
    def intent() -> str:
        return app.resolve_policy(None).read_text(encoding="utf-8")

    @server.tool(
        name="wait_converged",
        description=(
            "Poll until every OSPF adjacency and BGP session named in the policy is up and "
            "the routers' routing tables stop changing, or until timeout seconds pass. Call "
            "this before judging anything after a change."
        ),
        annotations=ToolAnnotations(read_only_hint=True),
    )
    async def wait_converged(timeout: int = 60) -> ConvergeResult:
        try:
            return await app.wait_converged(timeout=float(timeout))
        except (ReadOnlyViolation, ValueError, FileNotFoundError) as exc:
            raise ToolError(str(exc)) from exc

    @server.tool(
        name="reachability_matrix",
        description=(
            "Run every reachability probe implied by the policy (ICMP, DF-bit MTU probes, TCP "
            "connects) from the source hosts and report which passed."
        ),
        annotations=ToolAnnotations(read_only_hint=True),
    )
    async def reachability_matrix() -> ReachabilityResult:
        try:
            probes = await app.reachability()
        except (ReadOnlyViolation, ValueError, FileNotFoundError) as exc:
            raise ToolError(str(exc)) from exc
        ok = sum(p.ok for p in probes)
        return ReachabilityResult(probes=probes, ok=ok, failed=len(probes) - ok)

    @server.tool(
        name="route_diff",
        description=(
            "Compare the routing tables captured in two snapshots (ids or prefixes of at "
            "least 8 characters) and list routes added and removed per node."
        ),
        annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True),
    )
    async def route_diff(before_id: str, after_id: str) -> RouteDiff:
        try:
            return app.route_diff(before_id, after_id)
        except KeyError as exc:
            raise ToolError(f"unknown snapshot: {exc}") from exc

    @server.tool(
        name="intent_check",
        description=(
            "Wait for convergence, capture a snapshot, then evaluate every intent rule "
            "(reach, path_mtu, ospf_full, bgp_established, no_route_leak, no_spof) against "
            "the live twin. Returns pass/fail per rule with evidence and, when everything "
            "passes, a signed attestation bound to the snapshot id."
        ),
        annotations=ToolAnnotations(read_only_hint=True),
    )
    async def intent_check() -> VerificationReport:
        try:
            return await app.intent_check()
        except (ReadOnlyViolation, ValueError, FileNotFoundError) as exc:
            raise ToolError(str(exc)) from exc

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
    app = NetVerify.from_settings(settings)
    log.info(
        "netverify: lab=%s policy=%s nodes=%d port=%d",
        settings.lab_name,
        settings.policy_path,
        len(app.topology.nodes),
        settings.netverify_port,
    )
    build_server(app).run(
        "streamable-http",
        host="0.0.0.0",
        port=settings.netverify_port,
        transport_security=transport_security(),
    )


if __name__ == "__main__":
    main()
