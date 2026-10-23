from __future__ import annotations

import shutil
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from calelektra.catalog.library import Library
from calelektra.procedures.ambient import AmbientReading, write_tph_workbook
from calelektra.procedures.engine import dut_role_of, with_dut
from calelektra.setups.model import ConfiguredSetup

EXAMPLE_LIBRARY = Path(__file__).resolve().parents[1] / "biblioteca_ejemplo"
TODAY = date(2026, 10, 19)
# The certificate of the E5061B (VNA-BF-01, the VNA of the LISN setup) expired on 2026-07-30:
# the tests of the LISN flow run on a day when it was still valid.
E5061B_VALID_DAY = date(2026, 7, 15)


def renew_calibration(library: Library, equipment_id: str, due: date = date(2027, 7, 30)) -> None:
    """Calibration of an equipment of the library copy valid until ``due`` (tests of flows, not of calibration dates)."""
    equipment = library.equipment[equipment_id]
    equipment.calibration.due = due
    library.save_equipment(equipment)


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


def tph_workbook(path: Path, start: datetime, end: datetime, temperature: float = 23.0, every: timedelta = timedelta(minutes=1)) -> Path:
    """Workbook of the TPH probe (layout of the laboratory export) with a reading every minute around ``start`` – ``end``."""
    readings = []
    time = start.replace(microsecond=0) - timedelta(minutes=5)
    while time <= end + timedelta(minutes=5):
        readings.append(AmbientReading(time, temperature, 942.6, 37.0 + 0.1 * (len(readings) % 3)))
        time += every
    return write_tph_workbook(path, readings, "VIRTUAL LAB: Datos sonda TPH")
