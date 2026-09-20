from __future__ import annotations

import pytest

from twinlab.allowlist import EXAMPLES, CommandNotAllowed, check


@pytest.mark.parametrize("cmd", EXAMPLES)
def test_every_documented_example_is_allowed(cmd: str) -> None:
    check(cmd)


@pytest.mark.parametrize(
    ("cmd", "argv", "family"),
    [
        ("show ip route", ["vtysh", "-c", "show ip route"], "frr"),
        ("show  ip   route json", ["vtysh", "-c", "show ip route json"], "frr"),
        (
            "show ip ospf neighbor eth1 detail",
            ["vtysh", "-c", "show ip ospf neighbor eth1 detail"],
            "frr",
        ),
        ("ip -j -4 addr", ["ip", "-j", "-4", "addr"], "kernel"),
        ("nft list table inet fw", ["nft", "list", "table", "inet", "fw"], "kernel"),
    ],
)
def test_argv_rendering(cmd: str, argv: list[str], family: str) -> None:
    allowed = check(cmd)
    assert allowed.argv == argv
    assert allowed.family == family


def test_probe_timeout_is_longer() -> None:
    assert check("ping -c 5 -W 3 10.0.0.1").timeout > check("show ip route").timeout


@pytest.mark.parametrize(
    "cmd",
    [
        "",
        "   ",
        "show",
        "show run | include ospf",
        "show ip route; reboot",
        "show ip route && rm -rf /",
        "show ip route > /tmp/x",
        "show ip route `id`",
        "show ip route $(id)",
        'show ip route "x"',
        "configure terminal",
        "conf t",
        "clear ip ospf process",
        "write memory",
        "vtysh -c 'show ip route'",
        "ip addr add 10.0.0.1/24 dev eth1",
        "ip link set dev eth1 mtu 1400",
        "ip route flush table main",
        "ip link del dev eth3.10",
        "ip monitor",
        "bridge vlan add dev eth2 vid 30",
        "nft add rule inet fw forward drop",
        "nft flush ruleset",
        "nft -f /etc/x",
        "ping 10.0.0.1",
        "ping -c 100 10.0.0.1",
        "ping -f -c 1 10.0.0.1",
        "ping -c 1 -s 65000 10.0.0.1",
        "ping -c 1 srv",
        "ping -c 1 10.0.0.1 10.0.0.2",
        "traceroute -m 100 10.0.0.1",
        "traceroute -n",
        "sysctl -w net.ipv4.ip_forward=0",
        "sysctl kernel.hostname",
        "cat /etc/frr/frr.conf",
        "sh /setup.sh",
        "frr-reload.py --reload /golden/frr.conf",
        "tcpdump -i eth1",
        "iperf3 -c 10.0.40.10",
        "show ip route " + "x" * 200,
    ],
)
def test_rejected(cmd: str) -> None:
    with pytest.raises(CommandNotAllowed) as excinfo:
        check(cmd)
    assert "Allowed forms include" in str(excinfo.value)
