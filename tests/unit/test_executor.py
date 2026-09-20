from __future__ import annotations

import asyncio

import pytest

from nettwin_core.executor import (
    TRUNCATION_MARKER,
    DockerExecutor,
    Executor,
    FakeExecutor,
    cap_output,
)


async def test_fake_executor_exact_prefix_handler_and_default() -> None:
    fake = FakeExecutor()
    fake.script("r1", ["vtysh", "-c", "show ip route"], "R 0.0.0.0/0")
    fake.script_prefix("r1", ["ip", "-j"], "[]")
    fake.on(lambda node, argv: "pong" if argv and argv[0] == "ping" else None)

    exact = await fake.exec("r1", ["vtysh", "-c", "show ip route"])
    assert exact.ok and exact.stdout == "R 0.0.0.0/0"
    prefixed = await fake.exec("r1", ["ip", "-j", "addr"])
    assert prefixed.ok and prefixed.stdout == "[]"
    handled = await fake.exec("r1", ["ping", "-c", "1", "10.0.0.1"])
    assert handled.ok and handled.stdout == "pong"
    unknown = await fake.exec("r1", ["nft", "list", "ruleset"])
    assert unknown.returncode == 127 and "no script" in unknown.stderr
    assert fake.calls_for("r1")[0] == ("vtysh", "-c", "show ip route")
    assert isinstance(fake, Executor)


async def test_fake_executor_rejects_bad_node() -> None:
    fake = FakeExecutor()
    with pytest.raises(ValueError):
        await fake.exec("R1 ", ["true"])


def test_cap_output() -> None:
    text, truncated = cap_output("x" * 10, 5)
    assert truncated and text == "xxxxx" + TRUNCATION_MARKER
    assert cap_output("short", 100) == ("short", False)


def test_docker_executor_container_and_command() -> None:
    ex = DockerExecutor("nettwin")
    assert ex.container("r1") == "clab-nettwin-r1"
    assert ex.command("r1", ["vtysh", "-c", "show ip route"]) == [
        "docker",
        "exec",
        "clab-nettwin-r1",
        "vtysh",
        "-c",
        "show ip route",
    ]
    with pytest.raises(ValueError):
        ex.container("r1; rm")
    with pytest.raises(ValueError):
        ex.command("r1", [])


class _FakeProc:
    def __init__(self, out: bytes, err: bytes, rc: int, delay: float = 0.0) -> None:
        self._out, self._err, self.returncode, self._delay = out, err, rc, delay
        self.killed = False

    async def communicate(self) -> tuple[bytes, bytes]:
        if self._delay:
            await asyncio.sleep(self._delay)
        return self._out, self._err

    def kill(self) -> None:
        self.killed = True

    async def wait(self) -> int:
        return self.returncode


async def test_docker_executor_runs_argv_without_shell(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, tuple[str, ...]] = {}

    async def fake_create(*cmd: str, **_: object) -> _FakeProc:
        captured["cmd"] = cmd
        return _FakeProc(b"ok\n", b"", 0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)
    ex = DockerExecutor("nettwin", output_cap=2)
    result = await ex.exec("r2", ["ip", "-j", "addr"])
    assert captured["cmd"] == ("docker", "exec", "clab-nettwin-r2", "ip", "-j", "addr")
    assert result.ok is True
    assert result.truncated is True
    assert result.stdout.startswith("ok")


async def test_docker_executor_times_out(monkeypatch: pytest.MonkeyPatch) -> None:
    proc = _FakeProc(b"", b"", 0, delay=5.0)

    async def fake_create(*_: str, **__: object) -> _FakeProc:
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)
    result = await DockerExecutor("nettwin").exec("r1", ["sleep", "5"], timeout=0.01)
    assert result.timed_out is True
    assert result.ok is False
    assert proc.killed is True
