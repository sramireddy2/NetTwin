"""Apply typed ops to one node and describe what changed."""

from __future__ import annotations

import difflib
from collections.abc import Sequence

from nettwin_core.executor import ExecResult, Executor
from nettwin_core.models import NodeState
from nettwin_core.ops import Op, render
from twinlab.snapshot import addresses, bridge_vlans, mtus, vlan_links

APPLY_TIMEOUT = 30.0


class ApplyError(RuntimeError):
    def __init__(self, node: str, argv: Sequence[str], detail: str) -> None:
        self.node = node
        self.argv = list(argv)
        self.detail = detail
        super().__init__(f"{node}: {' '.join(argv)} failed: {detail}")


def frr_errors(output: str) -> list[str]:
    """vtysh reports rejected lines with a leading '%' and still exits 0."""
    return [ln.strip() for ln in output.splitlines() if ln.strip().startswith("%")]


async def apply_ops(executor: Executor, node: str, ops: Sequence[Op]) -> list[ExecResult]:
    """Run every op's argv in order; stop at the first failure."""
    results: list[ExecResult] = []
    for op in ops:
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
