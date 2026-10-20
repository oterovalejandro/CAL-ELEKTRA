"""Test templates: frequency definition, trace settings, smoothing, specifications and tests in folders."""

from pathlib import Path

import numpy as np
import pytest
from pydantic import ValidationError

from calelektra.core.frequency import FrequencyRange
from calelektra.core.touchstone import Touchstone, write_touchstone
from calelektra.procedures.frequencies import FrequencyError, plan_frequencies, read_frequency_list
from calelektra.procedures.model import SpecFile, SweepSettings
from calelektra.procedures.processing import smooth, smoothing_window
from calelektra.procedures.record import TestDefinition
from calelektra.procedures.specs import evaluate_spec, spec_curves, verdict
from calelektra.catalog.library import LibraryError


# ── frequency definition ───────────────────────────────────────────────────
def test_linear_by_points_and_by_step():
    plan = plan_frequencies(SweepSettings(modo="lineal", inicio="1 GHz", fin="2 GHz", puntos=11))
    assert plan.points == 11 and plan.frequencies[1] == pytest.approx(1.1e9)
    plan = plan_frequencies(SweepSettings(modo="lineal", definicion="paso", inicio="50 MHz", fin="40 GHz", paso="50 MHz"))
    assert plan.points == 800 and plan.stop == pytest.approx(40e9) and not plan.note
    assert "paso 50 MHz" in plan.describe()
    plan = plan_frequencies(SweepSettings(modo="lineal", definicion="paso", inicio="1 GHz", fin="2 GHz", paso="300 MHz"))
    assert plan.points == 4 and plan.stop == pytest.approx(1.9e9) and "no es múltiplo" in plan.note
    with pytest.raises(FrequencyError, match="mayor que el rango"):
        plan_frequencies(SweepSettings(modo="lineal", definicion="paso", inicio="1 GHz", fin="2 GHz", paso="3 GHz"))


def test_logarithmic_and_setup_range():
    sweep = SweepSettings(modo="logaritmico", puntos=201)
    with pytest.raises(FrequencyError, match="inicio y fin"):
        plan_frequencies(sweep)
    plan = plan_frequencies(sweep, FrequencyRange(9e3, 30e6))
    assert plan.points == 201 and plan.start == pytest.approx(9e3) and plan.stop == pytest.approx(30e6)
    assert plan.frequencies[1] / plan.frequencies[0] == pytest.approx(plan.frequencies[-1] / plan.frequencies[-2])
    with pytest.raises(FrequencyError, match="DC"):
        plan_frequencies(sweep, FrequencyRange(0, 30e6))


def test_sweep_validation_and_old_keys():
    assert SweepSettings.model_validate({"tipo": "logaritmico", "puntos": 11}).mode == "logaritmico"  # files of v0.3
    with pytest.raises(ValidationError, match="paso"):
        SweepSettings(modo="lineal", definicion="paso")
    with pytest.raises(ValidationError, match="CSV"):
        SweepSettings(modo="lista")
    with pytest.raises(ValidationError, match="mayor"):
        SweepSettings(inicio="2 GHz", fin="1 GHz")
    dumped = SweepSettings(modo="lineal", definicion="paso", inicio="50 MHz", fin="40 GHz", paso="50 MHz").model_dump(by_alias=True, mode="json")
    assert dumped["modo"] == "lineal" and dumped["paso"] == "50 MHz" and dumped["inicio"] == "50 MHz"
    assert SweepSettings(modo="lineal", definicion="paso", inicio="50 MHz", fin="40 GHz", paso="50 MHz").summary() == "Lineal 50 MHz – 40 GHz, paso 50 MHz"


def test_frequency_list_csv(tmp_path):
    path = tmp_path / "lista.csv"
    path.write_text("# puntos del laboratorio\nFrecuencia (MHz);Nota\n30;a\n1000,5;b\n100\n100\n2 GHz;c\n", encoding="utf-8")
    frequencies = read_frequency_list(path)
    assert list(frequencies) == pytest.approx([30e6, 100e6, 1000.5e6, 2e9])  # sorted, without repeated points
    plan = plan_frequencies(SweepSettings(modo="lista", fichero_lista="lista.csv", inicio="50 MHz", fin="1,5 GHz"), list_path=path)
    assert list(plan.frequencies) == pytest.approx([100e6, 1000.5e6])
    path.write_text("Frecuencia\n1e9\nabc\n", encoding="utf-8")
    with pytest.raises(FrequencyError, match="línea 3"):
        read_frequency_list(path)
    with pytest.raises(FrequencyError, match="No se puede leer"):
        read_frequency_list(tmp_path / "no_existe.csv")


def test_trace_settings_inherit_from_all_traces():
    sweep = SweepSettings(ifbw="1 kHz", promedios=2, suavizado_pct=0, por_traza=True, trazas={"S21": {"ifbw": "100 Hz"}, "S11": {"suavizado_pct": 5}})
    assert sweep.trace("S21") == (100.0, 2, 0.0)
    assert sweep.trace("S11") == (1e3, 2, 5.0)
    assert sweep.trace("S22") == (1e3, 2, 0.0)
    sweep.per_trace = False
    assert sweep.trace("S21") == (1e3, 2, 0.0)  # per-trace values are kept but not used


# ── smoothing ──────────────────────────────────────────────────────────────
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


# ── specifications ─────────────────────────────────────────────────────────
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


# ── tests in folders ───────────────────────────────────────────────────────
def test_tests_in_folders_and_external_roots(library, tmp_path):
    procedure = library.procedures["cable_N_lista"]
    definition = TestDefinition.from_template(procedure, "cable_N_18G", "CAB-DUT-0001")
    first = library.create_test(library.tests_folder / "2026" / "cliente A", "Cable 1", definition, "ana")
    second = library.create_test(library.tests_folder / "2026" / "cliente A", "Cable 1", definition, "ana")
    assert first.folder.name == "Cable_1" and second.folder.name == "Cable_1_2"
    assert (first.folder / "listas" / "puntos_cable_N.csv").is_file()
    assert first.definition.template().sweep.list_file == "listas/puntos_cable_N.csv"
    assert library.test_folders(library.tests_folder) == [library.tests_folder / "2026"]
    external = tmp_path / "red" / "ensayos"
    external.mkdir(parents=True)
    library.add_test_root(external)
    assert library.test_roots() == [library.tests_folder, external.resolve()]
    third = library.create_test(external, "Cable externo", definition)
    assert {t.folder for t in library.tests()} == {first.folder, second.folder, third.folder}
    assert [t.folder for t in library.tests(external)] == [third.folder]
    library.remove_test_root(external)
    assert library.test_roots() == [library.tests_folder]
    library.delete_test(second)
    assert not second.folder.exists()
    with pytest.raises(LibraryError):
        library.delete_test(second.folder)


def test_redefine_a_prepared_test(library, tmp_path):
    spec = tmp_path / "otra.s2p"
    f = np.linspace(10e6, 40e9, 5)
    write_touchstone(spec, Touchstone(2, f, np.full((5, 2, 2), 0.5 + 0j)))
    procedure = library.procedures["cable_292_pasa_falla"]
    record = library.create_test(tmp_path, "Ensayo", TestDefinition.from_template(procedure, "cable_vna_292", "CAB-DUT-0002"))
    template = record.definition.template()
    data = template.to_yaml_dict()
    data["especificaciones"] = [{"nombre": "Otra", "fichero": str(spec), "parametros": ["S21"], "criterio": "minimo"}]
    changed = TestDefinition.model_validate({"procedimiento": template.id, "setup": "componente_292_tosm", "dut": "CAB-DUT-0003", "configuracion": data})
    library.redefine_test(record, changed)
    reloaded = library.load_test(record.folder)
    assert reloaded.definition.setup_id == "componente_292_tosm" and reloaded.dut_id == "CAB-DUT-0003"
    assert reloaded.definition.template().specifications[0].file == "especificaciones/otra.s2p"
    assert (record.folder / "especificaciones" / "otra.s2p").is_file()
    reloaded.status = "completado"
    with pytest.raises(LibraryError, match="preparado"):
        library.redefine_test(reloaded, changed)
