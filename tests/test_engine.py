"""End-to-end launches of the test templates with the simulated VNA."""

import numpy as np
import pytest
import yaml

from calelektra.core.touchstone import read_touchstone
from calelektra.instruments.dummy_vna import DummyVna
from calelektra.procedures.coupler import (
    coupler_quantities,
    pair_transmission_losses,
    port_return_losses,
    return_loss_sources,
    transmission_symbol,
)
from calelektra.procedures.engine import AutoOperator, ProcedureRunner, RunListener, prepare_run, suitable_setups
from calelektra.procedures.results import read_csv_columns

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
    assert {"hardware_setup.yaml", "plantilla_ensayo.yaml"} <= set(record.files)  # as launched
    s2p = record.folder / next(f for f in record.files if f.endswith(".s2p"))
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


def test_impedance_in_one_port_at_the_end_of_the_path(library):
    result, operator, listener = run(library, "lisn_LISN-0001", today=E5061B_VALID_DAY)
    record = result.record
    assert record.status == "completado" and record.verdict == "SIN_ESPECIFICACIONES"
    assert record.calibration["metodo"] == "OSM" and record.calibration["planos"] == {"PR1": "Terminales / bornas (no coaxial)"}
    assert record.sweep["modo"] == "logaritmico" and record.sweep["puntos"] == 201
    assert record.sweep["inicio"] == "9 kHz" and record.sweep["fin"] == "30 MHz"
    assert set(record.results) == {"perdidas_retorno_min_db", "z_min_ohm", "z_max_ohm"}  # generic: no LISN lines or nominal network
    assert sum(name.endswith(".s1p") for name in record.files) == 1 and listener.names == ["DUT"]
    assert record.measurements == [{"nombre": "DUT", "fichero": record.files[0], "formato": "touchstone", "puertos_eut": {"P1": "EUT"}}]
    [path] = record.paths
    assert [e["id"] for e in path["elementos"]] == ["CAB-PT-BF-01", "ADP-LISN-01"] and path["plano"] == "PR1"
    prompt = next(r for r in operator.requests if r.step == "medida")
    assert "EUT (Terminales / bornas (no coaxial)) en PR1 (extremo B de «ADP-LISN-01»" in prompt.message


def test_record_keeps_the_paths_of_the_setup(library):
    result, _, _ = run(library, "cable_CAB-DUT-0003")
    record = result.record
    assert record.setup["caminos"] == {"P1": ["CAB-PT-292-01"], "P2": ["CAB-PT-292-02"]}
    assert record.setup["conectores_dut_admitidos"] == "PR1: 2,92 mm (K) hembra; PR2: 2,92 mm (K) macho"
    first, second = record.paths
    assert first["conector_puerto_vna"] == "2,92 mm (K) macho" and first["conector_plano"] == "2,92 mm (K) macho"
    [element] = first["elementos"]
    assert element["id"] == "CAB-PT-292-01" and element["lado_vna"] == "A" and element["lado_dut"] == "B"
    assert element["datos_s"] == "CAB-PT-292-01.s2p" and second["conector_dut_admitido"] == "2,92 mm (K) macho"
    cable = next(e for e in record.equipment if e["id"] == "CAB-PT-292-02")
    assert cable["rol"] == "P2-1" and cable["camino"] == "P2" and cable["posicion"] == 1 and cable["rol_nombre"] == "Camino P2 · 1"
    snapshot = yaml.safe_load((record.folder / "hardware_setup.yaml").read_text(encoding="utf-8"))
    assert snapshot["formato"] == "calelektra/hardware_setup_ensayo@0.4" and snapshot["setup"]["caminos"]["P1"] == ["CAB-PT-292-01"]
    assert snapshot["caminos"][1]["elementos"][0]["id"] == "CAB-PT-292-02"
    data = read_touchstone(record.folder / record.measurements[0]["fichero"])
    assert any(line.startswith("Camino P1: P1 (2,92 mm (K) macho) → CAB-PT-292-01 (A→B) → PR1") for line in data.comments)


def test_eut_ports_on_each_path_can_be_chosen(library):
    template = template_with(library, "cable_CAB-DUT-0003", eut_ports=["B", "A"])
    plan = prepare_run(library, template, TODAY)
    assert not plan.ok  # B (male) cannot go on PR1 (male)
    assert any("mismo género" in i.message for i in plan.issues)
    template = template_with(library, "cable_CAB-DUT-0003", eut_ports=["C"])
    assert any("no tiene los puertos C" in i.message for i in prepare_run(library, template, TODAY).issues)


def test_transmission_with_normalization_exports_s21_to_csv(library):
    result, operator, listener = run(library, "transmision_CAB-DUT-0001")
    record = result.record
    assert record.status == "completado" and record.calibration["metodo"] == "normalizacion"
    assert record.calibration["through"] == "ADP-NN-01" and record.calibration["kit"] is None
    titles = [r.title for r in operator.requests]
    assert titles[0] == "Normalización de transmisión: through «ADP-NN-01»" and not any("Abierto" in title for title in titles)
    assert record.verification["comprobaciones"][0]["comprobacion"] == "|S21| del through normalizado"
    [measurement] = record.measurements
    assert measurement["formato"] == "csv_s21" and measurement["fichero"].endswith("_S21.csv")
    assert not [f for f in record.files if "/" not in f and f.endswith((".s2p", ".s1p"))]  # no Touchstone result
    header, columns = read_csv_columns(record.folder / measurement["fichero"])
    assert header == ["Frecuencia (Hz)", "S21 (dB)", "S21 (°)", "U S21 (dB, k=2)", "U S21 (°, k=2)"] and len(columns[0]) == 401
    s = result.data["DUT"]
    assert np.all(np.isfinite(s[:, 1, 0])) and np.all(np.isnan(s[:, 0, 0]))  # only S21 is measured
    assert list(result.uncertainty["DUT"]) == ["S21"]
    assert any("normalización de transmisión" in warning for warning in record.uncertainty["avisos"])
    assert [entry["parametros"][0]["parametro"] for entry in record.spec_results] == ["S21"]  # the S12 of the spec is not evaluated
    assert set(record.results) == {"perdidas_insercion_db", "retardo_ns"}


def test_directional_coupler_by_port_pairs(library):
    result, operator, listener = run(library, "acoplador_ACP-0001")
    record = result.record
    assert record.status == "completado" and record.verdict == "PASA"
    assert listener.names[-3:] == ["ENTRADA-SALIDA", "ENTRADA-ACOPLADO", "SALIDA-ACOPLADO"]
    prompts = [r for r in operator.requests if r.step == "medida"]
    assert [p.title for p in prompts] == ["Par ENTRADA – SALIDA (1/3)", "Par ENTRADA – ACOPLADO (2/3)", "Par SALIDA – ACOPLADO (3/3)"]
    assert "ACOPLADO (N 50 Ω hembra) en «LOAD-N-01»" in prompts[0].message and "SALIDA (N 50 Ω hembra) en PR2" in prompts[0].message
    assert [m["puertos_eut"] for m in record.measurements] == [
        {"P1": "ENTRADA", "P2": "SALIDA"}, {"P1": "ENTRADA", "P2": "ACOPLADO"}, {"P1": "SALIDA", "P2": "ACOPLADO"},
    ]
    assert record.measurements[0]["puertos_en_carga"] == ["ACOPLADO", "AISLADO"]
    assert sum(name.endswith(".s2p") for name in record.files) == 3
    entries = {entry["nombre"]: entry for entry in record.spec_results}
    assert set(entries) == {"Pérdidas de inserción", "Acoplamiento", "Aislamiento", "Directividad", "Pérdidas de retorno"}
    assert entries["Acoplamiento"]["criterio"].startswith("20 ± 1 dB") and all(e["resultado"] == "PASA" for e in entries.values())
    coupling = record.results["acoplamiento_db"]
    assert 19 <= coupling["min"] <= coupling["max"] <= 21
    header, _ = read_csv_columns(record.folder / next(f for f in record.files if f.endswith("_acoplador.csv")))
    assert header[1:] == [
        "Pérdidas de inserción IL (dB)", "Acoplamiento C (dB)", "Aislamiento I (dB)", "Directividad D (dB)", "Pérdidas de retorno RL (dB)",
        "RL ENTRADA (dB)", "RL SALIDA (dB)", "RL ACOPLADO (dB)",
    ]
    assert {m["medida"] for m in record.uncertainty["medidas"]} == {"ENTRADA-SALIDA", "ENTRADA-ACOPLADO", "SALIDA-ACOPLADO"}


def test_return_loss_of_each_coupler_port_comes_from_one_pair():
    names = ["ENTRADA-ACOPLADO", "SALIDA-ACOPLADO", "SALIDA-ENTRADA", "ACOPLADO-AISLADO"]
    # ENTRADA and SALIDA are in two pairs each: their RL comes from the pair between them, whatever its order
    assert return_loss_sources(names) == {
        "ENTRADA": ("SALIDA-ENTRADA", 1), "SALIDA": ("SALIDA-ENTRADA", 0), "ACOPLADO": ("ENTRADA-ACOPLADO", 1), "AISLADO": ("ACOPLADO-AISLADO", 1),
    }
    assert return_loss_sources(["ENTRADA-ACOPLADO", "SALIDA-ACOPLADO"])["ENTRADA"] == ("ENTRADA-ACOPLADO", 0)  # without that pair
    f = np.linspace(1e9, 2e9, 3)
    data = {name: np.zeros((len(f), 2, 2), dtype=complex) for name in names}
    data["SALIDA-ENTRADA"][:, 0, 0], data["SALIDA-ENTRADA"][:, 1, 1] = 0.1, 0.01  # RL 20 dB at SALIDA, 40 dB at ENTRADA
    data["ENTRADA-ACOPLADO"][:, 0, 0] = 0.5  # the other measurement of ENTRADA does not count
    data["SALIDA-ENTRADA"][:, 1, 0] = 0.9
    losses = port_return_losses(data)
    assert np.allclose(losses["ENTRADA"], 40) and np.allclose(losses["SALIDA"], 20)
    quantities = {q.symbol: q.values for q in coupler_quantities(data)}
    assert np.allclose(quantities["RL"], 20)  # the worst of ENTRADA and SALIDA
    assert np.allclose(pair_transmission_losses(data)["SALIDA-ENTRADA"], -20 * np.log10(0.9))
    assert transmission_symbol("SALIDA-ENTRADA") == "IL" and transmission_symbol("ACOPLADO-AISLADO") is None


def test_coupler_pairs_are_checked(library):
    template = template_with(library, "acoplador_ACP-0001", pairs=[("ENTRADA", "ENTRADA"), ("SALIDA", "OTRO")])
    plan = prepare_run(library, template, TODAY)
    assert not plan.ok and sum("no válido" in i.message for i in plan.issues) == 2
    library.models["carga_n_50"].values["conector"] = {"familia": "N", "genero": "hembra"}  # loads that do not mate the ports
    plan = prepare_run(library, library.procedures["acoplador_ACP-0001"], TODAY)
    assert any(i.message.startswith("Par ENTRADA – ACOPLADO: ") and "mismo género" in i.message for i in plan.issues)


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
    assert {s.id for s in suitable_setups(library, "vna_transmision")} == {"transmision_N_18G"}
    assert {s.id for s in suitable_setups(library, "vna_acoplador")} == {"acoplador_N_18G"}
    plan = prepare_run(library, template_with(library, "cable_CAB-DUT-0002", setup_id="lisn_impedancia_VNA-BF"), TODAY)
    assert not plan.ok and any("no sirve para «Camino RF — 2 puertos — Reflexión y transmisión" in i.message for i in plan.issues)
    plan = prepare_run(library, template_with(library, "cable_CAB-DUT-0001", setup_id="sac_nsa_3m"), TODAY)
    assert any("no tiene todavía algoritmo de ensayo" in i.message for i in plan.issues)
    template = library.procedures["cable_CAB-DUT-0002"].model_copy(deep=True)
    template.sweep.traces["S21"].if_bandwidth = 1e9
    plan = prepare_run(library, template, TODAY)
    assert any("IFBW" in i.message and "S21" in i.message for i in plan.issues)


def test_operator_can_stop_the_test(library):
    result, _, listener = run(library, "cable_CAB-DUT-0002", answers=lambda p: "3/" not in p.title)
    assert result.status == "abortado"
    assert ("calibracion", "abortado") in listener.steps
    assert result.record.files == ["hardware_setup.yaml", "plantilla_ensayo.yaml", "registro.txt"]
