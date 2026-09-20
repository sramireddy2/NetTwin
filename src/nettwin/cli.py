"""`nettwin` command line interface.

Windows is the editing and orchestration side; the lab and the MCP servers run inside a
WSL2 distro. `nettwin lab <target>` forwards a Makefile target into that distro.
"""

from __future__ import annotations

import platform
import shlex
import subprocess
from pathlib import Path

import typer

from nettwin import doctor as doctor_mod
from nettwin_core.settings import Settings

app = typer.Typer(
    no_args_is_help=True,
    help="NetTwin: agentic network troubleshooting inside a digital twin.",
)
lab_app = typer.Typer(
    no_args_is_help=True, help="Drive the containerlab lab (runs inside WSL on Windows)."
)
app.add_typer(lab_app, name="lab")


def repo_root(start: Path | None = None) -> Path:
    """Walk up from `start` (default cwd) to the directory holding lab/ and pyproject.toml."""
    here = (start or Path.cwd()).resolve()
    for candidate in (here, *here.parents):
        if (candidate / "pyproject.toml").is_file() and (candidate / "lab").is_dir():
            return candidate
    return here


def to_wsl_path(path: Path) -> str:
    """C:\\dev\\NetTwin -> /mnt/c/dev/NetTwin. Non-Windows paths are returned as POSIX."""
    resolved = path.resolve()
    if not resolved.drive:
        return resolved.as_posix()
    drive = resolved.drive[0].lower()
    rest = resolved.as_posix()[len(resolved.drive) :]
    return f"/mnt/{drive}{rest}"


def make_command(target: str, extra: list[str], root: Path, settings: Settings) -> list[str]:
    """Build the argv that runs `make -C lab <target>` on the lab host."""
    if platform.system() == "Windows":
        inner = f"cd {shlex.quote(to_wsl_path(root))} && make -C lab {shlex.join([target, *extra])}"
        return ["wsl.exe", "-d", settings.wsl_distro, "--", "bash", "-lc", inner]
    return ["make", "-C", str(root / "lab"), target, *extra]


def _run_make(target: str, extra: list[str] | None = None) -> None:
    settings = Settings.from_env()
    cmd = make_command(target, extra or [], repo_root(), settings)
    raise typer.Exit(subprocess.call(cmd))


@app.command()
def doctor() -> None:
    """Check every prerequisite: WSL distro, docker engine, containerlab, ports, CLIs."""
    settings = Settings.from_env()
    checks = doctor_mod.run_checks(settings)
    typer.echo(doctor_mod.format_report(checks))
    raise typer.Exit(doctor_mod.exit_code(checks))


@lab_app.command("up")
def lab_up() -> None:
    """Build the image if needed and deploy the reference topology."""
    _run_make("up")


@lab_app.command("down")
def lab_down() -> None:
    """Destroy the lab."""
    _run_make("down")


@lab_app.command("golden")
def lab_golden() -> None:
    """Re-apply the golden configuration and kernel state on every node."""
    _run_make("golden")


@lab_app.command("check")
def lab_check() -> None:
    """Run the golden reachability check from every host."""
    _run_make("check")


@lab_app.command("test")
def lab_test() -> None:
    """Run the live-lab pytest suite inside WSL."""
    _run_make("test-lab")


@lab_app.command("inject")
def lab_inject(scenario: str) -> None:
    """Inject a NetBench fault scenario through the twinlab admin route (milestone M3)."""
    typer.echo(f"inject {scenario}: available from milestone M3", err=True)
    raise typer.Exit(2)


@app.command()
def serve() -> None:
    """Start the MCP servers inside WSL and stay attached (Ctrl-C stops them).

    Staying attached also keeps the WSL2 VM, and with it the containerlab links, alive.
    """
    _run_make("serve")


@app.command("stop-serve")
def stop_serve() -> None:
    """Stop the MCP servers inside WSL."""
    _run_make("stop-serve")


@app.command()
def approve(change_id: str) -> None:
    """Approve an export outside of MCP elicitation (milestone M5)."""
    typer.echo(f"approve {change_id}: available from milestone M5", err=True)
    raise typer.Exit(2)


if __name__ == "__main__":
    app()
