"""Capture, store and restore the state of the twin.

A snapshot is the normalised FRR running-config plus kernel networking state of every node,
content-addressed by sha256. Restoring a snapshot diffs the current state against it and
runs the minimal set of argv commands (plus `frr-reload.py` for FRR) to get back.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from nettwin_core.executor import ExecResult, Executor
from nettwin_core.models import NodeState, Snapshot
from nettwin_core.topology import Node, Topology

ROUTER_ROLES = frozenset({"router"})
SWITCH_ROLES = frozenset({"switch"})
MGMT_IFACES = frozenset({"eth0"})
RESTORE_PATH = "/tmp/nettwin-restore.conf"
FRR_RELOAD = "/usr/lib/frr/frr-reload.py"

_LINK_DROP = frozenset(
    {
        "ifindex",
        "link_index",
        "link_netnsid",
        "txqlen",
        "qdisc",
        "address",
        "broadcast",
        "group",
        "promiscuity",
        "allmulti",
        "min_mtu",
        "max_mtu",
        "gso_max_size",
        "gso_max_segs",
        "tso_max_size",
        "tso_max_segs",
        "gro_max_size",
        "gso_ipv4_max_size",
        "gro_ipv4_max_size",
        "num_tx_queues",
        "num_rx_queues",
        "inet6_addr_gen_mode",
        "parentbus",
        "parentdev",
    }
)
_ADDR_INFO_DROP = frozenset({"valid_life_time", "preferred_life_time", "dynamic", "temporary"})
_PEER_LINK_RE = re.compile(r"^if\d+$")
_VOLATILE_LINKINFO_RE = re.compile(
    r"^(?:.*_timer|topology_change.*|root_id|bridge_id|root_port|root_path_cost|designated_.*|"
    r"state|no|port_no|port_id|config_pending|mcast_.*|multicast_.*)$"
)
_CONFIG_HEADER_RE = re.compile(r"^(Building configuration|Current configuration|frr version)")


class SnapshotError(RuntimeError):
    pass


# --- normalisation -------------------------------------------------------------------------


def normalise_running_config(text: str) -> str:
    lines = [ln.rstrip() for ln in text.splitlines() if not _CONFIG_HEADER_RE.match(ln)]
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


def _ifname(entry: dict[str, Any]) -> str:
    return str(entry.get("ifname", ""))


def _scrub_linkinfo(obj: Any) -> Any:
    """Drop spanning-tree timers, ids and port states that change while a bridge runs."""
    if isinstance(obj, dict):
        return {k: _scrub_linkinfo(v) for k, v in obj.items() if not _VOLATILE_LINKINFO_RE.match(k)}
    if isinstance(obj, list):
        return [_scrub_linkinfo(v) for v in obj]
    return obj


def normalise_links(raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for entry in raw:
        if _ifname(entry) in MGMT_IFACES:
            continue
        cleaned = {k: v for k, v in entry.items() if k not in _LINK_DROP}
        if isinstance(cleaned.get("link"), str) and _PEER_LINK_RE.match(cleaned["link"]):
            del cleaned["link"]
        if "linkinfo" in cleaned:
            cleaned["linkinfo"] = _scrub_linkinfo(cleaned["linkinfo"])
        out.append(cleaned)
    return sorted(out, key=_ifname)


def normalise_addrs(raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for entry in raw:
        if _ifname(entry) in MGMT_IFACES:
            continue
        infos = [
            {k: v for k, v in info.items() if k not in _ADDR_INFO_DROP}
            for info in entry.get("addr_info", [])
            if info.get("family") == "inet"
        ]
        out.append({"ifname": _ifname(entry), "addr_info": sorted(infos, key=lambda i: str(i))})
    return sorted(out, key=_ifname)


def normalise_routes(raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for entry in raw:
        if entry.get("dev") in MGMT_IFACES:
            continue
        cleaned = {k: v for k, v in entry.items() if k != "nhid"}
        out.append(cleaned)
    return sorted(
        out, key=lambda r: (str(r.get("dst")), str(r.get("gateway", "")), str(r.get("dev", "")))
    )


def normalise_bridge_vlans(raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for entry in raw:
        vlans = sorted(
            (
                {"vlan": v.get("vlan"), "flags": sorted(v.get("flags", []))}
                for v in entry.get("vlans", [])
            ),
            key=lambda v: int(v["vlan"] or 0),
        )
        out.append({"ifname": _ifname(entry), "vlans": vlans})
    return sorted(out, key=_ifname)


# --- accessors on normalised state ----------------------------------------------------------


def vlan_links(state: NodeState) -> dict[str, tuple[str, int]]:
    result: dict[str, tuple[str, int]] = {}
    for entry in state.links or []:
        info = entry.get("linkinfo") or {}
        if info.get("info_kind") == "vlan":
            data = info.get("info_data") or {}
            result[_ifname(entry)] = (str(entry.get("link", "")), int(data.get("id", 0)))
    return result


def bridge_devices(state: NodeState) -> set[str]:
    return {
        _ifname(e)
        for e in state.links or []
        if (e.get("linkinfo") or {}).get("info_kind") == "bridge"
    }


def mtus(state: NodeState) -> dict[str, int]:
    return {_ifname(e): int(e["mtu"]) for e in state.links or [] if "mtu" in e}


def addresses(state: NodeState) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for entry in state.addrs or []:
        globals_ = sorted(
            f"{i['local']}/{i['prefixlen']}"
            for i in entry.get("addr_info", [])
            if i.get("scope") == "global"
        )
        result[_ifname(entry)] = globals_
    return result


def bridge_vlans(state: NodeState) -> dict[str, set[tuple[int, bool, bool]]]:
    result: dict[str, set[tuple[int, bool, bool]]] = {}
    for entry in state.bridge_vlans or []:
        result[_ifname(entry)] = {
            (int(v["vlan"]), "PVID" in v["flags"], "Egress Untagged" in v["flags"])
            for v in entry.get("vlans", [])
        }
    return result


# --- capture --------------------------------------------------------------------------------


def _json_or_empty(result: ExecResult) -> Any:
    text = result.stdout.strip()
    return json.loads(text) if text else []


async def capture_node(executor: Executor, node: Node) -> NodeState:
    plan: dict[str, list[str]] = {
        "addrs": ["ip", "-j", "-4", "addr"],
        "links": ["ip", "-j", "-d", "link"],
        "routes": ["ip", "-j", "-4", "route"],
    }
    if node.role in ROUTER_ROLES:
        plan["running_config"] = ["vtysh", "-c", "show running-config"]
        plan["nft"] = ["nft", "-s", "list", "ruleset"]
    if node.role in SWITCH_ROLES:
        plan["bridge_vlans"] = ["bridge", "-j", "vlan", "show"]
    results = dict(
        zip(
            plan,
            await asyncio.gather(*(executor.exec(node.name, argv) for argv in plan.values())),
            strict=True,
        )
    )
    for key, res in results.items():
        if not res.ok:
            raise SnapshotError(
                f"{node.name}: {' '.join(plan[key])} failed: {res.stderr or res.stdout}"
            )
    state = NodeState(
        addrs=normalise_addrs(_json_or_empty(results["addrs"])),
        links=normalise_links(_json_or_empty(results["links"])),
        routes=normalise_routes(_json_or_empty(results["routes"])),
    )
    if "running_config" in results:
        state.running_config = normalise_running_config(results["running_config"].stdout)
        state.nft = results["nft"].stdout.strip()
    if "bridge_vlans" in results:
        state.bridge_vlans = normalise_bridge_vlans(_json_or_empty(results["bridge_vlans"]))
    return state


async def capture(
    executor: Executor, topology: Topology, lab: str, *, concurrency: int = 4
) -> Snapshot:
    sem = asyncio.Semaphore(concurrency)

    async def one(node: Node) -> tuple[str, NodeState]:
        async with sem:
            return node.name, await capture_node(executor, node)

    pairs = await asyncio.gather(*(one(n) for n in topology.nodes.values()))
    return Snapshot.build(lab, dict(pairs))


# --- store ----------------------------------------------------------------------------------


class SnapshotStore:
    """Append-only directory of `<id>.json` files."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True)

    def path(self, snapshot_id: str) -> Path:
        return self.directory / f"{snapshot_id}.json"

    def save(self, snapshot: Snapshot) -> Path:
        path = self.path(snapshot.id)
        if not path.exists():
            path.write_text(snapshot.model_dump_json(indent=1), encoding="utf-8")
        return path

    def exists(self, snapshot_id: str) -> bool:
        return self.path(snapshot_id).exists()

    def load(self, snapshot_id: str) -> Snapshot:
        path = self.path(self.resolve(snapshot_id))
        return Snapshot.model_validate_json(path.read_text(encoding="utf-8"))

    def ids(self) -> list[str]:
        paths = sorted(self.directory.glob("*.json"), key=lambda p: p.stat().st_mtime)
        return [p.stem for p in paths]

    def resolve(self, prefix: str) -> str:
        if len(prefix) < 8:
            raise KeyError(f"snapshot id prefix too short: {prefix!r}")
        matches = [i for i in self.ids() if i.startswith(prefix)]
        if len(matches) != 1:
            raise KeyError(f"snapshot {prefix!r} matches {len(matches)} snapshots")
        return matches[0]


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
