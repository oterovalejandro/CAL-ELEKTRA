"""Frequencies of the setups, trace settings of the templates, display smoothing, specifications of the models and test records."""

from pathlib import Path

import numpy as np
import pytest
from pydantic import ValidationError

from calelektra.catalog.library import LibraryError
from calelektra.catalog.model import SpecFile
from calelektra.core.frequency import FrequencyRange
from calelektra.core.touchstone import Touchstone, write_touchstone
from calelektra.procedures.model import ProcedureTemplate, SweepSettings
from calelektra.procedures.processing import smooth, smoothing_window
from calelektra.procedures.specs import evaluate_spec, spec_curves, verdict
from calelektra.setups.analysis import analyze_setup
from calelektra.setups.frequencies import FrequencyError, plan_frequencies, read_frequency_list
from calelektra.setups.model import FrequencySettings

from conftest import TODAY


# ── frequencies of the hardware setup ──────────────────────────────────────
def test_linear_by_points_and_by_step():
    plan = plan_frequencies(FrequencySettings(modo="lineal", puntos=11), FrequencyRange(1e9, 2e9))
    assert plan.points == 11 and plan.frequencies[1] == pytest.approx(1.1e9)
    plan = plan_frequencies(FrequencySettings(modo="lineal", definicion="paso", paso="50 MHz"), FrequencyRange(50e6, 40e9))
    assert plan.points == 800 and plan.stop == pytest.approx(40e9) and not plan.note
    assert "paso 50 MHz" in plan.describe()
    plan = plan_frequencies(FrequencySettings(modo="lineal", definicion="paso", paso="300 MHz"), FrequencyRange(1e9, 2e9))
    assert plan.points == 4 and plan.stop == pytest.approx(1.9e9) and "no es múltiplo" in plan.note
    with pytest.raises(FrequencyError, match="mayor que el rango"):
        plan_frequencies(FrequencySettings(modo="lineal", definicion="paso", paso="3 GHz"), FrequencyRange(1e9, 2e9))


def test_logarithmic():
    plan = plan_frequencies(FrequencySettings(modo="logaritmico", puntos=201), FrequencyRange(9e3, 30e6))
    assert plan.points == 201 and plan.start == pytest.approx(9e3) and plan.stop == pytest.approx(30e6)
    assert plan.frequencies[1] / plan.frequencies[0] == pytest.approx(plan.frequencies[-1] / plan.frequencies[-2])
    with pytest.raises(FrequencyError, match="DC"):
        plan_frequencies(FrequencySettings(modo="logaritmico"), FrequencyRange(0, 30e6))


def test_frequency_settings_validation():
    with pytest.raises(ValidationError, match="paso"):
        FrequencySettings(modo="lineal", definicion="paso")
    with pytest.raises(ValidationError, match="CSV"):
        FrequencySettings(modo="lista")
    dumped = FrequencySettings(modo="lineal", definicion="paso", paso="50 MHz").model_dump(by_alias=True, exclude_none=True, mode="json")
    assert dumped["modo"] == "lineal" and dumped["paso"] == "50 MHz"
    assert FrequencySettings(modo="lineal", definicion="paso", paso="50 MHz").summary() == "Lineal, paso 50 MHz"


def test_frequency_list_csv(tmp_path):
    path = tmp_path / "lista.csv"
    path.write_text("# puntos del laboratorio\nFrecuencia (MHz);Nota\n30;a\n1000,5;b\n100\n100\n2 GHz;c\n", encoding="utf-8")
    assert list(read_frequency_list(path)) == pytest.approx([30e6, 100e6, 1000.5e6, 2e9])  # sorted, without repeated points
    plan = plan_frequencies(FrequencySettings(modo="lista", fichero_lista="lista.csv"), FrequencyRange(50e6, 1.5e9), path)
    assert list(plan.frequencies) == pytest.approx([100e6, 1000.5e6])  # only inside the required range
    assert "2 frecuencias de la lista quedan fuera" in plan.note
    path.write_text("Frecuencia\n1e9\nabc\n", encoding="utf-8")
    with pytest.raises(FrequencyError, match="línea 3"):
        read_frequency_list(path)
    with pytest.raises(FrequencyError, match="No se puede leer"):
        read_frequency_list(tmp_path / "no_existe.csv")


def test_setup_analysis_checks_the_frequencies(library):
    setup = library.setups["cable_N_18G"]
    analysis = analyze_setup(setup, library, TODAY)
    assert analysis.frequency_plan.points == 268 and analysis.frequency_plan.mode == "lista"
    assert library.setup_list_path(setup) == (library.root / "hardware_setups/listas/puntos_cable_N.csv").resolve()
    setup = setup.model_copy(deep=True, update={"required_range": FrequencyRange(30e6, 1e9)})
    analysis = analyze_setup(setup, library, TODAY)
    assert analysis.frequency_plan.points == 98 and any(i.code == "frecuencias.aviso" for i in analysis.issues)
    setup = library.setups["cable_vna_292"].model_copy(deep=True)
    setup.frequencies = FrequencySettings(modo="lineal", puntos=200001)
    analysis = analyze_setup(setup, library, TODAY)
    assert not analysis.ok and any(i.code == "frecuencias.puntos" and "VNA-01" in i.message for i in analysis.issues)
    setup.frequencies = FrequencySettings(modo="lista", fichero_lista="no_existe.csv")
    analysis = analyze_setup(setup, library, TODAY)
    assert not analysis.ok and any(i.code == "frecuencias.definicion" for i in analysis.issues)


# ── trace settings of the template ─────────────────────────────────────────
def test_trace_settings_inherit_from_all_traces():
    sweep = SweepSettings(ifbw="1 kHz", promedios=2, suavizado_pct=0, por_traza=True, trazas={"S21": {"ifbw": "100 Hz"}, "S11": {"suavizado_pct": 5}})
    assert sweep.trace("S21") == (100.0, 2, 0.0)
    assert sweep.trace("S11") == (1e3, 2, 5.0)
    assert sweep.trace("S22") == (1e3, 2, 0.0)
    assert sweep.smoothing(("S11", "S21", "S12", "S22")) == {"S11": 5.0}
    sweep.per_trace = False
    assert sweep.trace("S21") == (1e3, 2, 0.0)  # per-trace values are kept but not used


def test_templates_of_earlier_versions_load():
    template = ProcedureTemplate.model_validate(
        {
            "id": "viejo",
            "nombre": "Viejo",
            "algoritmo": "vna_2_puertos",
            "plantillas_compatibles": ["cable_vna"],
            "barrido": {"modo": "lineal", "inicio": "1 GHz", "fin": "2 GHz", "puntos": 11, "ifbw": "100 Hz"},
            "especificaciones": [{"nombre": "x", "fichero": "x.s2p"}],
        }
    )
    assert template.sweep.if_bandwidth == 100.0 and template.setup_id is None
    dumped = template.to_yaml_dict()
    assert "plantillas_compatibles" not in dumped and "especificaciones" not in dumped and "modo" not in dumped["barrido"]


# ── display smoothing ──────────────────────────────────────────────────────
def test_smoothing_keeps_the_delay_and_reduces_the_ripple():
    assert smoothing_window(800, 1) == 9 and smoothing_window(100, 0.5) == 1
    f = np.linspace(1e9, 2e9, 801)
    rng = np.random.default_rng(1)
    trace = 0.5 * (1 + 0.05 * rng.standard_normal(f.size)) * np.exp(-2j * np.pi * f * 5e-9)
    smoothed = smooth(trace, 3)
    assert np.std(np.abs(smoothed[50:-50])) < 0.3 * np.std(np.abs(trace[50:-50]))
    phase_error = np.angle(smoothed[50:-50] / np.exp(-2j * np.pi * f[50:-50] * 5e-9))
    assert np.max(np.abs(phase_error)) < 0.05  # the linear phase of a long cable is not averaged away
    assert np.allclose(smooth(trace, 0), trace)


# ── specifications of the models ───────────────────────────────────────────
def _spec_file(tmp_path: Path, s21_db: float, s11_db: float, start=1e9, stop=2e9) -> Path:
    f = np.linspace(start, stop, 11)
    s = np.zeros((f.size, 2, 2), dtype=complex)
    s[:, 1, 0] = s[:, 0, 1] = 10 ** (s21_db / 20)
    s[:, 0, 0] = s[:, 1, 1] = 10 ** (s11_db / 20)
    path = tmp_path / "espec.s2p"
    write_touchstone(path, Touchstone(2, f, s), "DB", "GHZ")
    return path


def _measurement(s21_db: float, s11_db: float, f: np.ndarray) -> np.ndarray:
    s = np.zeros((f.size, 2, 2), dtype=complex)
    s[:, 1, 0] = s[:, 0, 1] = 10 ** (s21_db / 20) * np.exp(-1j * f * 1e-9)
    s[:, 0, 0] = s[:, 1, 1] = 10 ** (s11_db / 20)
    return s


def test_minimum_maximum_and_tolerance(tmp_path):
    path = _spec_file(tmp_path, s21_db=-3.0, s11_db=-15.0)
    f = np.linspace(0.5e9, 2e9, 16)
    measured = _measurement(-2.0, -20.0, f)
    insertion = evaluate_spec(SpecFile(nombre="IL", fichero="espec.s2p", parametros=["S21", "S12"], criterio="minimo"), path, f, measured)
    assert insertion.passed is True and insertion.parameters[0].worst_margin == pytest.approx(1.0)
    assert insertion.parameters[0].not_evaluated == 5  # below 1 GHz the specification does not apply
    assert insertion.to_record()["resultado"] == "PASA" and len(insertion.sha256) == 64
    return_loss = evaluate_spec(SpecFile(nombre="RL", fichero="espec.s2p", parametros="S11", criterio="maximo"), path, f, measured)
    assert return_loss.passed is True and return_loss.parameters[0].worst_margin == pytest.approx(5.0)
    tight = evaluate_spec(SpecFile(nombre="ref", fichero="espec.s2p", parametros=["S21"], criterio="tolerancia", tolerancia=0.5), path, f, measured)
    assert tight.passed is False and tight.parameters[0].failed == 11 and "FALLA en 11 de 11" in tight.parameters[0].text("dB")
    wide = evaluate_spec(SpecFile(nombre="ref", fichero="espec.s2p", parametros=["S21"], criterio="tolerancia", tolerancia=1.5), path, f, measured)
    assert wide.passed is True
    assert verdict([insertion, return_loss]) == "PASA" and verdict([insertion, tight]) == "FALLA" and verdict([]) == "SIN_ESPECIFICACIONES"


def test_spec_outside_the_measurement_is_not_evaluated(tmp_path):
    path = _spec_file(tmp_path, -3.0, -15.0, start=10e9, stop=20e9)
    f = np.linspace(1e9, 2e9, 5)
    result = evaluate_spec(SpecFile(nombre="IL", fichero="espec.s2p"), path, f, _measurement(-2.0, -20.0, f))
    assert result.passed is None and verdict([result]) == "NO_EVALUADO"
    missing = evaluate_spec(SpecFile(nombre="IL", fichero="x.s2p"), tmp_path / "x.s2p", f, _measurement(-2.0, -20.0, f))
    assert missing.passed is None and "No se puede leer" in missing.message
    curves = spec_curves(SpecFile(nombre="IL", fichero="espec.s2p", parametros=["S21", "S12"]), path)
    assert [c.parameter for c in curves] == ["S21", "S12"] and curves[0].values[0] == pytest.approx(-3.0)


def test_spec_file_validation():
    with pytest.raises(ValidationError, match="tolerancia"):
        SpecFile(nombre="ref", fichero="a.s2p", criterio="tolerancia")
    with pytest.raises(ValidationError, match="parámetro S"):
        SpecFile(nombre="ref", fichero="a.s2p", parametros=["Z21"])
    assert SpecFile(nombre="ref", fichero="a.s2p", parametros=["s21"], criterio="tolerancia", tolerancia=0.5).describe() == "|S21| (dB) = especificación ± 0.5 dB"


def test_specifications_belong_to_the_model_of_the_eut(library, tmp_path):
    specs = library.specifications_of(library.equipment["CAB-DUT-0002"])
    assert [spec.name for spec, _ in specs] == ["Pérdidas de inserción máximas", "Pérdidas de retorno mínimas"]
    assert all(path == (library.root / "catalogo/especificaciones/cable_292_0m5_mascara.s2p").resolve() for _, path in specs)
    assert library.specifications_of(library.equipment["LISN-0001"]) == []
    source = tmp_path / "nueva.s2p"
    write_touchstone(source, Touchstone(2, np.array([1e9, 2e9]), np.full((2, 2, 2), 0.5 + 0j)))
    reference = library.copy_spec_file(source)
    assert reference == "../especificaciones/nueva.s2p" and (library.root / "catalogo/especificaciones/nueva.s2p").is_file()
    model = library.models["cable_coaxial_n"]
    model.s_specifications.append(SpecFile(nombre="Nueva", fichero=reference))
    library.save_model(model)
    library.reload()
    assert [s.name for s in library.models["cable_coaxial_n"].s_specifications][-1] == "Nueva"
    assert library.load_issues == []


# ── tests: records of launched templates ───────────────────────────────────
def test_test_folders_and_external_roots(library, tmp_path):
    procedure = library.procedures["cable_CAB-DUT-0001"]
    first = library.new_test_record(library.tests_folder / "2026" / "cliente A", "Cable 1", procedure, "ana")
    second = library.new_test_record(library.tests_folder / "2026" / "cliente A", "Cable 1", procedure, "ana")
    assert first.folder.name == "Cable_1" and second.folder.name == "Cable_1_2" and first.status == "en_curso"
    assert library.load_test(first.folder).template["eut"] == "CAB-DUT-0001"
    assert library.test_folders(library.tests_folder) == [library.tests_folder / "2026"]
    assert library.new_test_id(tmp_path, "Cable 2,92 mm nº 3 — Verificación") == "Cable_2_92_mm_no_3_Verificacion"
    external = tmp_path / "red" / "ensayos"
    external.mkdir(parents=True)
    library.add_test_root(external)
    assert library.test_roots() == [library.tests_folder, external.resolve()]
    third = library.new_test_record(external, "Cable externo", procedure)
    assert {t.folder for t in library.tests()} == {first.folder, second.folder, third.folder}
    assert [t.folder for t in library.tests(external)] == [third.folder]
    library.remove_test_root(external)
    assert library.test_roots() == [library.tests_folder]
    library.delete_test(second)
    assert not second.folder.exists()
    with pytest.raises(LibraryError):
        library.delete_test(second.folder)


def test_setups_and_equipment_used_by_templates_are_protected(library):
    assert library.templates_using(setup_id="cable_vna_292") == ["cable_CAB-DUT-0002"]
    assert library.templates_using(equipment_id="LISN-0001") == ["lisn_LISN-0001"]
    with pytest.raises(LibraryError, match="plantillas de ensayo"):
        library.delete_setup("cable_vna_292")
    with pytest.raises(LibraryError, match="EUT"):
        library.delete_equipment("CAB-DUT-0003")
    assert "cable_vna_292" in library.setups
