from __future__ import annotations

from pathlib import Path

import pytest

from nettwin_core.executor import FakeExecutor
from nettwin_core.models import NodeState
from nettwin_core.ops import FrrLines, SetMtu
from nettwin_core.settings import Settings
from twinlab.app import TwinLab, UnknownNode
from twinlab.apply import ApplyError, apply_ops, describe_change, frr_errors, kernel_diff


def test_frr_errors_detects_percent_lines() -> None:
    out = "line 1\n% Must remove previous area config before changing ospf area\n"
    assert frr_errors(out) == ["% Must remove previous area config before changing ospf area"]
    assert frr_errors("all good\n") == []


async def test_apply_ops_runs_in_order_and_stops_on_failure() -> None:
    fake = FakeExecutor()
    fake.on(lambda node, argv: "" if argv[0] == "ip" else None)
    fake.script(
        "r1", ["vtysh", "-c", "configure terminal", "-c", "router ospf"], "% Unknown command"
    )
    ops = [
        SetMtu(iface="eth1", mtu=1400),
        FrrLines(lines=["router ospf"]),
        SetMtu(iface="eth2", mtu=1400),
    ]
    with pytest.raises(ApplyError) as excinfo:
        await apply_ops(fake, "r1", ops)
    assert "Unknown command" in str(excinfo.value)
    assert [argv[0] for _, argv in fake.calls] == ["ip", "vtysh"]


async def test_apply_ops_raises_on_nonzero_exit() -> None:
    fake = FakeExecutor()
    with pytest.raises(ApplyError):
        await apply_ops(fake, "r1", [SetMtu(iface="eth1", mtu=1400)])


def test_kernel_diff_and_describe_change() -> None:
    before = NodeState(
        running_config="interface eth1\n mtu 1500",
        links=[{"ifname": "eth1", "mtu": 1500}],
        addrs=[
            {
                "ifname": "eth1",
                "addr_info": [{"local": "10.0.0.1", "prefixlen": 24, "scope": "global"}],
            }
        ],
        nft="",
    )
    after = NodeState(
        running_config="interface eth1\n mtu 1400",
        links=[{"ifname": "eth1", "mtu": 1400}],
        addrs=[
            {
                "ifname": "eth1",
                "addr_info": [{"local": "10.0.0.1", "prefixlen": 30, "scope": "global"}],
            }
        ],
        nft="table inet fw {\n}",
    )
    lines = kernel_diff(before, after)
    assert "eth1: mtu 1500 -> 1400" in lines
    assert "eth1: addr ['10.0.0.1/24'] -> ['10.0.0.1/30']" in lines
    assert any(ln.startswith("+table inet fw") for ln in lines)
    text = describe_change(before, after, "r1")
    assert "- mtu 1500" in text and "+ mtu 1400" in text
    assert describe_change(before, before, "r1") == "(no change detected)"


@pytest.fixture
def app(tmp_path: Path, fixtures: Path) -> TwinLab:
    settings = Settings.from_env(
        {
            "NETTWIN_STATE_DIR": str(tmp_path),
            "NETTWIN_TOPOLOGY": str(fixtures / "topology.clab.yml"),
        }
    )
    fake = FakeExecutor()

    def scripted(node: str, argv: tuple[str, ...]) -> str | None:
        if argv[:2] == ("vtysh", "-c") and argv[2] == "show running-config":
            return f"hostname {node}\n!\nrouter ospf\nend\n"
        if argv[0] == "ip":
            return "[]"
        if argv[0] in ("nft", "bridge"):
            return ""
        if argv[0] == "vtysh" and "% boom" in argv:
            return "% boom"
        if argv[0] == "vtysh":
            return ""
        return None

    fake.on(scripted)
    return TwinLab.from_settings(settings, executor=fake)


async def test_app_apply_records_change(app: TwinLab) -> None:
    change = await app.apply("r1", [FrrLines(lines=["router ospf"])], rationale="test")
    assert change.node == "r1" and change.rationale == "test"
    assert app.changes.ids() == [change.change_id]
    assert app.changes.load(change.change_id).before_snapshot_id == change.before_snapshot_id
    assert change.diff == "(no change detected)"


async def test_app_apply_rolls_back_on_failure(app: TwinLab) -> None:
    with pytest.raises(ApplyError) as excinfo:
        await app.apply("r1", [FrrLines(lines=["% boom"])])
    assert "rolled back to snapshot" in str(excinfo.value)
    assert app.changes.ids() == []
    with pytest.raises(UnknownNode):
        await app.apply("r9", [FrrLines(lines=["router ospf"])])
    with pytest.raises(ValueError):
        await app.apply("r1", [])
