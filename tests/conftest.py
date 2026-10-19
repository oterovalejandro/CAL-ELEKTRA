from __future__ import annotations

import shutil
from datetime import date
from pathlib import Path

import pytest

from calelektra.catalog.library import Library

EXAMPLE_LIBRARY = Path(__file__).resolve().parents[1] / "biblioteca_ejemplo"
TODAY = date(2026, 10, 19)


@pytest.fixture
def library_copy(tmp_path: Path) -> Path:
    """Writable copy of the example library."""
    target = tmp_path / "biblioteca"
    shutil.copytree(EXAMPLE_LIBRARY, target)
    return target


@pytest.fixture
def library(library_copy: Path) -> Library:
    return Library.load(library_copy)
