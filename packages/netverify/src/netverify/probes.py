"""Reachability probes derived from the intent policy.

Each `reach` and `path_mtu` rule becomes one probe run from the source host: ICMP, ICMP with
DF set at the required size, or a TCP connect. Probes run concurrently.
"""

from __future__ import annotations

import asyncio

from pydantic import BaseModel

from nettwin_core.executor import Executor
from nettwin_core.policy import PathMtuRule, Policy, ReachRule

PROBE_TIMEOUT = 20.0
ProbeRule = ReachRule | PathMtuRule


class ProbeResult(BaseModel):
    rule_id: str
    src: str
    dst: str
    kind: str
    argv: list[str]
    ok: bool
    detail: str = ""


def probe_argv(rule: ProbeRule, policy: Policy) -> list[str]:
    dst_ip = policy.targets[rule.dst].ip
    if isinstance(rule, PathMtuRule):
        size = rule.min_mtu - 28
        return ["ping", "-c", "2", "-W", "1", "-M", "do", "-s", str(size), dst_ip]
    if rule.proto == "tcp":
        return ["nc", "-z", "-w", "2", dst_ip, str(rule.port)]
    return ["ping", "-c", "2", "-W", "1", dst_ip]


def probe_kind(rule: ProbeRule) -> str:
    if isinstance(rule, PathMtuRule):
        return "path_mtu"
    return "tcp" if rule.proto == "tcp" else "icmp"


async def run_probe(
    executor: Executor, policy: Policy, rule: ProbeRule, sem: asyncio.Semaphore
) -> ProbeResult:
    src_node = policy.targets[rule.src].node
    argv = probe_argv(rule, policy)
    async with sem:
        res = await executor.exec(src_node, argv, timeout=PROBE_TIMEOUT)
    lines = [ln for ln in (res.stdout + res.stderr).splitlines() if ln.strip()]
    return ProbeResult(
        rule_id=rule.id,
        src=rule.src,
        dst=rule.dst,
        kind=probe_kind(rule),
        argv=argv,
        ok=res.ok,
        detail=lines[-1] if lines else "",
    )


async def reachability_matrix(
    executor: Executor, policy: Policy, *, concurrency: int = 8
) -> list[ProbeResult]:
    rules = [r for r in policy.rules if isinstance(r, ReachRule | PathMtuRule)]
    sem = asyncio.Semaphore(concurrency)
    return list(await asyncio.gather(*(run_probe(executor, policy, r, sem) for r in rules)))
