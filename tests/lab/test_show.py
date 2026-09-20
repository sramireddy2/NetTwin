"""run_show_command against the live lab (marker: lab)."""

from __future__ import annotations

import json

import pytest

from nettwin_core.executor import DockerExecutor
from nettwin_core.settings import Settings
from twinlab.allowlist import CommandNotAllowed
from twinlab.app import TwinLab, UnknownNode

pytestmark = pytest.mark.lab


@pytest.fixture(scope="module")
def app(settings: Settings, executor: DockerExecutor) -> TwinLab:
    return TwinLab.from_settings(settings, executor=executor)


async def test_frr_show_command(app: TwinLab) -> None:
    res = await app.show("r1", "show ip ospf neighbor")
    assert res.ok
    assert res.stdout.count("Full") >= 3
    assert "Can't open configuration file" not in res.stdout + res.stderr


async def test_kernel_show_command_returns_json(app: TwinLab) -> None:
    res = await app.show("h10", "ip -j -4 addr")
    assert res.ok
    entries = json.loads(res.stdout)
    assert any(e["ifname"] == "eth1" for e in entries)


async def test_probe_command(app: TwinLab) -> None:
    res = await app.show("h10", "ping -c 1 -W 1 10.0.40.10")
    assert res.ok


async def test_rejections(app: TwinLab) -> None:
    with pytest.raises(UnknownNode):
        await app.show("r9", "show ip route")
    with pytest.raises(CommandNotAllowed):
        await app.show("r1", "configure terminal")
