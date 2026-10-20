"""End-to-end runs of the test templates with the simulated VNA."""

import numpy as np
import pytest
import yaml

from calelektra.core.touchstone import read_touchstone
from calelektra.instruments.dummy_vna import DummyVna
from calelektra.procedures.engine import AutoOperator, ProcedureRunner, RunListener, prepare_run
from calelektra.procedures.record import TestDefinition

from conftest import TODAY


class Recorder(RunListener):
    def __init__(self):
        self.steps, self.names = [], []

    def step_changed(self, key, status, detail=""):
        self.steps.append((key, status))

    def data(self, name, frequencies, s):
        self.names.append(name)


def run(library, procedure, setup, dut, answers=None, **kw):
    operator = AutoOperator(answers)
    listener = Recorder()
    runner = ProcedureRunner(
        library, library.procedures[procedure], library.setups[setup], operator, listener, "prueba", TODAY, seed=3, dut_id=dut, **kw
    )
    return runner.run(), operator, listener


def test_vna_only_cable_calibration_exports_s2p(library):
    result, operator, listener = run(library, "cable_parametros_s", "cable_vna_292", "CAB-DUT-0002")
    record = result.record
    assert record.status == "completado" and record.simulated
    assert record.calibration["metodo"] == "UOSM" and record.calibration["kit"] == "KIT-292-01"
    assert record.verification["correcta"]
    assert [key for key, status in listener.steps if status not in ("en_curso",)] == [
        "validacion", "conexion", "configuracion", "calibracion", "verificacion", "medida", "evaluacion", "exportacion", "cierre",
    ]
    assert record.verdict == "SIN_ESPECIFICACIONES"
    s2p = record.folder / record.files[0]
    data = read_touchstone(s2p)
    assert data.n_ports == 2 and len(data.frequencies) == 801  # no range in the template: the required range of the setup
    assert data.frequency_range.start == pytest.approx(100e3) and data.frequency_range.stop == pytest.approx(40e9)
    assert any("SIMULADOS" in line for line in data.comments)
    assert np.allclose(data.s[:, 1, 0], data.s[:, 0, 1], atol=1e-3)  # a cable is reciprocal
    # prompts name the plane, the kit and the gender of the standards (fields of the equipment)
    first = operator.requests[0].message
    assert "KIT-292-01" in first and "hembra" in first and "puerto P1" in first
    assert (record.folder / "ensayo.yaml").exists() and (record.folder / "registro.txt").exists()
    assert [t.folder for t in library.tests()] == [record.folder]
    assert record.definition.dut_id == "CAB-DUT-0002" and record.dut["id"] == "CAB-DUT-0002"


def test_tosm_with_insertable_planes(library):
    result, operator, _ = run(library, "cable_parametros_s", "componente_292_tosm", "CAB-DUT-0003")
    assert result.status == "completado"
    assert result.record.calibration["metodo"] == "TOSM"
    assert any("through plano" in p.title for p in operator.requests)


def test_lisn_impedance_per_line(library):
    result, _, listener = run(library, "impedancia_lisn", "lisn_impedancia_VNA-BF", "LISN-0001")
    record = result.record
    assert record.status == "completado"
    assert record.calibration["metodo"] == "OSM"
    assert record.sweep["modo"] == "logaritmico" and record.sweep["puntos"] == 201
    assert record.sweep["inicio"] == "9 kHz" and record.sweep["fin"] == "30 MHz"
    assert set(record.results["lineas"]) == {"L1", "N"}
    assert all(record.results["dentro_de_tolerancia"].values())
    assert sum(name.endswith(".s1p") for name in record.files) == 2
    assert listener.names == ["L1", "N"]


def test_test_range_must_be_covered_by_the_setup(library):
    result, operator, _ = run(library, "impedancia_lisn", "lisn_impedancia_VNA-01", "LISN-0001")
    assert result.status == "error" and operator.requests == []
    assert "9 kHz – 30 MHz" in result.record.message and "VNA-01" in result.record.message


def test_without_dut_the_test_is_not_run(library):
    plan = prepare_run(library, library.procedures["cable_parametros_s"], library.setups["cable_vna_292"], TODAY)
    assert not plan.ok and any("equipo a calibrar" in i.message for i in plan.issues)


def test_invalid_setup_is_not_run(library):
    result, operator, _ = run(library, "cable_parametros_s", "cable_vna_N_alarma", "CAB-DUT-0001")
    assert result.status == "error" and "familias incompatibles" in result.record.message
    assert operator.requests == []


def test_incompatible_procedure_and_vna_limits(library):
    procedure = library.procedures["cable_parametros_s"]
    plan = prepare_run(library, procedure, library.setups["lisn_impedancia_VNA-BF"], TODAY, "LISN-0001")
    assert not plan.ok and any("plantillas" in i.message for i in plan.issues)
    procedure.sweep.points = 200001
    plan = prepare_run(library, procedure, library.setups["cable_vna_292"], TODAY, "CAB-DUT-0002")
    assert any("puntos" in i.message for i in plan.issues)


def test_operator_can_stop_the_test(library):
    result, _, listener = run(library, "cable_parametros_s", "cable_vna_292", "CAB-DUT-0002", answers=lambda p: "3/" not in p.title)
    assert result.status == "abortado"
    assert ("calibracion", "abortado") in listener.steps
    assert result.record.files == ["registro.txt"]


def test_step_sweep_per_trace_settings_and_pass(library, monkeypatch):
    calls = []
    original = DummyVna.single_sweep

    def single_sweep(vna):
        calls.append((vna.if_bandwidth, vna.averages))
        return original(vna)

    monkeypatch.setattr(DummyVna, "single_sweep", single_sweep)
    result, _, _ = run(library, "cable_292_pasa_falla", "cable_vna_292", "CAB-DUT-0002")
    record = result.record
    assert record.status == "completado" and record.verdict == "PASA"
    assert record.sweep["modo"] == "lineal" and record.sweep["paso"] == "50 MHz" and record.sweep["puntos"] == 800
    assert record.sweep["trazas"]["S21"] == {"ifbw": "100 Hz", "promedios": 4, "suavizado_pct": 0.0}
    # the DUT is measured in two sweeps: S11/S22 with the settings of all traces, S21/S12 with 100 Hz and 4 averages
    assert calls[-2:] == [(1e3, 1), (100.0, 4)]
    assert [r["resultado"] for r in record.spec_results] == ["PASA", "PASA"]
    assert all(r["margen_minimo"] > 0 for r in record.spec_results)
    assert "especificaciones/cable_292_0m5_mascara.s2p" in record.files
    assert (record.folder / "especificaciones" / "cable_292_0m5_mascara.s2p").is_file()
    assert "PASA" in record.message
    data = read_touchstone(next(record.folder / f for f in record.files if f.endswith(".s2p")))
    assert data.frequencies[0] == pytest.approx(50e6) and data.frequencies[-1] == pytest.approx(40e9)
    assert any("Veredicto" in line and "PASA" in line for line in data.comments)


def test_longer_cable_fails_the_specification(library):
    result, _, listener = run(library, "cable_292_pasa_falla", "componente_292_tosm", "CAB-DUT-0003")
    record = result.record
    assert record.status == "completado" and record.verdict == "FALLA"
    insertion, return_loss = record.spec_results
    assert insertion["resultado"] == "FALLA" and insertion["margen_minimo"] < 0
    assert return_loss["resultado"] == "PASA"
    assert ("evaluacion", "aviso") in listener.steps


def test_frequency_list_and_tolerance_spec(library):
    result, _, _ = run(library, "cable_N_lista", "cable_N_18G", "CAB-DUT-0001")
    record = result.record
    assert record.status == "completado" and record.verdict == "PASA"
    assert record.sweep["modo"] == "lista" and record.sweep["puntos"] == 268
    assert record.sweep["lista"] == "listas/puntos_cable_N.csv"
    assert (record.folder / "listas" / "puntos_cable_N.csv").is_file()
    data = read_touchstone(next(record.folder / f for f in record.files if f.endswith(".s2p")))
    assert len(data.frequencies) == 268 and data.frequencies[0] == pytest.approx(30e6)


def test_prepared_test_keeps_its_files_and_runs(library, tmp_path):
    procedure = library.procedures["cable_292_pasa_falla"]
    definition = TestDefinition.from_template(procedure, "cable_vna_292", "CAB-DUT-0002")
    folder = tmp_path / "clientes" / "ACME"
    record = library.create_test(folder, "Cable ACME 1", definition, "prueba")
    assert record.status == "preparado" and record.folder == folder / "Cable_ACME_1"
    assert (record.folder / "especificaciones" / "cable_292_0m5_mascara.s2p").is_file()
    saved = yaml.safe_load((record.folder / "ensayo.yaml").read_text(encoding="utf-8"))
    assert saved["estado"] == "preparado" and saved["definicion"]["dut"] == "CAB-DUT-0002"
    # the test uses its own copy of the specification, even if the template file changes later
    (library.procedures_folder / "especificaciones" / "cable_292_0m5_mascara.s2p").unlink()
    runner = ProcedureRunner.for_test(library, library.load_test(record.folder), AutoOperator(), operator_name="prueba", today=TODAY, seed=3)
    result = runner.run()
    assert result.status == "completado" and result.record.verdict == "PASA"
    assert result.record.folder == record.folder
    assert library.tests(tmp_path)[0].verdict == "PASA"
