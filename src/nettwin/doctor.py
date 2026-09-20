"""Environment checks for `nettwin doctor`.

Everything here is read-only and runs without a shell: each check is an argv list with a
timeout. On Windows the WSL-side checks are forwarded with `wsl.exe -d <distro> --`.
"""

from __future__ import annotations

import platform
import shutil
import socket
import subprocess
import sys
from dataclasses import dataclass

from nettwin_core.settings import Settings

PASS = "PASS"
FAIL = "FAIL"
WARN = "WARN"
SKIP = "SKIP"


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    detail: str = ""


def _decode(raw: bytes) -> str:
    text = raw.decode("utf-16-le", "ignore") if b"\x00" in raw else raw.decode("utf-8", "ignore")
    return text.replace("\x00", "").strip()


def _run(argv: list[str], timeout: float = 30) -> tuple[int, str]:
    try:
        proc = subprocess.run(argv, capture_output=True, timeout=timeout, check=False)
    except FileNotFoundError:
        return 127, "not found"
    except subprocess.TimeoutExpired:
        return 124, "timed out"
    return proc.returncode, _decode(proc.stdout + proc.stderr)


def _wsl(distro: str, command: str, timeout: float = 60) -> tuple[int, str]:
    return _run(["wsl.exe", "-d", distro, "--", "bash", "-lc", command], timeout)


def _first_line(text: str) -> str:
    return text.splitlines()[0] if text else ""


def _port_open(port: int, host: str = "127.0.0.1") -> bool:
    try:
        with socket.create_connection((host, port), timeout=1.0):
            return True
    except OSError:
        return False


def _tool_check(name: str, required: bool) -> Check:
    path = shutil.which(name)
    if path:
        _, out = _run([name, "--version"], timeout=15)
        return Check(name, PASS, _first_line(out) or path)
    return Check(name, FAIL if required else WARN, "not on PATH")


def _wsl_checks(settings: Settings) -> list[Check]:
    checks: list[Check] = []
    rc, out = _run(["wsl.exe", "-l", "-q"], timeout=20)
    distros = [line.strip() for line in out.splitlines() if line.strip()]
    if rc != 0:
        checks.append(Check("wsl", FAIL, out or "wsl.exe failed"))
        return checks
    if settings.wsl_distro not in distros:
        found = ", ".join(distros) or "none"
        checks.append(
            Check(
                f"wsl distro {settings.wsl_distro}",
                FAIL,
                f"not installed (found: {found}); see docs/setup-wsl.md",
            )
        )
        return checks
    checks.append(Check(f"wsl distro {settings.wsl_distro}", PASS, "installed"))
    checks.extend(_lab_host_checks(lambda cmd: _wsl(settings.wsl_distro, cmd)))
    return checks


def _lab_host_checks(run) -> list[Check]:
    """Checks that must pass on the machine that runs docker and containerlab."""
    checks: list[Check] = []
    rc, out = run("docker info --format '{{.OperatingSystem}} / {{.ServerVersion}}'")
    if rc != 0:
        checks.append(Check("docker engine", FAIL, _first_line(out) or "docker info failed"))
    elif "Docker Desktop" in out:
        checks.append(
            Check(
                "docker engine",
                FAIL,
                f"{_first_line(out)}: containerlab needs a native engine in the distro, "
                "disable Docker Desktop WSL integration for it",
            )
        )
    else:
        checks.append(Check("docker engine", PASS, _first_line(out)))
    rc, out = run("containerlab version 2>/dev/null | grep -i 'version' | head -1")
    checks.append(
        Check("containerlab", PASS if rc == 0 and out else FAIL, _first_line(out) or "not found")
    )
    for tool in ("nft", "make", "rsync", "uv"):
        rc, out = run(f"command -v {tool} >/dev/null && {tool} --version 2>&1 | head -1")
        checks.append(Check(f"{tool} (lab host)", PASS if rc == 0 else FAIL, _first_line(out)))
    return checks


def run_checks(settings: Settings) -> list[Check]:
    checks: list[Check] = []
    py_ok = sys.version_info >= (3, 12)
    checks.append(Check("python", PASS if py_ok else FAIL, platform.python_version()))
    for tool, required in (("uv", True), ("git", True)):
        checks.append(_tool_check(tool, required))
    gh = _tool_check("gh", required=False)
    checks.append(gh)
    if gh.status == PASS:
        rc, out = _run(["gh", "auth", "status"], timeout=20)
        checks.append(Check("gh auth", PASS if rc == 0 else WARN, _first_line(out)))
    checks.append(_tool_check("claude", required=False))
    checks.append(_tool_check("ollama", required=False))

    if platform.system() == "Windows":
        checks.extend(_wsl_checks(settings))
    else:
        checks.extend(_lab_host_checks(lambda cmd: _run(["bash", "-lc", cmd])))

    for name, port in (("twinlab", settings.twinlab_port), ("netverify", settings.netverify_port)):
        if _port_open(port):
            checks.append(Check(f"{name} port {port}", PASS, "listening"))
        else:
            checks.append(Check(f"{name} port {port}", WARN, "not listening (nettwin serve)"))
    return checks


def format_report(checks: list[Check]) -> str:
    width = max(len(c.name) for c in checks) if checks else 10
    lines = [f"{c.status:<4} {c.name:<{width}}  {c.detail}" for c in checks]
    fails = sum(c.status == FAIL for c in checks)
    warns = sum(c.status == WARN for c in checks)
    lines.append("")
    lines.append(f"{len(checks)} checks: {fails} failed, {warns} warnings")
    return "\n".join(lines)


def exit_code(checks: list[Check]) -> int:
    return 1 if any(c.status == FAIL for c in checks) else 0
