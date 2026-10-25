"""Uncertainty of the VNA measurements: engine (same results as the laboratory script), library, tests and charts."""

from __future__ import annotations

import json
import math
import subprocess
import sys
from datetime import date

import numpy as np
import pytest

from calelektra.core.issues import Severity
from calelektra.gui.plots import band_segments
from calelektra.gui.test_common import read_uncertainty_csv, record_uncertainty
from calelektra.procedures.engine import AutoOperator, ProcedureRunner, prepare_run
from calelektra.procedures.results import read_csv_columns
from calelektra.uncertainty import UncertaintyEngine, trace_uncertainty
from calelektra.uncertainty.impedance import impedance_uncertainty
from conftest import E5061B_VALID_DAY, EXAMPLE_LIBRARY, TODAY

FILES = EXAMPLE_LIBRARY / "catalogo" / "incertidumbres"
ZNB43 = FILES / "VNA_ZNB43_101683_uncertainties_calculation.yaml"
E5061B = FILES / "VNA_E5061B_MY49408336_uncertainties_calculation.yaml"

# expanded uncertainty given by the laboratory script DEF_vna_uncertainty_s_parameters_calculation.py
REFERENCE = [
    (ZNB43, "ZNB43", dict(parameter="S21", frequency_hz=1e9, value=-0.5, unit="dB", if_bandwidth_hz=1000), 0.04),
    (ZNB43, "ZNB43", dict(parameter="S21", frequency_hz=1e9, value=-0.5, unit="dB", if_bandwidth_hz=10000), 0.04079215610874228),
    (
        ZNB43,
        "ZNB43",
        dict(parameter="S21", frequency_hz=3.5e10, value=37.0, unit="deg", level_value=-45, level_unit="dB", if_bandwidth_hz=10000,
             temp_min_c=15, temp_max_c=31),
        1.953663225840114,
    ),
    (ZNB43, "ZNB43", dict(parameter="S11", frequency_hz=2.2e10, value=-20, unit="dB", if_bandwidth_hz=100), 0.69),
    (ZNB43, "ZNB43", dict(parameter="S22", frequency_hz=5e9, value=0.3, unit="linear", output_unit="linear"), 0.01300293610431086),
    (E5061B, "E5061B", dict(parameter="S11", frequency_hz=1e6, value=-20, unit="dB", if_bandwidth_hz=3000), 0.6949431231041369),
    (
        E5061B,
        "E5061B",
        dict(parameter="S11", frequency_hz=2e9, value=12.0, unit="deg", level_value=-30, level_unit="dB", temp_min_c=12, temp_max_c=24),
        10.024150836853963,
    ),
    (
        E5061B,
        "E5061B",
        dict(parameter="S21", frequency_hz=5e4, value=-55, unit="dB", if_bandwidth_hz=10, temp_min_c=20, temp_max_c=33),
        0.9073771725877466,
    ),
]


@pytest.mark.parametrize("path, equipment, inputs, expected", REFERENCE)
def test_engine_gives_the_results_of_the_laboratory_script(path, equipment, inputs, expected):
    result = UncertaintyEngine(path).calculate(equipment=equipment, today=TODAY, **inputs)
    assert result.expanded_uncertainty == pytest.approx(expected, rel=1e-12)
    assert not result.out_of_limits


def test_values_outside_the_ranges_give_nan_without_extrapolation():
    engine = UncertaintyEngine(ZNB43)
    result = engine.calculate("ZNB43", "S21", 1e9, 0.02, "dB", today=TODAY)  # the table of S21 ends at 0 dB
    assert math.isnan(result.expanded_uncertainty)
    assert result.out_of_limits[0]["type"] == "input_level_out_of_limits"
    assert "fuera de todos los rangos de nivel" in result.out_of_limits[0]["message"]
    beyond = engine.calculate("ZNB43", "S21", 50e9, -3, "dB", today=TODAY)
    assert math.isnan(beyond.expanded_uncertainty) and beyond.out_of_limits[0]["type"] == "equipment_frequency_out_of_limits"
    with pytest.raises(Exception, match="no coincide"):
        engine.calculate("E5061B", "S21", 1e9, -3, "dB")


def test_calibration_status_of_the_files():
    _due, valid, warnings = UncertaintyEngine(E5061B).calibration_status(TODAY)
    assert valid is False and "fuera de calibración" in warnings[0]
    assert UncertaintyEngine(E5061B).calibration_status(E5061B_VALID_DAY)[1] is True
    assert UncertaintyEngine(ZNB43).calibration_status(TODAY) == ("2028-04-21", True, [])


def test_trace_uncertainty_point_by_point():
    engine = UncertaintyEngine(ZNB43)
    f = np.array([1e9, 2e9, 3e9])
    s21 = 10 ** (np.array([-0.5, 0.02, -60.5]) / 20) * np.exp(1j * np.array([0.1, 0.2, 0.3]))
    trace = trace_uncertainty(engine, "Rohde & Schwarz ZNB43", "S21", f, s21, if_bandwidth=1000)
    assert trace.magnitude_db[0] == pytest.approx(0.04) and trace.phase_deg[0] == pytest.approx(0.4)
    assert np.isnan(trace.magnitude_db[1:]).all() and trace.missing == 2  # above 0 dB and below −60 dB
    assert "S21 a 2 GHz: nivel 0,02 dB" in trace.problems[0].replace(".", ",")
    assert trace.summary()["U_db"] == {"min": 0.04, "max": 0.04} and trace.summary()["puntos_sin_incertidumbre"] == 2


def test_library_keeps_the_uncertainty_file_of_each_vna(library):
    assert not [i for i in library.load_issues if i.code == "equipo.incertidumbre"]
    znb = library.equipment["VNA-01"]
    assert library.uncertainty_engine(znb).serial_number == "101683" == znb.serial_number
    assert library.uncertainty_designation(library.equipment["VNA-BF-01"]) == "Keysight E5061B"
    znb.serial_number = "999999"
    assert "nº de serie 101683" in library.uncertainty_problems(znb)[0][1]
    znb.uncertainty_file = "../incertidumbres/no_existe.yaml"
    assert library.uncertainty_problems(znb)[0][0] is Severity.ERROR


def test_cable_test_with_uncertainty(library):
    result = ProcedureRunner(library, library.procedures["cable_CAB-DUT-0002"], AutoOperator(), today=TODAY, seed=2).run()
    record = result.record
    assert record.status == "completado"
    # without the ambient conditions of the test the uncertainty is provisional: no temperature component
    step = next(s for s in record.steps if s.key == "incertidumbre")
    assert step.status == "aviso" and "S21 0.06 dB" in step.detail and "PROVISIONAL" in step.detail
    assert record.conditions == {"estado": "pendiente"}
    assert record.uncertainty["estado"] == "pendiente" and "incertidumbre pendiente" in record.message
    data = record.uncertainty
    assert data["k"] == 2.0 and data["documento"].startswith("VNA-ZNB43-101683")
    assert (record.folder / data["fichero"]).is_file()  # the file used is kept with the test
    traces = result.uncertainty["DUT"]
    assert list(traces) == ["S11", "S21", "S12", "S22"]
    entry = data["medidas"][0]
    csv_file = record.folder / entry["fichero"]
    assert entry["fichero_resultado"].endswith(".s2p") and csv_file.name in record.files
    read_back = read_uncertainty_csv(csv_file)
    assert np.allclose(read_back["S21"][0], traces["S21"].magnitude_db, rtol=1e-6)
    assert np.allclose(read_back["S11"][1], traces["S11"].phase_deg, rtol=1e-6)
    assert set(record_uncertainty(record)) == {"DUT"}
    header = (record.folder / next(f for f in record.files if f.endswith(".csv") and "incertidumbre" not in f)).read_text(encoding="utf-8-sig")
    assert "U S21 (dB, k=2)" in header.splitlines()[0]
    s2p = (record.folder / entry["fichero_resultado"]).read_text()
    assert "Incertidumbre expandida (k=2)" in s2p


def test_uncertainty_off_or_without_file(library):
    template = library.procedures["cable_CAB-DUT-0002"].model_copy(deep=True)
    template.uncertainty.active = False
    record = ProcedureRunner(library, template, AutoOperator(), today=TODAY, seed=2).run().record
    assert next(s for s in record.steps if s.key == "incertidumbre").status == "omitido"
    assert not record.uncertainty and not [f for f in record.files if "incertidumbre" in f]

    vna = library.equipment["VNA-01"]
    vna.uncertainty_file = None
    plan = prepare_run(library, library.procedures["cable_CAB-DUT-0002"], TODAY)
    assert any("no tiene fichero de incertidumbres" in i.message for i in plan.issues if i.severity is Severity.WARNING)


def test_expired_e5061b_blocks_the_lisn_test_and_its_file_gives_the_uncertainty(library):
    plan = prepare_run(library, library.procedures["lisn_LISN-0001"], TODAY)
    assert any("VNA-BF-01" in i.message and "caducó" in i.message for i in plan.issues if i.severity is Severity.ERROR)
    result = ProcedureRunner(library, library.procedures["lisn_LISN-0001"], AutoOperator(), today=E5061B_VALID_DAY, seed=1).run()
    record = result.record
    assert record.status == "completado"
    assert record.uncertainty["vna"].startswith("VNA-BF-01 — Keysight E5061B nº MY49408336")
    assert set(result.uncertainty) == {"DUT"} and set(record_uncertainty(record)) == {"DUT"}
    # the impedance: its uncertainty from that of S11 (Z0 = 50 Ω), in the record and in both CSV files
    impedance = result.impedance["DUT"]
    [entry] = record.uncertainty["medidas"]
    summary = entry["impedancia"]
    assert summary["z0_ohm"] == 50.0 and summary["puntos"] == len(result.frequencies)
    assert 0.3 < summary["U_ohm"]["max"] < 2 and summary["U_grados"]["max"] < 10
    gamma = result.data["DUT"][:, 0, 0]
    trace = result.uncertainty["DUT"]["S11"]
    expected = impedance_uncertainty(gamma, trace.magnitude_db, trace.phase_deg, "dB")
    assert np.allclose(impedance.magnitude_plus, expected.magnitude_plus, equal_nan=True)
    header, columns = read_csv_columns(record.folder / entry["fichero"])
    assert "U+ |Z| (Ω, k=2)" in header and "U− ∠Z (°, k=2)" in header and "Z: cota exacta (1 = sí)" in header
    assert np.allclose(columns[header.index("U+ |Z| (Ω, k=2)")], impedance.magnitude_plus, rtol=1e-6, equal_nan=True)
    results_csv = record.folder / entry["fichero_resultado"].replace(".s1p", ".csv")
    assert "U+ |Z| (Ω, k=2)" in read_csv_columns(results_csv)[0]
    assert "|Z|" in next(step.detail for step in record.steps if step.key == "incertidumbre")


def test_band_segments_break_at_gaps_and_phase_jumps():
    y = np.array([0.0, 1, 2, 3, 4])
    u = np.array([0.1, 0.1, np.nan, 0.1, 0.1])
    assert band_segments(y, u) == [slice(0, 2), slice(3, 5)]
    phase = np.array([170.0, 178, -176, -170, -160])
    assert band_segments(phase, np.full(5, 1.0), wrap=True) == [slice(0, 2), slice(2, 5)]
    assert band_segments(y, np.full(5, np.nan)) == []


def test_command_line_like_the_laboratory_script():
    output = subprocess.run(
        [sys.executable, "-m", "calelektra.uncertainty", "--yaml", str(ZNB43), "--equipment", "ZNB43", "--parameter", "S21",
         "--frequency-hz", "1e9", "--value", "-0.5", "--unit", "dB", "--if-bandwidth-hz", "1000"],
        capture_output=True, text=True, check=True,
    ).stdout
    result = json.loads(output)
    assert result["expanded_uncertainty"] == pytest.approx(0.04)
    assert [c["source_id"] for c in result["contributions"]] == ["DS_ZNB43_TRANS_DB"]
    assert date.fromisoformat(result["calculation_date"])
