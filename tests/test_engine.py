"""End-to-end runs of the measurement procedures with the simulated VNA."""

import numpy as np
import pytest

from calelektra.core.touchstone import read_touchstone
from calelektra.procedures.engine import AutoOperator, ProcedureRunner, RunListener, prepare_run

from conftest import TODAY


class Recorder(RunListener):
    def __init__(self):
        self.steps, self.names = [], []

    def step_changed(self, key, status, detail=""):
        self.steps.append((key, status))

    def data(self, name, frequencies, s):
        self.names.append(name)


def run(library, procedure, setup, answers=None):
    operator = AutoOperator(answers)
    listener = Recorder()
    runner = ProcedureRunner(
        library, library.procedures[procedure], library.setups[setup], operator, listener, "prueba", TODAY, seed=3
    )
    return runner.run(), operator, listener


def test_vna_only_cable_calibration_exports_s2p(library):
    result, operator, listener = run(library, "cable_parametros_s", "cable_vna_292")
    record = result.record
    assert record.status == "completado" and record.simulated
    assert record.calibration["metodo"] == "UOSM" and record.calibration["kit"] == "KIT-292-01"
    assert record.verification["correcta"]
    assert [key for key, status in listener.steps if status not in ("en_curso",)] == [
        "validacion", "conexion", "configuracion", "calibracion", "verificacion", "medida", "exportacion", "cierre",
    ]
    s2p = record.folder / record.files[0]
    data = read_touchstone(s2p)
    assert data.n_ports == 2 and len(data.frequencies) == 801
    assert data.frequency_range.start == pytest.approx(100e3) and data.frequency_range.stop == pytest.approx(40e9)
    assert any("SIMULADOS" in line for line in data.comments)
    assert np.allclose(data.s[:, 1, 0], data.s[:, 0, 1], atol=1e-3)  # a cable is reciprocal
    # prompts name the plane, the kit and the gender of the standards (fields of the equipment)
    first = operator.requests[0].message
    assert "KIT-292-01" in first and "hembra" in first and "puerto P1" in first
    assert (record.folder / "ensayo.yaml").exists() and (record.folder / "registro.txt").exists()
    assert [t.id for t in library.tests()] == [record.id]


def test_tosm_with_insertable_planes(library):
    result, operator, _ = run(library, "cable_parametros_s", "componente_292_tosm")
    assert result.status == "completado"
    assert result.record.calibration["metodo"] == "TOSM"
    assert any("through plano" in p.title for p in operator.requests)


def test_lisn_impedance_per_line(library):
    result, _, listener = run(library, "impedancia_lisn", "lisn_impedancia_VNA-BF")
    record = result.record
    assert record.status == "completado"
    assert record.calibration["metodo"] == "OSM"
    assert set(record.results["lineas"]) == {"L1", "N"}
    assert all(record.results["dentro_de_tolerancia"].values())
    assert sum(name.endswith(".s1p") for name in record.files) == 2
    assert listener.names == ["L1", "N"]


def test_invalid_setup_is_not_run(library):
    result, operator, _ = run(library, "cable_parametros_s", "cable_vna_N_alarma")
    assert result.status == "error" and "familias incompatibles" in result.record.message
    assert operator.requests == []


def test_incompatible_procedure_and_vna_limits(library):
    procedure = library.procedures["cable_parametros_s"]
    plan = prepare_run(library, procedure, library.setups["lisn_impedancia_VNA-BF"], TODAY)
    assert not plan.ok and any("plantillas" in i.message for i in plan.issues)
    procedure.sweep.points = 200001
    plan = prepare_run(library, procedure, library.setups["cable_vna_292"], TODAY)
    assert any("puntos" in i.message for i in plan.issues)


def test_operator_can_stop_the_test(library):
    result, _, listener = run(library, "cable_parametros_s", "cable_vna_292", answers=lambda p: "3/" not in p.title)
    assert result.status == "abortado"
    assert ("calibracion", "abortado") in listener.steps
    assert result.record.files == ["registro.txt"]
