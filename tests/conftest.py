from __future__ import annotations

import os
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session")
def fixtures() -> Path:
    return FIXTURES


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if os.environ.get("NETTWIN_LAB", "").strip().lower() in {"1", "true", "yes", "on"}:
        return
    skip = pytest.mark.skip(reason="needs the live lab; set NETTWIN_LAB=1 inside WSL")
    for item in items:
        if "lab" in item.keywords:
            item.add_marker(skip)
