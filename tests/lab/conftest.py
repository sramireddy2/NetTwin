"""Fixtures for tests that need the live lab. Run inside WSL with NETTWIN_LAB=1."""

from __future__ import annotations

import pytest

from nettwin_core.executor import DockerExecutor
from nettwin_core.settings import Settings


@pytest.fixture(scope="session")
def settings() -> Settings:
    return Settings.from_env()


@pytest.fixture(scope="session")
def executor(settings: Settings) -> DockerExecutor:
    return DockerExecutor(settings.lab_name)
