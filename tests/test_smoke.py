"""Smoke tests proving the project skeleton is wired up correctly."""

from __future__ import annotations

from pathlib import Path

import concord


def test_package_importable() -> None:
    assert concord.__version__


def test_fixtures_dir_exists(fixtures_dir: Path) -> None:
    assert fixtures_dir.exists()
    assert fixtures_dir.is_dir()
