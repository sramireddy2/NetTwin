"""Wait until the control plane is steady before judging anything.

Converged means: every `ospf_full` and `bgp_established` rule in the policy holds, and the
routers' kernel routing tables are unchanged between two consecutive polls.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time

from pydantic import BaseModel, Field

from nettwin_core.executor import Executor
from nettwin_core.policy import BgpEstablishedRule, OspfFullRule, Policy
from nettwin_core.snapshot import ROUTER_ROLES, normalise_routes
from nettwin_core.topology import Topology


class ConvergeResult(BaseModel):
    converged: bool
    seconds: float
    polls: int
    ospf_missing: list[str] = Field(default_factory=list, description="ospf_full rule ids not Full")
    bgp_down: list[str] = Field(
        default_factory=list, description="bgp_established rule ids not Established"
    )
    rib_stable: bool


async def ospf_full(executor: Executor, node: str, iface: str) -> bool:
    res = await executor.exec(node, ["vtysh", "-c", f"show ip ospf neighbor {iface}"])
    return res.ok and "Full" in res.stdout


_ESTABLISHED_RE = re.compile(r'"bgpState"\s*:\s*"Established"')


async def bgp_established(executor: Executor, node: str, peer: str) -> bool:
    res = await executor.exec(node, ["vtysh", "-c", f"show bgp neighbors {peer} json"])
    return res.ok and _ESTABLISHED_RE.search(res.stdout) is not None


async def rib_fingerprint(executor: Executor, routers: list[str]) -> str:
    outs = await asyncio.gather(*(executor.exec(n, ["ip", "-j", "-4", "route"]) for n in routers))
    payload = {
        node: normalise_routes(json.loads(res.stdout) if res.stdout.strip() else [])
        for node, res in zip(routers, outs, strict=True)
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


async def wait_converged(
    executor: Executor,
    topology: Topology,
    policy: Policy,
    *,
    timeout: float = 60.0,
    interval: float = 2.0,
) -> ConvergeResult:
    routers = [name for name, node in topology.nodes.items() if node.role in ROUTER_ROLES]
    ospf_rules = [r for r in policy.rules if isinstance(r, OspfFullRule)]
    bgp_rules = [r for r in policy.rules if isinstance(r, BgpEstablishedRule)]
    start = time.monotonic()
    previous: str | None = None
    polls = 0
    while True:
        polls += 1
        ospf = await asyncio.gather(*(ospf_full(executor, r.node, r.iface) for r in ospf_rules))
        bgp = await asyncio.gather(*(bgp_established(executor, r.node, r.peer) for r in bgp_rules))
        fingerprint = await rib_fingerprint(executor, routers)
        missing = [r.id for r, ok in zip(ospf_rules, ospf, strict=True) if not ok]
        down = [r.id for r, ok in zip(bgp_rules, bgp, strict=True) if not ok]
        stable = previous == fingerprint
        elapsed = time.monotonic() - start
        if not missing and not down and stable:
            return ConvergeResult(
                converged=True, seconds=round(elapsed, 1), polls=polls, rib_stable=True
            )
        if elapsed >= timeout:
            return ConvergeResult(
                converged=False,
                seconds=round(elapsed, 1),
                polls=polls,
                ospf_missing=missing,
                bgp_down=down,
                rib_stable=stable,
            )
        previous = fingerprint
        await asyncio.sleep(interval)
