"""End-to-end launches of the test templates with the simulated VNA."""

import numpy as np
import pytest

from calelektra.core.touchstone import read_touchstone
from calelektra.instruments.dummy_vna import DummyVna
from calelektra.procedures.engine import AutoOperator, ProcedureRunner, RunListener, prepare_run, suitable_setups

from conftest import E5061B_VALID_DAY, TODAY


class Recorder(RunListener):
    def __init__(self):
        self.steps, self.names = [], []

    def step_changed(self, key, status, detail=""):
        self.steps.append((key, status))

    def data(self, name, frequencies, s):
        self.names.append(name)


def run(library, procedure, answers=None, today=TODAY, **kw):
    operator = AutoOperator(answers)
    listener = Recorder()
    template = library.procedures[procedure] if isinstance(procedure, str) else procedure
    runner = ProcedureRunner(library, template, operator, listener, "prueba", today, seed=3, **kw)
    return runner.run(), operator, listener


def template_with(library, procedure_id, **changes):
    return library.procedures[procedure_id].model_copy(deep=True, update=changes)


def test_vna_only_cable_calibration_exports_s2p(library):
    result, operator, listener = run(library, "cable_CAB-DUT-0002")
    record = result.record
    assert record.status == "completado" and record.simulated and record.verdict == "PASA"
    assert record.calibration["metodo"] == "UOSM" and record.calibration["kit"] == "KIT-292-01"
    assert record.verification["correcta"]
    assert [key for key, status in listener.steps if status not in ("en_curso",)] == [
        "validacion", "conexion", "configuracion", "calibracion", "verificacion", "medida", "incertidumbre", "evaluacion", "exportacion", "cierre",
    ]
    s2p = record.folder / record.files[0]
    data = read_touchstone(s2p)
    # the frequencies are those of the hardware setup: 50 MHz – 40 GHz, step 50 MHz
    assert data.n_ports == 2 and len(data.frequencies) == 800
    assert data.frequencies[0] == pytest.approx(50e6) and data.frequencies[-1] == pytest.approx(40e9)
    assert any("SIMULADOS" in line for line in data.comments)
    assert any("sin suavizar" in line for line in data.comments)
    assert np.allclose(data.s[:, 1, 0], data.s[:, 0, 1], atol=1e-3)  # a cable is reciprocal
    # prompts name the plane, the kit and the gender of the standards (fields of the equipment)
    first = operator.requests[0].message
    assert "KIT-292-01" in first and "hembra" in first and "puerto P1" in first
    assert (record.folder / "ensayo.yaml").exists() and (record.folder / "registro.txt").exists()
    assert [t.folder for t in library.tests()] == [record.folder]
    # the record keeps the template as launched, the setup with its frequencies and the EUT
    assert record.template["hardware_setup"] == "cable_vna_292" and record.template["eut"] == "CAB-DUT-0002"
    assert record.setup["frecuencias"] == {"modo": "lineal", "definicion": "paso", "puntos": 801, "paso": "50 MHz"}
    assert record.dut["id"] == "CAB-DUT-0002" and record.dut_id == "CAB-DUT-0002"


def test_per_trace_settings_and_unsmoothed_data(library, monkeypatch):
    calls = []
    original = DummyVna.single_sweep

    def single_sweep(vna):
        calls.append((vna.if_bandwidth, vna.averages))
        return original(vna)

    monkeypatch.setattr(DummyVna, "single_sweep", single_sweep)
    result, _, _ = run(library, "cable_CAB-DUT-0002")
    record = result.record
    # the DUT is measured in two sweeps: S11/S22 with the settings of all traces, S21/S12 with 100 Hz and 4 averages
    assert calls[-2:] == [(1e3, 1), (100.0, 4)]
    assert record.sweep["trazas"]["S11"] == {"ifbw": "1 kHz", "promedios": 1, "suavizado_visualizacion_pct": 1.0}
    # smoothing is only for display: the exported S11 is the measured one
    data = read_touchstone(next(record.folder / f for f in record.files if f.endswith(".s2p")))
    assert np.allclose(data.s, result.data["DUT"], rtol=1e-6, atol=1e-9)


def test_specifications_of_the_eut_model(library):
    result, _, _ = run(library, "cable_CAB-DUT-0002")
    record = result.record
    assert [r["resultado"] for r in record.spec_results] == ["PASA", "PASA"]
    assert all(r["margen_minimo"] > 0 for r in record.spec_results)
    assert [s["fichero"] for s in record.specifications] == ["especificaciones/cable_292_0m5_mascara.s2p"] * 2
    assert (record.folder / "especificaciones" / "cable_292_0m5_mascara.s2p").is_file()
    assert "especificaciones/cable_292_0m5_mascara.s2p" in record.files
    data = read_touchstone(next(record.folder / f for f in record.files if f.endswith(".s2p")))
    assert any("Veredicto" in line and "PASA" in line for line in data.comments)


def test_longer_cable_fails_the_specification_of_its_model(library):
    result, _, listener = run(library, "cable_CAB-DUT-0003")
    record = result.record
    assert record.status == "completado" and record.verdict == "FALLA"
    assert record.calibration["metodo"] == "TOSM"
    insertion, return_loss = record.spec_results
    assert insertion["resultado"] == "FALLA" and insertion["margen_minimo"] < 0
    assert insertion["parametros"][0]["parametro"] == "S21" and insertion["parametros"][0]["fallos"] > 0
    assert return_loss["resultado"] == "PASA"
    assert ("evaluacion", "aviso") in listener.steps


def test_frequency_list_of_the_setup_and_tolerance_spec(library):
    result, _, _ = run(library, "cable_CAB-DUT-0001")
    record = result.record
    assert record.status == "completado" and record.verdict == "PASA"
    assert record.sweep["modo"] == "lista" and record.sweep["puntos"] == 268
    assert record.sweep["lista"] == "listas/puntos_cable_N.csv"
    assert (record.folder / "listas" / "puntos_cable_N.csv").is_file()
    data = read_touchstone(next(record.folder / f for f in record.files if f.endswith(".s2p")))
    assert len(data.frequencies) == 268 and data.frequencies[0] == pytest.approx(30e6)


def test_lisn_impedance_per_line(library):
    result, _, listener = run(library, "lisn_LISN-0001", today=E5061B_VALID_DAY)
    record = result.record
    assert record.status == "completado" and record.verdict == "SIN_ESPECIFICACIONES"
    assert record.calibration["metodo"] == "OSM"
    assert record.sweep["modo"] == "logaritmico" and record.sweep["puntos"] == 201
    assert record.sweep["inicio"] == "9 kHz" and record.sweep["fin"] == "30 MHz"
    assert set(record.results["lineas"]) == {"L1", "N"}
    assert all(record.results["dentro_de_tolerancia"].values())
    assert sum(name.endswith(".s1p") for name in record.files) == 2
    assert listener.names == ["L1", "N"]


def test_each_launch_is_a_new_record_in_the_chosen_folder(library):
    folder = library.tests_folder / "2026" / "Cliente"
    first, _, _ = run(library, "cable_CAB-DUT-0002", folder=folder, name="Cable 2001")
    second, _, _ = run(library, "cable_CAB-DUT-0002", folder=folder, name="Cable 2001")
    assert first.record.folder == folder / "Cable_2001" and second.record.folder == folder / "Cable_2001_2"
    assert {t.folder for t in library.tests(folder)} == {first.record.folder, second.record.folder}
    assert library.load_test(first.record.folder).template_id == "cable_CAB-DUT-0002"


def test_setup_range_must_be_covered(library):
    template = template_with(library, "lisn_LISN-0001", setup_id="lisn_impedancia_VNA-01")
    result, operator, _ = run(library, template)
    assert result.status == "error" and operator.requests == []
    assert "no cubre el rango requerido (9 kHz – 30 MHz)" in result.record.message


def test_template_needs_setup_and_eut(library):
    plan = prepare_run(library, template_with(library, "cable_CAB-DUT-0002", eut_id=None), TODAY)
    assert not plan.ok and any("EUT" in i.message for i in plan.issues)
    plan = prepare_run(library, template_with(library, "cable_CAB-DUT-0002", setup_id=None), TODAY)
    assert not plan.ok and any("hardware setup" in i.message for i in plan.issues)
    plan = prepare_run(library, template_with(library, "cable_CAB-DUT-0002", setup_id="no_existe"), TODAY)
    assert not plan.ok and any("no_existe" in i.message for i in plan.issues)


def test_invalid_eut_for_the_setup_is_not_run(library):
    template = template_with(library, "cable_CAB-DUT-0002", setup_id="cable_vna_N_alarma", eut_id="CAB-DUT-0001")
    result, operator, _ = run(library, template)
    assert result.status == "error" and "familias incompatibles" in result.record.message
    assert operator.requests == []


def test_algorithm_and_vna_limits(library):
    assert {s.id for s in suitable_setups(library, "vna_1_puerto")} == {"lisn_impedancia_VNA-01", "lisn_impedancia_VNA-BF"}
    assert "cable_vna_292" in {s.id for s in suitable_setups(library, "vna_2_puertos")}
    plan = prepare_run(library, template_with(library, "cable_CAB-DUT-0002", setup_id="lisn_impedancia_VNA-BF"), TODAY)
    assert not plan.ok and any("plano(s) de referencia" in i.message for i in plan.issues)
    template = library.procedures["cable_CAB-DUT-0002"].model_copy(deep=True)
    template.sweep.traces["S21"].if_bandwidth = 1e9
    plan = prepare_run(library, template, TODAY)
    assert any("IFBW" in i.message and "S21" in i.message for i in plan.issues)


def test_operator_can_stop_the_test(library):
    result, _, listener = run(library, "cable_CAB-DUT-0002", answers=lambda p: "3/" not in p.title)
    assert result.status == "abortado"
    assert ("calibracion", "abortado") in listener.steps
    assert result.record.files == ["registro.txt"]
