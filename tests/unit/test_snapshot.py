from __future__ import annotations

import json
from pathlib import Path

import pytest

from nettwin_core.executor import FakeExecutor
from nettwin_core.models import NodeState, Snapshot
from nettwin_core.topology import Node, Topology
from twinlab.snapshot import (
    FRR_RELOAD,
    RESTORE_PATH,
    SnapshotError,
    SnapshotStore,
    addresses,
    capture,
    capture_node,
    mtus,
    normalise_running_config,
    plan_frr_restore,
    plan_kernel_restore,
    restore,
    vlan_links,
)

RUNNING_CONFIG = """Building configuration...

Current configuration:
!
frr version 10.2.1_git
frr defaults traditional
hostname r3
!
interface eth1
 ip ospf area 0.0.0.1
exit
!
end
"""

ADDRS = [
    {
        "ifindex": 2,
        "ifname": "eth0",
        "addr_info": [
            {"family": "inet", "local": "172.20.20.6", "prefixlen": 24, "scope": "global"}
        ],
    },
    {
        "ifindex": 5,
        "link_index": 98,
        "ifname": "eth1",
        "addr_info": [
            {
                "family": "inet",
                "local": "10.0.13.2",
                "prefixlen": 30,
                "scope": "global",
                "valid_life_time": 4294967295,
            }
        ],
    },
    {
        "ifindex": 1,
        "ifname": "lo",
        "addr_info": [
            {"family": "inet", "local": "127.0.0.1", "prefixlen": 8, "scope": "host"},
            {"family": "inet", "local": "10.255.0.3", "prefixlen": 32, "scope": "global"},
        ],
    },
]

LINKS = [
    {"ifindex": 2, "ifname": "eth0", "mtu": 1500},
    {"ifindex": 5, "link_index": 98, "ifname": "eth1", "mtu": 1500, "link": "if98", "txqlen": 1000},
    {"ifindex": 7, "ifname": "eth3", "mtu": 1500},
    {
        "ifindex": 9,
        "ifname": "eth3.10",
        "mtu": 1500,
        "link": "eth3",
        "linkinfo": {"info_kind": "vlan", "info_data": {"protocol": "802.1Q", "id": 10}},
    },
]

ROUTES = [
    {"dst": "default", "gateway": "172.20.20.1", "dev": "eth0"},
    {"dst": "10.0.40.0/24", "gateway": "10.0.23.2", "dev": "eth2", "protocol": "ospf", "nhid": 34},
]


def _fake_router() -> FakeExecutor:
    fake = FakeExecutor()
    fake.script("r3", ["ip", "-j", "-4", "addr"], json.dumps(ADDRS))
    fake.script("r3", ["ip", "-j", "-d", "link"], json.dumps(LINKS))
    fake.script("r3", ["ip", "-j", "-4", "route"], json.dumps(ROUTES))
    fake.script("r3", ["vtysh", "-c", "show running-config"], RUNNING_CONFIG)
    fake.script("r3", ["nft", "-s", "list", "ruleset"], "table inet fw {\n}\n")
    return fake


def test_normalise_running_config_strips_headers() -> None:
    text = normalise_running_config(RUNNING_CONFIG)
    assert text.startswith("!\nfrr defaults traditional")
    assert "frr version" not in text
    assert "Building" not in text
    assert text.endswith("end")


async def test_capture_node_normalises_and_hashes_deterministically() -> None:
    fake = _fake_router()
    node = Node(name="r3", role="router")
    state = await capture_node(fake, node)
    assert [e["ifname"] for e in state.addrs] == ["eth1", "lo"]
    assert addresses(state) == {"eth1": ["10.0.13.2/30"], "lo": ["10.255.0.3/32"]}
    assert mtus(state) == {"eth1": 1500, "eth3": 1500, "eth3.10": 1500}
    assert vlan_links(state) == {"eth3.10": ("eth3", 10)}
    assert all("nhid" not in r for r in state.routes)
    assert all(r.get("dev") != "eth0" for r in state.routes)
    assert "link" not in next(e for e in state.links if e["ifname"] == "eth1")
    assert state.nft == "table inet fw {\n}"

    again = await capture_node(_fake_router(), node)
    assert Snapshot.compute_id({"r3": state}) == Snapshot.compute_id({"r3": again})


async def test_capture_raises_on_failed_command() -> None:
    fake = FakeExecutor()
    fake.script("h1", ["ip", "-j", "-4", "addr"], "[]")
    fake.script("h1", ["ip", "-j", "-d", "link"], "[]")
    topo = Topology(name="t", nodes={"h1": Node(name="h1", role="host")})
    with pytest.raises(SnapshotError):
        await capture(fake, topo, "t")


def test_store_round_trip_and_prefix_resolution(tmp_path: Path) -> None:
    store = SnapshotStore(tmp_path / "snapshots")
    snap = Snapshot.build("lab", {"r1": NodeState(running_config="x")})
    store.save(snap)
    store.save(snap)
    assert store.ids() == [snap.id]
    assert store.load(snap.id[:12]).id == snap.id
    with pytest.raises(KeyError):
        store.resolve("abc")
    with pytest.raises(KeyError):
        store.resolve("0" * 12)


def test_plan_frr_restore_only_when_config_differs() -> None:
    same = NodeState(running_config="a")
    assert plan_frr_restore(same, same) == []
    cmds = plan_frr_restore(NodeState(running_config="a"), NodeState(running_config="b"))
    assert cmds[0].argv == ["tee", RESTORE_PATH]
    assert cmds[0].stdin == b"b\n"
    assert cmds[1].argv == [FRR_RELOAD, "--reload", RESTORE_PATH]


def test_plan_kernel_restore_covers_vlan_mtu_addr_bridge_nft() -> None:
    target = NodeState(
        addrs=[
            {
                "ifname": "eth1",
                "addr_info": [{"local": "10.0.13.2", "prefixlen": 30, "scope": "global"}],
            },
            {
                "ifname": "eth3.20",
                "addr_info": [{"local": "10.0.20.1", "prefixlen": 24, "scope": "global"}],
            },
            {
                "ifname": "lo",
                "addr_info": [
                    {"local": "127.0.0.1", "prefixlen": 8, "scope": "host"},
                    {"local": "10.255.0.3", "prefixlen": 32, "scope": "global"},
                ],
            },
        ],
        links=[
            {"ifname": "eth1", "mtu": 1500},
            {"ifname": "eth3", "mtu": 1500},
            {
                "ifname": "eth3.20",
                "mtu": 1500,
                "link": "eth3",
                "linkinfo": {"info_kind": "vlan", "info_data": {"id": 20}},
            },
            {"ifname": "br0", "mtu": 1500, "linkinfo": {"info_kind": "bridge"}},
        ],
        bridge_vlans=[
            {"ifname": "eth2", "vlans": [{"vlan": 10, "flags": ["Egress Untagged", "PVID"]}]},
            {"ifname": "br0", "vlans": []},
        ],
        nft="table inet fw {\n}",
    )
    current = NodeState(
        addrs=[
            {
                "ifname": "eth1",
                "addr_info": [{"local": "10.0.13.2", "prefixlen": 24, "scope": "global"}],
            },
            {"ifname": "eth3.30", "addr_info": []},
            {
                "ifname": "lo",
                "addr_info": [{"local": "127.0.0.1", "prefixlen": 8, "scope": "host"}],
            },
        ],
        links=[
            {"ifname": "eth1", "mtu": 1400},
            {"ifname": "eth3", "mtu": 1500},
            {
                "ifname": "eth3.30",
                "mtu": 1500,
                "link": "eth3",
                "linkinfo": {"info_kind": "vlan", "info_data": {"id": 30}},
            },
            {"ifname": "br0", "mtu": 1500, "linkinfo": {"info_kind": "bridge"}},
        ],
        bridge_vlans=[
            {"ifname": "eth2", "vlans": [{"vlan": 20, "flags": ["PVID"]}]},
            {"ifname": "br0", "vlans": [{"vlan": 1, "flags": []}]},
        ],
        nft="",
    )
    argv = [c.argv for c in plan_kernel_restore(current, target)]
    assert ["ip", "link", "del", "dev", "eth3.30"] in argv
    assert [
        "ip",
        "link",
        "add",
        "link",
        "eth3",
        "name",
        "eth3.20",
        "type",
        "vlan",
        "id",
        "20",
    ] in argv
    assert ["ip", "link", "set", "dev", "eth1", "mtu", "1500"] in argv
    assert ["ip", "-4", "addr", "flush", "dev", "eth1"] in argv
    assert ["ip", "addr", "add", "10.0.13.2/30", "dev", "eth1"] in argv
    assert ["ip", "addr", "add", "10.0.20.1/24", "dev", "eth3.20"] in argv
    assert ["ip", "addr", "add", "10.255.0.3/32", "dev", "lo"] in argv
    assert ["ip", "-4", "addr", "flush", "dev", "lo", "scope", "global"] not in argv
    assert ["bridge", "vlan", "del", "dev", "eth2", "vid", "20"] in argv
    assert ["bridge", "vlan", "add", "dev", "eth2", "vid", "10", "pvid", "untagged"] in argv
    assert ["bridge", "vlan", "del", "dev", "br0", "vid", "1", "self"] in argv
    nft = [c for c in plan_kernel_restore(current, target) if c.argv == ["nft", "-f", "-"]]
    assert nft and nft[0].stdin.startswith(b"flush ruleset\n")
    assert plan_kernel_restore(target, target) == []


async def test_restore_runs_plan_and_reports_changed_nodes() -> None:
    fake = FakeExecutor()
    fake.on(lambda node, argv: "")
    topo = Topology(
        name="t", nodes={"r1": Node(name="r1", role="router"), "h1": Node(name="h1", role="host")}
    )
    current = Snapshot.build("t", {"r1": NodeState(running_config="a"), "h1": NodeState()})
    target = Snapshot.build("t", {"r1": NodeState(running_config="b"), "h1": NodeState()})
    changed = await restore(fake, topo, target, current)
    assert list(changed) == ["r1"]
    assert fake.stdin_log[0][:2] == ("r1", ("tee", RESTORE_PATH))


async def test_restore_raises_when_a_step_fails() -> None:
    fake = FakeExecutor()
    topo = Topology(name="t", nodes={"r1": Node(name="r1", role="router")})
    current = Snapshot.build("t", {"r1": NodeState(running_config="a")})
    target = Snapshot.build("t", {"r1": NodeState(running_config="b")})
    with pytest.raises(SnapshotError):
        await restore(fake, topo, target, current)


def test_normalise_links_scrubs_bridge_timers_but_keeps_vlan_ids() -> None:
    from twinlab.snapshot import normalise_links

    raw = [
        {
            "ifname": "br0",
            "mtu": 1500,
            "linkinfo": {
                "info_kind": "bridge",
                "info_data": {"gc_timer": 198.87, "hello_timer": 0.0, "vlan_filtering": 1},
            },
        },
        {
            "ifname": "eth2",
            "mtu": 1500,
            "linkinfo": {
                "info_kind": "veth",
                "info_slave_kind": "bridge",
                "info_slave_data": {"state": "forwarding", "port_no": "0x2", "cost": 2},
            },
        },
        {
            "ifname": "eth3.10",
            "mtu": 1500,
            "link": "eth3",
            "linkinfo": {"info_kind": "vlan", "info_data": {"protocol": "802.1Q", "id": 10}},
        },
    ]
    links = {e["ifname"]: e for e in normalise_links(raw)}
    assert links["br0"]["linkinfo"]["info_data"] == {"vlan_filtering": 1}
    assert links["eth2"]["linkinfo"]["info_slave_data"] == {"cost": 2}
    assert links["eth3.10"]["linkinfo"]["info_data"]["id"] == 10
