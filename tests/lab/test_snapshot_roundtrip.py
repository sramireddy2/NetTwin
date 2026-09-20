"""Snapshot, break the lab, roll back, and prove the state is byte-identical (marker: lab)."""

from __future__ import annotations

import asyncio

import pytest

from nettwin_core.executor import DockerExecutor
from nettwin_core.settings import Settings
from twinlab.app import TwinLab

pytestmark = pytest.mark.lab

BREAK_R3 = [
    "vtysh",
    "-c",
    "conf t",
    "-c",
    "interface eth1",
    "-c",
    "no ip ospf area 0.0.0.1",
    "-c",
    "ip ospf area 0.0.0.0",
]


async def _wait_full(executor: DockerExecutor, node: str, count: int, timeout: float = 40) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        res = await executor.exec(node, ["vtysh", "-c", "show ip ospf neighbor"])
        if res.stdout.count("Full") >= count:
            return
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"{node} did not reach {count} Full neighbours:\n{res.stdout}")
        await asyncio.sleep(1)


async def test_snapshot_rollback_restores_frr_and_kernel_state(
    settings: Settings, executor: DockerExecutor
) -> None:
    app = TwinLab.from_settings(settings, executor=executor)
    await _wait_full(executor, "r1", 3)
    golden = await app.snapshot()

    # Break two things: an OSPF area on r3 and an MTU on r2.
    await executor.exec("r3", BREAK_R3)
    await executor.exec("r2", ["ip", "link", "set", "dev", "eth1", "mtu", "1400"])
    await asyncio.sleep(2)
    broken = await app.snapshot()
    assert broken.id != golden.id
    assert golden.diff(broken) == {"r2": ["links"], "r3": ["running_config"]}

    result = await app.rollback(golden.id)
    after = app.store.load(result["after"])
    assert result["matches_target"] is True, golden.diff(after)
    assert set(result["changed_nodes"]) == {"r2", "r3"}
    assert after.nodes["r3"].running_config == golden.nodes["r3"].running_config
    await _wait_full(executor, "r1", 3)
