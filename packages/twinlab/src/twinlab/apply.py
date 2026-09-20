"""Apply typed ops to one node and describe what changed."""

from __future__ import annotations

import difflib
from collections.abc import Sequence

from nettwin_core.executor import ExecResult, Executor
from nettwin_core.models import NodeState
from nettwin_core.ops import NftRule, Op, render
from twinlab.snapshot import addresses, bridge_vlans, mtus, vlan_links

APPLY_TIMEOUT = 30.0
NFT_LIST_TIMEOUT = 10.0

#: nft prints protocol numbers it can resolve by name; compare both sides numerically.
_PROTO_ALIASES = {"icmp": "1", "tcp": "6", "udp": "17", "ospf": "89"}


class ApplyError(RuntimeError):
    def __init__(self, node: str, argv: Sequence[str], detail: str) -> None:
        self.node = node
        self.argv = list(argv)
        self.detail = detail
        super().__init__(f"{node}: {' '.join(argv)} failed: {detail}")


def frr_errors(output: str) -> list[str]:
    """vtysh reports rejected lines with a leading '%' and still exits 0."""
    return [ln.strip() for ln in output.splitlines() if ln.strip().startswith("%")]


def normalise_nft_rule(text: str) -> str:
    """Canonical form for comparing a rule as written with a rule as nft prints it."""
    body = text.split("#", 1)[0]
    tokens = [_PROTO_ALIASES.get(t.strip('"'), t.strip('"')) for t in body.split()]
    return " ".join(tokens)


async def resolve_nft_handle(executor: Executor, node: str, op: NftRule) -> int:
    """Find the handle of the rule whose text matches `op.rule` in its chain."""
    argv = ["nft", "-a", "list", "chain", op.family, op.table, op.chain]
    res = await executor.exec(node, argv, timeout=NFT_LIST_TIMEOUT)
    if not res.ok:
        raise ApplyError(node, argv, res.stderr.strip() or res.stdout.strip() or "exit")
    wanted = normalise_nft_rule(op.rule or "")
    for line in res.stdout.splitlines():
        body, sep, handle = line.rpartition("# handle")
        if sep and normalise_nft_rule(body) == wanted and handle.strip().isdigit():
            return int(handle.strip())
    raise ApplyError(
        node, argv, f"no rule matching {op.rule!r} in {op.family} {op.table} {op.chain}"
    )


async def apply_ops(executor: Executor, node: str, ops: Sequence[Op]) -> list[ExecResult]:
    """Run every op's argv in order; stop at the first failure."""
    results: list[ExecResult] = []
    for op in ops:
        if isinstance(op, NftRule) and op.action == "delete" and op.handle is None:
            op = op.model_copy(update={"handle": await resolve_nft_handle(executor, node, op)})
        for argv in render(op):
            res = await executor.exec(node, argv, timeout=APPLY_TIMEOUT)
            results.append(res)
            if not res.ok:
                raise ApplyError(node, argv, res.stderr.strip() or res.stdout.strip() or "exit")
            if argv[0] == "vtysh":
                errors = frr_errors(res.stdout + "\n" + res.stderr)
                if errors:
                    raise ApplyError(node, argv, "; ".join(errors))
    return results


def config_diff(before: NodeState, after: NodeState, node: str) -> str:
    return "\n".join(
        difflib.unified_diff(
            before.running_config.splitlines(),
            after.running_config.splitlines(),
            fromfile=f"{node}/frr.conf@before",
            tofile=f"{node}/frr.conf@after",
            lineterm="",
            n=2,
        )
    )


def kernel_diff(before: NodeState, after: NodeState) -> list[str]:
    lines: list[str] = []
    b_mtu, a_mtu = mtus(before), mtus(after)
    for name in sorted(set(b_mtu) | set(a_mtu)):
        if b_mtu.get(name) != a_mtu.get(name):
            lines.append(f"{name}: mtu {b_mtu.get(name)} -> {a_mtu.get(name)}")
    b_addr, a_addr = addresses(before), addresses(after)
    for name in sorted(set(b_addr) | set(a_addr)):
        if b_addr.get(name) != a_addr.get(name):
            lines.append(f"{name}: addr {b_addr.get(name)} -> {a_addr.get(name)}")
    b_vlan, a_vlan = vlan_links(before), vlan_links(after)
    for name in sorted(set(b_vlan) | set(a_vlan)):
        if b_vlan.get(name) != a_vlan.get(name):
            lines.append(f"{name}: vlan {b_vlan.get(name)} -> {a_vlan.get(name)}")
    b_bv, a_bv = bridge_vlans(before), bridge_vlans(after)
    for port in sorted(set(b_bv) | set(a_bv)):
        if b_bv.get(port) != a_bv.get(port):
            lines.append(
                f"{port}: bridge vlans {sorted(b_bv.get(port, set()))} -> "
                f"{sorted(a_bv.get(port, set()))}"
            )
    if (before.nft or "") != (after.nft or ""):
        lines.extend(
            difflib.unified_diff(
                (before.nft or "").splitlines(),
                (after.nft or "").splitlines(),
                fromfile="nft@before",
                tofile="nft@after",
                lineterm="",
                n=1,
            )
        )
    return lines


def describe_change(before: NodeState, after: NodeState, node: str) -> str:
    parts = [
        p for p in (config_diff(before, after, node), "\n".join(kernel_diff(before, after))) if p
    ]
    return "\n".join(parts) if parts else "(no change detected)"
