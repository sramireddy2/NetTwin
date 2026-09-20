"""Append-only store of applied changes, keyed by change id."""

from __future__ import annotations

from pathlib import Path

from nettwin_core.models import ChangeResult


class ChangeStore:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True)

    def path(self, change_id: str) -> Path:
        return self.directory / f"{change_id}.json"

    def save(self, change: ChangeResult) -> Path:
        path = self.path(change.change_id)
        path.write_text(change.model_dump_json(indent=1), encoding="utf-8")
        return path

    def load(self, change_id: str) -> ChangeResult:
        path = self.path(change_id)
        if not path.exists():
            raise KeyError(f"unknown change {change_id!r}")
        return ChangeResult.model_validate_json(path.read_text(encoding="utf-8"))

    def ids(self) -> list[str]:
        paths = sorted(self.directory.glob("*.json"), key=lambda p: p.stat().st_mtime)
        return [p.stem for p in paths]
