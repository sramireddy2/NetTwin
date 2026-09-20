"""Environment configuration shared by the CLI and both servers."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

TRUE_VALUES = {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    state_dir: Path
    lab_name: str
    wsl_distro: str
    twinlab_port: int
    netverify_port: int
    bench: bool
    admin_token: str | None
    topology_path: Path

    @property
    def snapshots_dir(self) -> Path:
        return self.state_dir / "snapshots"

    @property
    def exports_dir(self) -> Path:
        return self.state_dir / "exports"

    @property
    def lab_run_dir(self) -> Path:
        return self.state_dir / "lab-run"

    @property
    def attest_key_path(self) -> Path:
        return self.state_dir / "attest.key"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        env = os.environ if env is None else env
        return cls(
            state_dir=Path(env.get("NETTWIN_STATE_DIR", "~/.nettwin")).expanduser(),
            lab_name=env.get("NETTWIN_LAB_NAME", "nettwin"),
            wsl_distro=env.get("NETTWIN_WSL_DISTRO", "Containerlab"),
            twinlab_port=int(env.get("NETTWIN_TWINLAB_PORT", "8001")),
            netverify_port=int(env.get("NETTWIN_NETVERIFY_PORT", "8002")),
            bench=env.get("NETTWIN_BENCH", "").strip().lower() in TRUE_VALUES,
            admin_token=env.get("NETTWIN_ADMIN_TOKEN") or None,
            topology_path=Path(env.get("NETTWIN_TOPOLOGY", "lab/topology.clab.yml")),
        )

    def ensure_dirs(self) -> None:
        for directory in (self.state_dir, self.snapshots_dir, self.exports_dir, self.lab_run_dir):
            directory.mkdir(parents=True, exist_ok=True)
