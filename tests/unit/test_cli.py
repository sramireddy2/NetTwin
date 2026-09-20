from __future__ import annotations

from pathlib import Path, PureWindowsPath

import pytest

from nettwin import cli
from nettwin.doctor import FAIL, PASS, WARN, Check, exit_code, format_report
from nettwin_core.settings import Settings


def test_to_wsl_path_windows_drive(monkeypatch: pytest.MonkeyPatch) -> None:
    class _P(PureWindowsPath):
        def resolve(self) -> _P:
            return self

    assert cli.to_wsl_path(_P(r"C:\dev\NetTwin")) == "/mnt/c/dev/NetTwin"  # type: ignore[arg-type]


def test_make_command_shapes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    settings = Settings.from_env({"NETTWIN_WSL_DISTRO": "Containerlab"})
    monkeypatch.setattr(cli.platform, "system", lambda: "Linux")
    assert cli.make_command("up", [], tmp_path, settings) == [
        "make",
        "-C",
        str(tmp_path / "lab"),
        "up",
    ]
    monkeypatch.setattr(cli.platform, "system", lambda: "Windows")
    cmd = cli.make_command("check", ["V=1"], tmp_path, settings)
    assert cmd[:6] == ["wsl.exe", "-d", "Containerlab", "--", "bash", "-lc"]
    assert cmd[6].endswith("&& make -C lab check V=1")


def test_settings_from_env_defaults_and_overrides() -> None:
    s = Settings.from_env({})
    assert s.twinlab_port == 8001 and s.netverify_port == 8002
    assert s.bench is False and s.admin_token is None
    assert s.snapshots_dir == s.state_dir / "snapshots"
    s2 = Settings.from_env({"NETTWIN_BENCH": "yes", "NETTWIN_ADMIN_TOKEN": "t"})
    assert s2.bench is True and s2.admin_token == "t"


def test_doctor_report_and_exit_code() -> None:
    checks = [Check("a", PASS, "ok"), Check("b", WARN, "meh")]
    assert exit_code(checks) == 0
    assert "1 warnings" in format_report(checks)
    assert exit_code([*checks, Check("c", FAIL, "no")]) == 1
