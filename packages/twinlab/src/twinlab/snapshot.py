"""Restore the twin to a snapshot.

Capture, normalisation and the store live in :mod:`nettwin_core.snapshot` so netverify can
read snapshots without importing twinlab. This module owns the mutating half: computing and
running the minimal set of commands that brings a node back to a stored state.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from dataclasses import dataclass, field

from nettwin_core.executor import Executor
from nettwin_core.models import NodeState, Snapshot
from nettwin_core.snapshot import (
    MGMT_IFACES,
    ROUTER_ROLES,
    SWITCH_ROLES,
    SnapshotError,
    SnapshotStore,
    addresses,
    bridge_devices,
    bridge_vlans,
    capture,
    capture_node,
    mtus,
    normalise_addrs,
    normalise_bridge_vlans,
    normalise_links,
    normalise_routes,
    normalise_running_config,
    vlan_links,
)
from nettwin_core.topology import Node, Topology

RESTORE_PATH = "/tmp/nettwin-restore.conf"
FRR_RELOAD = "/usr/lib/frr/frr-reload.py"

__all__ = [
    "MGMT_IFACES",
    "ROUTER_ROLES",
    "SWITCH_ROLES",
    "SnapshotError",
    "SnapshotStore",
    "addresses",
    "bridge_devices",
    "bridge_vlans",
    "capture",
    "capture_node",
    "mtus",
    "normalise_addrs",
    "normalise_bridge_vlans",
    "normalise_links",
    "normalise_routes",
    "normalise_running_config",
    "vlan_links",
    "RESTORE_PATH",
    "FRR_RELOAD",
    "Command",
    "RestorePlan",
    "plan_frr_restore",
    "plan_kernel_restore",
    "plan_restore",
    "restore",
]


# --- restore --------------------------------------------------------------------------------


@dataclass(frozen=True)
class Command:
    argv: list[str]
    stdin: bytes | None = None


@dataclass
class RestorePlan:
    frr: list[Command] = field(default_factory=list)
    kernel: list[Command] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.frr and not self.kernel

    def argv(self) -> list[list[str]]:
        return [c.argv for c in (*self.frr, *self.kernel)]


def plan_frr_restore(current: NodeState, target: NodeState) -> list[Command]:
    if (current.running_config or "") == (target.running_config or ""):
        return []
    return [
        Command(["tee", RESTORE_PATH], stdin=(target.running_config + "\n").encode()),
        Command([FRR_RELOAD, "--reload", RESTORE_PATH]),
    ]


def plan_kernel_restore(
    current: NodeState, target: NodeState, *, protect: Iterable[str] = MGMT_IFACES
) -> list[Command]:
    protected = set(protect)
    cmds: list[Command] = []

    cur_vlans, tgt_vlans = vlan_links(current), vlan_links(target)
    for name, spec in sorted(cur_vlans.items()):
        if tgt_vlans.get(name) != spec:
            cmds.append(Command(["ip", "link", "del", "dev", name]))
    recreated: set[str] = set()
    for name, (parent, vid) in sorted(tgt_vlans.items()):
        if cur_vlans.get(name) != (parent, vid):
            cmds.append(
                Command(
                    [
                        "ip",
                        "link",
                        "add",
                        "link",
                        parent,
                        "name",
                        name,
                        "type",
                        "vlan",
                        "id",
                        str(vid),
                    ]
                )
            )
            cmds.append(Command(["ip", "link", "set", "dev", name, "up"]))
            recreated.add(name)

    cur_mtu, tgt_mtu = mtus(current), mtus(target)
    for name, mtu in sorted(tgt_mtu.items()):
        if name in protected:
            continue
        if name in recreated or cur_mtu.get(name) != mtu:
            cmds.append(Command(["ip", "link", "set", "dev", name, "mtu", str(mtu)]))

    cur_addr, tgt_addr = addresses(current), addresses(target)
    for name in sorted(set(cur_addr) | set(tgt_addr)):
        if name in protected:
            continue
        want = tgt_addr.get(name, [])
        have = [] if name in recreated else cur_addr.get(name, [])
        if want == have:
            continue
        if have:
            flush = ["ip", "-4", "addr", "flush", "dev", name]
            if name == "lo":
                flush += ["scope", "global"]
            cmds.append(Command(flush))
        for addr in want:
            cmds.append(Command(["ip", "addr", "add", addr, "dev", name]))

    if target.bridge_vlans is not None:
        cur_bv, tgt_bv = bridge_vlans(current), bridge_vlans(target)
        bridges = bridge_devices(target)
        for port in sorted(set(cur_bv) | set(tgt_bv)):
            self_flag = ["self"] if port in bridges else []
            have, want = cur_bv.get(port, set()), tgt_bv.get(port, set())
            for vid, _, _ in sorted(have - want):
                cmds.append(
                    Command(["bridge", "vlan", "del", "dev", port, "vid", str(vid), *self_flag])
                )
            for vid, pvid, untagged in sorted(want - have):
                argv = ["bridge", "vlan", "add", "dev", port, "vid", str(vid)]
                if pvid:
                    argv.append("pvid")
                if untagged:
                    argv.append("untagged")
                cmds.append(Command([*argv, *self_flag]))

    if (current.nft or "") != (target.nft or ""):
        ruleset = "flush ruleset\n" + (target.nft or "") + "\n"
        cmds.append(Command(["nft", "-f", "-"], stdin=ruleset.encode()))
    return cmds


def plan_restore(current: NodeState, target: NodeState, node: Node) -> RestorePlan:
    plan = RestorePlan()
    if node.role in ROUTER_ROLES:
        plan.frr = plan_frr_restore(current, target)
    plan.kernel = plan_kernel_restore(current, target)
    return plan


async def restore(
    executor: Executor, topology: Topology, target: Snapshot, current: Snapshot
) -> dict[str, list[list[str]]]:
    """Bring every node from `current` to `target`. Returns the commands run per changed node."""

    async def one(node: Node) -> tuple[str, list[list[str]]]:
        cur = current.nodes.get(node.name, NodeState())
        tgt = target.nodes.get(node.name)
        if tgt is None:
            return node.name, []
        plan = plan_restore(cur, tgt, node)
        for cmd in (*plan.frr, *plan.kernel):
            res = await executor.exec(node.name, cmd.argv, timeout=60, stdin=cmd.stdin)
            if not res.ok:
                detail = res.stderr or res.stdout
                raise SnapshotError(
                    f"{node.name}: restore step {' '.join(cmd.argv)} failed: {detail}"
                )
        return node.name, plan.argv()

    results = await asyncio.gather(*(one(n) for n in topology.nodes.values()))
    return {name: cmds for name, cmds in results if cmds}
