from __future__ import annotations

import pytest
from pydantic import ValidationError

from nettwin_core.ops import (
    AddVlan,
    BridgeVlan,
    DelVlan,
    FrrLines,
    NftRule,
    SetAddr,
    SetMtu,
    parse_node_ops,
    parse_ops,
    render,
    render_node_ops,
)


def test_frr_lines_render_uses_vtysh_argv() -> None:
    op = FrrLines(lines=["router ospf", " network 10.0.0.0/24 area 0"])
    assert render(op) == [
        [
            "vtysh",
            "-c",
            "configure terminal",
            "-c",
            "router ospf",
            "-c",
            " network 10.0.0.0/24 area 0",
        ]
    ]


@pytest.mark.parametrize(
    "lines",
    [["router ospf\nexit"], [""], ["   "], ["! comment"], []],
)
def test_frr_lines_rejects_bad_input(lines: list[str]) -> None:
    with pytest.raises(ValidationError):
        FrrLines(lines=lines)


def test_set_mtu_render_and_bounds() -> None:
    assert render(SetMtu(iface="eth1", mtu=1400)) == [
        ["ip", "link", "set", "dev", "eth1", "mtu", "1400"]
    ]
    with pytest.raises(ValidationError):
        SetMtu(iface="eth1", mtu=10)
    with pytest.raises(ValidationError):
        SetMtu(iface="eth1; rm -rf /", mtu=1500)


def test_set_addr_requires_prefix_and_renders_replace() -> None:
    with pytest.raises(ValidationError):
        SetAddr(iface="eth1", address="10.0.0.1")
    op = SetAddr(iface="eth1", address="10.0.0.1/24")
    assert render(op) == [
        ["ip", "addr", "flush", "dev", "eth1"],
        ["ip", "addr", "add", "10.0.0.1/24", "dev", "eth1"],
    ]
    assert render(SetAddr(iface="eth1", address="10.0.0.1/24", replace=False)) == [
        ["ip", "addr", "add", "10.0.0.1/24", "dev", "eth1"]
    ]


def test_add_vlan_default_name_and_render() -> None:
    op = AddVlan(parent="eth1", vlan_id=20)
    assert op.link_name == "eth1.20"
    assert render(op) == [
        ["ip", "link", "add", "link", "eth1", "name", "eth1.20", "type", "vlan", "id", "20"],
        ["ip", "link", "set", "dev", "eth1.20", "up"],
    ]
    assert render(DelVlan(name="eth1.20")) == [["ip", "link", "del", "dev", "eth1.20"]]


def test_bridge_vlan_render() -> None:
    op = BridgeVlan(action="add", dev="eth2", vlan_id=20, pvid=True, untagged=True)
    assert render(op) == [["bridge", "vlan", "add", "dev", "eth2", "vid", "20", "pvid", "untagged"]]


def test_nft_rule_render_variants() -> None:
    add = NftRule(action="add", table="fw", chain="forward", rule="ip saddr 10.0.20.0/24 drop")
    assert render(add) == [
        ["nft", "add", "rule", "inet", "fw", "forward", "ip", "saddr", "10.0.20.0/24", "drop"]
    ]
    insert = NftRule(action="insert", table="fw", chain="forward", rule="accept", index=2)
    assert render(insert) == [
        ["nft", "insert", "rule", "inet", "fw", "forward", "index", "2", "accept"]
    ]
    delete = NftRule(action="delete", table="fw", chain="forward", handle=7)
    assert render(delete) == [["nft", "delete", "rule", "inet", "fw", "forward", "handle", "7"]]
    flush = NftRule(action="flush_chain", family="ip", table="nat", chain="postrouting")
    assert render(flush) == [["nft", "flush", "chain", "ip", "nat", "postrouting"]]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"action": "add", "table": "fw", "chain": "forward"},
        {"action": "delete", "table": "fw", "chain": "forward"},
        {"action": "add", "table": "fw", "chain": "forward", "rule": "accept; drop"},
        {"action": "add", "table": "fw", "chain": "forward", "rule": 'comment "x"'},
        {"action": "add", "table": "fw", "chain": "forward", "rule": "include /etc/x"},
        {"action": "flush_chain", "table": "fw", "chain": "forward", "rule": "accept"},
        {"action": "add", "table": "bad table", "chain": "forward", "rule": "accept"},
    ],
)
def test_nft_rule_rejects_bad_shapes(kwargs: dict) -> None:
    with pytest.raises(ValidationError):
        NftRule(**kwargs)


def test_discriminated_parse_round_trip() -> None:
    ops = parse_ops(
        [
            {"kind": "set_mtu", "iface": "eth1", "mtu": 1400},
            {"kind": "frr_lines", "lines": ["router ospf"]},
        ]
    )
    assert isinstance(ops[0], SetMtu)
    assert isinstance(ops[1], FrrLines)
    with pytest.raises(ValidationError):
        parse_ops([{"kind": "shell", "cmd": "rm -rf /"}])


def test_parse_node_ops_validates_node_names() -> None:
    parsed = parse_node_ops({"r1": [{"kind": "set_mtu", "iface": "eth1", "mtu": 1400}]})
    assert isinstance(parsed["r1"][0], SetMtu)
    with pytest.raises(ValueError):
        parse_node_ops({"R1;": [{"kind": "set_mtu", "iface": "eth1", "mtu": 1400}]})


def test_render_node_ops_preserves_order() -> None:
    parsed = parse_node_ops(
        {
            "r1": [
                {"kind": "set_mtu", "iface": "eth1", "mtu": 1400},
                {"kind": "frr_lines", "lines": ["router ospf"]},
            ]
        }
    )
    rendered = render_node_ops(parsed)
    assert rendered["r1"][0][:2] == ["ip", "link"]
    assert rendered["r1"][1][0] == "vtysh"
