from __future__ import annotations

import shutil
from datetime import date
from pathlib import Path

import pytest

from calelektra.catalog.library import Library
from calelektra.procedures.engine import dut_role_of, with_dut
from calelektra.setups.model import ConfiguredSetup

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


def setup_with_dut(library: Library, setup_id: str, dut_id: str) -> ConfiguredSetup:
    """The setup as a test sees it: with the cable to calibrate of the test in its DUT role."""
    setup = library.setups[setup_id]
    return with_dut(setup, dut_role_of(library.resolved_template(setup.template)), dut_id)
