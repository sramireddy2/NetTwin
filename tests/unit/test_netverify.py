"""netverify rules, probes, convergence, attestation and route diff on a scripted twin."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest

from nettwin_core.executor import ExecResult, FakeExecutor
from nettwin_core.models import Attestation, NodeState, Snapshot
from nettwin_core.settings import Settings
from netverify.app import NetVerify, diff_snapshots
from netverify.attest import issue, load_or_create_key, verify
from netverify.executor import ReadOnlyExecutor, ReadOnlyViolation

LAB = Path(__file__).resolve().parents[2] / "lab"


def make_fake(
    *,
    ospf_down: set[tuple[str, str]] = frozenset(),
    bgp_down: bool = False,
    blocked: set[tuple[str, str]] = frozenset(),
    leaked: dict[str, list[str]] | None = None,
    big_dropped: set[tuple[str, str]] = frozenset(),
) -> FakeExecutor:
    """Golden twin unless told otherwise. Guest->srv and inet->guest are always blocked."""
    always_blocked = {("h20", "10.0.40.10"), ("inet", "10.0.20.10")}

    def fail(node: str, argv: Sequence[str]) -> ExecResult:
        return ExecResult(node, tuple(argv), 1, "", "100% packet loss", 0)

    def handler(node: str, argv: Sequence[str]) -> ExecResult | str | None:
        if argv[:2] == ("vtysh", "-c"):
            cmd = argv[2]
            if cmd.startswith("show ip ospf neighbor"):
                iface = cmd.split()[-1]
                return (
                    ""
                    if (node, iface) in ospf_down
                    else f"10.255.0.9 1 Full/- 3.2s 10.0.0.1 {iface}"
                )
            if cmd.startswith("show bgp neighbors"):
                state = "Active" if bgp_down else "Established"
                return json.dumps({"203.0.113.2": {"bgpState": state}})
            if "longer-prefixes" in cmd:
                prefix = cmd.split()[3]
                found = (leaked or {}).get(prefix, [])
                return json.dumps({p: [] for p in found}) if found else "{}"
            if cmd == "show running-config":
                return f"hostname {node}\nend\n"
            return ""
        if argv[0] == "ping":
            target = argv[-1]
            if (node, target) in always_blocked | set(blocked):
                return fail(node, argv)
            if "-M" in argv and (node, target) in big_dropped:
                return fail(node, argv)
            return "2 packets transmitted, 2 received, 0% packet loss"
        if argv[0] == "nc":
            return ""
        if argv[0] == "ip":
            return "[]"
        if argv[0] in ("nft", "bridge"):
            return ""
        return None

    return FakeExecutor().on(handler)


def make_app(tmp_path: Path, fake: FakeExecutor) -> NetVerify:
    settings = Settings.from_env(
        {
            "NETTWIN_STATE_DIR": str(tmp_path),
            "NETTWIN_TOPOLOGY": str(LAB / "topology.clab.yml"),
            "NETTWIN_POLICY": str(LAB / "policy" / "intent.yaml"),
        }
    )
    return NetVerify.from_settings(settings, executor=fake)


async def test_readonly_executor_refuses_writes_and_stdin() -> None:
    fake = FakeExecutor().on(lambda n, a: "ok")
    ro = ReadOnlyExecutor(fake)
    assert (await ro.exec("r1", ["vtysh", "-c", "show ip route"])).stdout == "ok"
    assert (await ro.exec("r1", ["ip", "-j", "-4", "addr"])).stdout == "ok"
    with pytest.raises(ReadOnlyViolation):
        await ro.exec("r1", ["ip", "link", "set", "dev", "eth1", "mtu", "1400"])
    with pytest.raises(ReadOnlyViolation):
        await ro.exec("r1", ["vtysh", "-c", "configure terminal", "-c", "router ospf"])
    with pytest.raises(ReadOnlyViolation):
        await ro.exec("r1", ["tee", "/tmp/x"], stdin=b"x")
    with pytest.raises(ReadOnlyViolation):
        await ro.exec("r1", ["nft", "-f", "-"])
    assert all(argv[0] in ("vtysh", "ip") for _, argv in fake.calls)


def test_attestation_round_trip_and_tamper(tmp_path: Path) -> None:
    key = load_or_create_key(tmp_path / "attest.key")
    assert load_or_create_key(tmp_path / "attest.key") == key
    att = issue(key, "a" * 64, "b" * 64)
    assert verify(key, att)
    tampered = Attestation(
        snapshot_id="c" * 64, policy_sha256=att.policy_sha256, issued_at=att.issued_at, mac=att.mac
    )
    assert not verify(key, tampered)
    assert not verify(b"other", att)
    stale = Attestation(
        snapshot_id=att.snapshot_id,
        policy_sha256=att.policy_sha256,
        issued_at=datetime(2020, 1, 1, tzinfo=UTC),
        mac=att.mac,
    )
    assert not verify(key, stale)


async def test_intent_check_passes_on_golden_twin(tmp_path: Path) -> None:
    app = make_app(tmp_path, make_fake())
    report = await app.intent_check(interval=0.01)
    assert report.passed, report.failed_rules
    assert report.attestation is not None and verify(app.key, report.attestation)
    assert report.snapshot_id in app.store.ids()
    assert {r.kind for r in report.rules} == {
        "reach",
        "path_mtu",
        "ospf_full",
        "bgp_established",
        "no_route_leak",
        "no_spof",
    }


@pytest.mark.parametrize(
    ("fault", "expected"),
    [
        ({"ospf_down": {("r1", "eth2")}}, {"ospf-r1-eth2"}),
        ({"bgp_down": True}, {"ebgp-r4-isp"}),
        ({"blocked": {("h10", "10.0.40.10")}}, {"corp-to-srv", "corp-to-srv-mtu"}),
        ({"big_dropped": {("h10", "10.0.40.10")}}, {"corp-to-srv-mtu"}),
        ({"leaked": {"10.0.20.0/24": ["10.0.20.0/24"]}}, {"no-guest-leak"}),
    ],
)
async def test_intent_check_reports_exactly_the_broken_rules(
    tmp_path: Path, fault: dict, expected: set[str]
) -> None:
    app = make_app(tmp_path, make_fake(**fault))
    report = await app.intent_check(interval=0.01, converge_timeout=0.05)
    failed = set(report.failed_rules) - {"converged"}
    assert failed == expected
    assert report.attestation is None


async def test_wait_converged_reports_missing_and_stability(tmp_path: Path) -> None:
    app = make_app(tmp_path, make_fake(ospf_down={("r2", "eth2")}))
    result = await app.wait_converged(timeout=0.05, interval=0.01)
    assert not result.converged and result.ospf_missing == ["ospf-r2-eth2"]
    healthy = make_app(tmp_path, make_fake())
    result = await healthy.wait_converged(timeout=5, interval=0.01)
    assert result.converged and result.polls >= 2


def test_policy_path_is_confined(tmp_path: Path) -> None:
    app = make_app(tmp_path, make_fake())
    with pytest.raises(ValueError):
        app.resolve_policy(str(LAB / "topology.clab.yml"))
    with pytest.raises(FileNotFoundError):
        app.resolve_policy(str(LAB / "policy" / "missing.yaml"))
    assert app.resolve_policy(None).name == "intent.yaml"


def test_route_diff_lists_added_and_removed_per_node() -> None:
    before = Snapshot.build(
        "t",
        {
            "r1": NodeState(
                routes=[
                    {
                        "dst": "10.0.40.0/24",
                        "gateway": "10.0.12.2",
                        "dev": "eth1",
                        "protocol": "ospf",
                    },
                    {"dst": "default", "gateway": "10.0.14.2", "dev": "eth3", "protocol": "ospf"},
                ]
            ),
            "r2": NodeState(routes=[{"dst": "10.0.10.0/24", "dev": "eth2", "protocol": "ospf"}]),
        },
    )
    after = Snapshot.build(
        "t",
        {
            "r1": NodeState(
                routes=[
                    {
                        "dst": "10.0.40.0/24",
                        "nexthops": [{"gateway": "10.0.12.2"}, {"gateway": "10.0.13.2"}],
                        "protocol": "ospf",
                    }
                ]
            ),
            "r2": NodeState(routes=[{"dst": "10.0.10.0/24", "dev": "eth2", "protocol": "ospf"}]),
        },
    )
    diff = diff_snapshots(before, after)
    assert diff.changed_nodes == 1 and list(diff.nodes) == ["r1"]
    assert diff.nodes["r1"].removed == [
        "10.0.40.0/24 via 10.0.12.2 dev eth1 proto ospf",
        "default via 10.0.14.2 dev eth3 proto ospf",
    ]
    assert diff.nodes["r1"].added == ["10.0.40.0/24 via 10.0.12.2,10.0.13.2 proto ospf"]
