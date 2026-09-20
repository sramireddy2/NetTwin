"""Prove that an MTU mismatch reproduces inside containers (marker: lab).

veth drops oversized frames on receive, so lowering the MTU on r2's server-facing port must
drop 1500-byte DF pings from srv while small pings still pass. Scenarios 004 and 005 depend
on this behaviour.
"""

from __future__ import annotations

import pytest

from nettwin_core.executor import DockerExecutor

pytestmark = pytest.mark.lab

GATEWAY = "10.0.40.1"


async def test_mtu_mismatch_drops_large_df_pings_only(executor: DockerExecutor) -> None:
    try:
        lowered = await executor.exec("r2", ["ip", "link", "set", "dev", "eth4", "mtu", "1400"])
        assert lowered.ok, lowered.stderr
        small = await executor.exec(
            "srv", ["ping", "-c", "2", "-W", "1", "-M", "do", "-s", "1300", GATEWAY], timeout=10
        )
        large = await executor.exec(
            "srv", ["ping", "-c", "2", "-W", "1", "-M", "do", "-s", "1472", GATEWAY], timeout=10
        )
        assert small.ok, small.stdout + small.stderr
        assert not large.ok, "1500-byte DF ping should be dropped by the 1400 MTU port"
    finally:
        await executor.exec("r2", ["ip", "link", "set", "dev", "eth4", "mtu", "1500"])
    restored = await executor.exec(
        "srv", ["ping", "-c", "2", "-W", "1", "-M", "do", "-s", "1472", GATEWAY], timeout=10
    )
    assert restored.ok, "MTU restore failed"
