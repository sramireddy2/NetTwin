"""Golden-state checks against the live lab (marker: lab)."""

from __future__ import annotations

import pytest

from nettwin_core.executor import DockerExecutor

pytestmark = pytest.mark.lab

PING = ["ping", "-c", "2", "-W", "1"]


@pytest.mark.parametrize(
    ("src", "dst", "reachable"),
    [
        ("h10", "10.0.40.10", True),
        ("h10", "198.51.100.10", True),
        ("h20", "198.51.100.10", True),
        ("h20", "10.0.40.10", False),
        ("srv", "10.0.10.10", True),
        ("inet", "10.0.40.10", True),
        ("inet", "10.0.20.10", False),
        ("r1", "10.255.0.4", True),
    ],
)
async def test_golden_reachability(
    executor: DockerExecutor, src: str, dst: str, reachable: bool
) -> None:
    result = await executor.exec(src, [*PING, dst], timeout=10)
    assert result.ok is reachable, result.stdout + result.stderr


async def test_all_ospf_adjacencies_full(executor: DockerExecutor) -> None:
    want = {"r1": 3, "r2": 3, "r3": 2, "r4": 2}
    for node, count in want.items():
        result = await executor.exec(node, ["vtysh", "-c", "show ip ospf neighbor"])
        assert result.ok
        assert result.stdout.count("Full") >= count, f"{node}: {result.stdout}"


async def test_ebgp_established(executor: DockerExecutor) -> None:
    result = await executor.exec(
        "r4", ["vtysh", "-c", "show bgp neighbors 203.0.113.2 json"], timeout=10
    )
    assert '"bgpState":"Established"' in result.stdout


async def test_guest_prefix_not_leaked_to_isp(executor: DockerExecutor) -> None:
    result = await executor.exec("isp", ["vtysh", "-c", "show ip route 10.0.20.0/24"])
    assert "10.0.20.0/24" not in result.stdout
    corp = await executor.exec("isp", ["vtysh", "-c", "show ip route 10.0.10.0/24"])
    assert "10.0.10.0/24" in corp.stdout
