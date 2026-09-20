"""Capture and store the state of the twin (read-only; restoring lives in twinlab.snapshot).

A snapshot is the normalised FRR running-config plus kernel networking state of every node,
content-addressed by sha256. Restoring a snapshot diffs the current state against it and
runs the minimal set of argv commands (plus `frr-reload.py` for FRR) to get back.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any

from nettwin_core.executor import ExecResult, Executor
from nettwin_core.models import NodeState, Snapshot
from nettwin_core.topology import Node, Topology

ROUTER_ROLES = frozenset({"router"})
SWITCH_ROLES = frozenset({"switch"})
MGMT_IFACES = frozenset({"eth0"})

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
