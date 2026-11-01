"""Calibration kits: their standards from a .ckt, the model of Keysight, the electrical specifications,
standards defined by data, the units of the kit and their checks.

The values and uncertainties of the AA180-VNACKN are those of the .sdatb files the laboratory opened
in METAS VNA Tools (October 2026): the model, and the phase deviation (1,5°) and VSWR (1,055) of its
datasheet as k = 2.
"""

from __future__ import annotations

import math
import shutil
from datetime import date
from pathlib import Path

import numpy as np
import pytest

from calelektra.catalog.model import Calibration
from calelektra.core.frequency import FrequencyRange
from calelektra.core.issues import Severity
from calelektra.core.touchstone import Touchstone, write_touchstone
from calelektra.kits import (
    KitError,
    KitSettings,
    KitStandard,
    ResolvedStandard,
    SpecRow,
    StandardDefinition,
    UnitStandard,
    export_sdatb,
    kit_frequencies,
    kit_standards,
    read_ckt,
    standard_data,
    table_rows,
)
from calelektra.kits.agilent import model_s, reflection
from calelektra.sdata import read_sdatb
from calelektra.sdata.correlation import MAX_POINTS, frequency_matrix, point_matrix
from calelektra.sdata.unclib import cross_covariance
from calelektra.setups.analysis import analyze_setup

from conftest import TODAY, setup_with_dut

CKT = Path(__file__).resolve().parent / "datos" / "kits" / "AA180-VNACKN1.ckt"
LOAD_SDATB = Path(__file__).resolve().parent / "datos" / "metas" / "Example_Loadf_18831_20150824.sdatb"
SETTINGS = KitSettings("AA180-VNACKN", 50.0, FrequencyRange(0.0, 18e9), 1800)


def by_id(standards: list[KitStandard]) -> dict[str, KitStandard]:
    return {standard.id: standard for standard in standards}


def at(data, ghz: float) -> int:
    return int(np.argmin(abs(data.frequencies - ghz * 1e9)))


def test_the_ckt_of_the_kit():
    kit = read_ckt(CKT)
    assert (kit.version, kit.label, kit.description, kit.family) == (5, "AA180-VNACKN", "TypeN (50) Calibration Kit", "N")
    assert [s.label for s in kit.standards] == [
        "BROADBAND LOAD -M-", "OPEN -M-", "SHORT -M-", "BROADBAND LOAD -F-", "OPEN -F-", "SHORT -F-", "THRU",
    ]
    classes = dict(kit.classes[:3])
    assert classes == {"OPENS": ["OPEN -M-", "OPEN -F-"], "SHORTS": ["SHORT -M-", "SHORT -F-"], "LOADS": ["BROADBAND LOAD -M-", "BROADBAND LOAD -F-"]}
    standards = by_id(kit_standards(kit))
    assert list(standards) == ["carga_m", "abierto_m", "cortocircuito_m", "carga_h", "abierto_h", "cortocircuito_h", "through_hm"]
    open_m = standards["abierto_m"].definition
    assert (open_m.offset_z0, open_m.offset_delay_ps, open_m.offset_loss) == (50.0, 57.993, 0.93)
    assert open_m.capacitance == [89.939, 2536.7999, -264.9901, 13.4]
    assert standards["cortocircuito_h"].definition.inductance == [-0.1315, 606.2089, -68.405, 2.0206]
    assert standards["through_hm"].genders == ["hembra", "macho"] and standards["through_hm"].definition.offset_loss == 2.2
    assert standards["abierto_m"].frequency_range is None  # 0 – 999 GHz: no limit
    assert str(standards["abierto_h"].frequency_range) == "DC – 18,6 GHz"  # its female connector
    for standard in standards.values():  # the YAML of the library and back
        assert KitStandard.model_validate(standard.to_yaml()) == standard


def test_the_model_of_keysight_as_vna_tools():
    standards = by_id(kit_standards(read_ckt(CKT)))
    f = np.array([1e9, 18e9])
    expected = {  # |Γ| and phase (°) of the files checked with VNA Tools
        "abierto_m": [(0.99988, -45.08), (0.99544, -103.60)],
        "cortocircuito_m": [(0.9973, 134.41), (0.99357, 81.81)],
        "abierto_h": [(0.99999, -20.17), (0.99792, -9.18)],
        "cortocircuito_h": [(0.99874, 159.77), (0.99728, 176.99)],
    }
    for key, values in expected.items():
        gamma = model_s(standards[key], f)[:, 0, 0]
        for g, (magnitude, phase) in zip(gamma, values):
            assert abs(g) == pytest.approx(magnitude, abs=1e-5) and math.degrees(np.angle(g)) == pytest.approx(phase, abs=0.005)
    assert model_s(standards["abierto_m"], np.array([1e9]))[0, 0, 0] == pytest.approx(0.7060269106131081 - 0.7080203474142178j, abs=1e-14)
    assert np.allclose(model_s(standards["carga_m"], f), 0) and np.allclose(model_s(standards["through_hm"], f)[:, 1, 0], 1)
    # the same Γ through the input impedance of the offset line
    definition = standards["cortocircuito_m"].definition
    w = 2 * np.pi * f
    loss, delay, z0 = definition.offset_loss * 1e9, definition.offset_delay_ps * 1e-12, definition.offset_z0
    root = np.sqrt(f / 1e9)
    zc = z0 + (1 - 1j) * loss / (2 * w) * root
    gl = loss * delay / (2 * z0) * root * (1 + 1j) + 1j * w * delay
    inductance = sum(c * u * f**k for k, (c, u) in enumerate(zip(definition.inductance, (1e-12, 1e-24, 1e-33, 1e-42))))
    zl = 1j * w * inductance
    zin = zc * (zl + zc * np.tanh(gl)) / (zc + zl * np.tanh(gl))
    assert np.allclose(reflection(f, "cortocircuito", definition), (zin - 50) / (zin + 50), rtol=0, atol=1e-12)
    with pytest.raises(KitError, match="DC"):
        model_s(standards["abierto_m"], np.array([0.0, 1e9]))
    with pytest.raises(ValueError, match="guía de onda"):  # only coaxial standards
        StandardDefinition.model_validate({"clase": "modelo", "medio": "guia", "frecuencia_corte": "6.557 GHz"})
    assert "medio" not in StandardDefinition.model_validate({"clase": "modelo", "medio": "coaxial"}).to_yaml()


def test_frequencies_of_the_kit():
    f = kit_frequencies(SETTINGS)
    assert (len(f), f[0], f[-1]) == (1800, 10e6, 18e9)  # from DC: a step above
    assert kit_frequencies(SETTINGS, FrequencyRange(0, 9e9))[-1] == 9e9
    assert kit_frequencies(KitSettings("x", 50, FrequencyRange(1e9, 2e9), 11))[1] == 1.1e9


def test_uncertainty_of_the_electrical_specifications():
    standards = by_id(kit_standards(read_ckt(CKT)))
    open_m = standards["abierto_m"]
    open_m.specifications = [SpecRow(frecuencia=0, modulo_db=0, fase_grados=1.5)]
    data = standard_data(ResolvedStandard(open_m, None, Path(".")), SETTINGS)
    u_re, u_im = data.standard_uncertainty()
    k = at(data, 1)
    assert (u_re[k, 0, 0], u_im[k, 0, 0]) == (pytest.approx(0.009267964675119167, rel=1e-12), pytest.approx(0.009241870648411823, rel=1e-12))
    assert len(data.vector.inputs) == 1  # one input, the same at all the frequencies
    assert "desviación de fase" in data.vector.inputs[0].description
    row = table_rows(data)[k]
    assert (row.parameter, row.u_phase_deg, row.u_magnitude) == ("S11", pytest.approx(1.5), pytest.approx(0, abs=1e-9))  # √ of rounding
    # a load: −40 dB is 0,01 in the real and the imaginary parts (k = 2), as VNA Tools
    load = standards["carga_m"]
    load.specifications = [SpecRow(frecuencia=0, perdidas_retorno_db=40)]  # positive: also −40 dB
    data = standard_data(ResolvedStandard(load, None, Path(".")), SETTINGS)
    u_re, u_im = data.standard_uncertainty()
    assert np.allclose(u_re, 0.005) and np.allclose(u_im, 0.005) and len(data.vector.inputs) == 2
    # interpolated between the rows, constant outside
    load.specifications = [SpecRow(frecuencia="1 GHz", perdidas_retorno_db=-40), SpecRow(frecuencia="3 GHz", perdidas_retorno_db=-20)]
    u_re = standard_data(ResolvedStandard(load, None, Path(".")), SETTINGS).standard_uncertainty()[0][:, 0, 0]
    f = kit_frequencies(SETTINGS)
    assert u_re[f == 10e6][0] == pytest.approx(0.005) and u_re[f == 18e9][0] == pytest.approx(0.05)
    assert u_re[f == 2e9][0] == pytest.approx(10 ** (-30 / 20) / 2)
    # a through: S21 and S12 with the same inputs, S11 and S22 with their own
    thru = standards["through_hm"]
    thru.specifications = [SpecRow(frecuencia=0, perdidas_retorno_db=-30, transmision_modulo_db=0.05, transmision_fase_grados=0.5)]
    data = standard_data(ResolvedStandard(thru, None, Path(".")), SETTINGS)
    assert len(data.vector.inputs) == 6
    k = at(data, 6)
    s21, s12, s11, s22 = data.element(k, 1, 0, 0), data.element(k, 0, 1, 0), data.element(k, 0, 0, 0), data.element(k, 1, 1, 0)
    covariance = data.vector.covariance([s21, s12, s11, s22])
    assert covariance[0, 1] == pytest.approx(covariance[0, 0]) and covariance[2, 3] == 0
    row = next(r for r in table_rows(data) if r.parameter == "S21" and r.frequency == data.frequencies[k])
    assert row.u_magnitude == pytest.approx(math.log(10) / 20 * 0.05) and row.u_phase_deg == pytest.approx(0.5)


def test_correlation_of_the_electrical_specifications():
    """One input per specified quantity: the same deviation at all the frequencies (docs/10 §7.2.1)."""
    standards = by_id(kit_standards(read_ckt(CKT)))
    open_m = standards["abierto_m"]
    open_m.specifications = [SpecRow(frecuencia=0, fase_grados=1.5)]
    data = standard_data(ResolvedStandard(open_m, None, Path(".")), SETTINGS)
    # the phase: +1 between all the frequencies, u = 1,5°/2 at each one
    phase = frequency_matrix(data, "S11", "fase")
    assert (phase.size, phase.total) == (MAX_POINTS, 1800) and (phase.points[0], phase.points[-1]) == (0, 1799)
    assert np.allclose(phase.correlation, 1.0) and np.allclose(phase.standard_uncertainties, 0.75) and phase.units[0] == "°"
    # the real part: ±1, with the sign of its sensitivity to the phase, −Im Γ
    real = frequency_matrix(data, "S11", "re")
    assert np.allclose(np.abs(real.correlation), 1.0)
    sign = np.sign(-data.s[real.points, 0, 0].imag)
    assert np.array_equal(np.sign(real.correlation[0]), sign * sign[0]) and {-1.0, 1.0} <= set(sign)
    band = frequency_matrix(data, "S11", "re", start=1e9, stop=2e9)  # a range: its frequencies, all of them
    assert band.size == band.total == 101 and (band.frequencies[0], band.frequencies[-1]) == (1e9, 2e9)
    # a difference of phases has no uncertainty: the deviation is the same at both frequencies
    pair = frequency_matrix(data, "S11", "fase", start=1e9, stop=1.01e9)
    v = pair.covariance
    assert pair.size == 2 and v[0, 0] + v[1, 1] - 2 * v[0, 1] == pytest.approx(0, abs=1e-9)
    # the magnitude only has rounding: no uncertainty, no correlation
    magnitude = frequency_matrix(data, "S11", "modulo")
    assert magnitude.defined.all() and not magnitude.valid.any() and np.isnan(magnitude.correlation).all()
    assert magnitude.off_diagonal().size == 0 and not magnitude.covariance.any()
    # at a frequency: the real and imaginary parts share the input
    point = point_matrix(data, at(data, 1))
    assert point.labels == ["S11 re", "S11 im"] and np.allclose(point.correlation, 1.0)
    assert np.allclose(point.covariance, data.point_covariance(at(data, 1)), rtol=1e-12, atol=0)
    polar = point_matrix(data, at(data, 1), "polar")
    assert polar.labels == ["S11 |S|", "S11 ∠"] and polar.units == ["", "°"] and polar.valid.tolist() == [False, True]
    assert polar.standard_uncertainties[1] == pytest.approx(0.75)
    # an ideal load: Γ = 0, the magnitude and the phase are not defined; its real and imaginary parts, +1
    load = standards["carga_m"]
    load.specifications = [SpecRow(frecuencia=0, perdidas_retorno_db=-40)]
    data = standard_data(ResolvedStandard(load, None, Path(".")), SETTINGS)
    assert not frequency_matrix(data, "S11", "fase").defined.any()
    assert np.allclose(frequency_matrix(data, "S11", "im", start=1e9, stop=3e9).correlation, 1.0)
    assert point_matrix(data, 0).correlation[0, 1] == 0  # an input for each part
    # a through: S21 and S12 with the same inputs, S11 and S22 with their own
    thru = standards["through_hm"]
    thru.specifications = [SpecRow(frecuencia=0, perdidas_retorno_db=-30, transmision_modulo_db=0.05, transmision_fase_grados=0.5)]
    data = standard_data(ResolvedStandard(thru, None, Path(".")), SETTINGS)
    point = point_matrix(data, at(data, 6))
    assert point.labels == ["S11 re", "S11 im", "S21 re", "S21 im", "S12 re", "S12 im", "S22 re", "S22 im"]
    rho = point.correlation
    assert rho[2, 4] == pytest.approx(1.0) and rho[3, 5] == pytest.approx(1.0) and rho[0, 6] == 0 and rho[0, 2] == 0
    polar = point_matrix(data, at(data, 6), "polar")
    assert polar.standard_uncertainties[2] == pytest.approx(abs(data.s[at(data, 6), 1, 0]) * math.log(10) / 20 * 0.05 / 2)
    assert polar.standard_uncertainties[3] == pytest.approx(0.25) and polar.correlation[2, 3] == pytest.approx(0, abs=1e-9)
    # copied for a spreadsheet: the frequencies in Hz, a point as decimal separator
    lines = frequency_matrix(data, "S21", "fase", start=1e9, stop=1.02e9).text().splitlines()
    assert lines[0].split("\t") == ["f (Hz)", "1000000000", "1010000000", "1020000000"] and lines[1].split("\t")[1:] == ["1.0"] * 3
    assert point.text().splitlines()[0].split("\t")[:3] == ["6 GHz", "S11 re", "S11 im"]


def test_standards_defined_by_data(tmp_path):
    load = KitStandard(
        id="carga_h", type="carga", genders=["hembra"],
        definition=StandardDefinition(kind="datos", file=str(LOAD_SDATB)),
        specifications=[SpecRow(frecuencia=0, perdidas_retorno_db=-20)],  # not used: the .sdatb has its uncertainty
    )
    data = standard_data(ResolvedStandard(load, None, tmp_path), SETTINGS)
    reference = read_sdatb(LOAD_SDATB)
    assert np.array_equal(data.s, reference.s) and len(data.vector.inputs) == len(reference.vector.inputs)
    assert np.array_equal(data.standard_uncertainty()[0], reference.standard_uncertainty()[0])
    # a Touchstone: its values and the uncertainty of the specifications
    f = np.array([1e9, 2e9])
    s = np.zeros((2, 2, 2), complex)
    s[:, 0, 0], s[:, 1, 1], s[:, 1, 0], s[:, 0, 1] = 0.01, 0.02 + 0.01j, 0.5, 0.5
    write_touchstone(tmp_path / "carga.s2p", Touchstone(2, f, s))
    load = load.model_copy(update={"definition": StandardDefinition(kind="datos", file="carga.s2p", ports=[2])})
    data = standard_data(ResolvedStandard(load, None, tmp_path), SETTINGS)
    assert data.n_ports == 1 and np.allclose(data.s[:, 0, 0], 0.02 + 0.01j)  # its port 2
    assert np.allclose(data.standard_uncertainty()[0], 0.05)
    with pytest.raises(KitError, match="elija los puertos"):
        standard_data(ResolvedStandard(load.model_copy(update={"definition": StandardDefinition(kind="datos", file="carga.s2p")}), None, tmp_path), SETTINGS)
    with pytest.raises(KitError, match="no tiene el puerto 3"):
        standard_data(ResolvedStandard(load.model_copy(update={"definition": StandardDefinition(kind="datos", file="carga.s2p", ports=[3])}), None, tmp_path), SETTINGS)
    with pytest.raises(KitError, match="no tiene definición"):
        standard_data(ResolvedStandard(load.model_copy(update={"definition": StandardDefinition()}), None, tmp_path), SETTINGS)


def test_export_as_vna_tools(tmp_path):
    standards = kit_standards(read_ckt(CKT))
    units = {"abierto_m": UnitStandard(numero_serie="975")}
    resolved = [ResolvedStandard(s, units.get(s.id), Path(".")) for s in standards]
    files = export_sdatb(resolved, SETTINGS, tmp_path)
    assert sorted(p.name for p in files) == sorted(
        [
            "Load(m) AA180-VNACKN.sdatb", "Open(m) 975.sdatb", "Short(m) AA180-VNACKN.sdatb", "Load(f) AA180-VNACKN.sdatb",
            "Open(f) AA180-VNACKN.sdatb", "Short(f) AA180-VNACKN.sdatb", "Thru(f-m) AA180-VNACKN.sdatb",
        ]
    )
    thru = read_sdatb(tmp_path / "Thru(f-m) AA180-VNACKN.sdatb")
    assert thru.n_ports == 2 and thru.frequencies[-1] == 18e9 and np.allclose(thru.s[:, 1, 0], 1)
    opened = read_sdatb(tmp_path / "Open(m) 975.sdatb")
    assert opened.s[1, 0, 0] == pytest.approx(model_s(standards[1], np.array([20e6]))[0, 0, 0], abs=1e-15)


def test_the_kit_in_the_library(library):
    kit = library.equipment["KIT-N-02"]
    kit.standards, kit.calibration = {}, None  # without its calibration of 01/09/2026 (test_kit_calibration.py)
    model = library.model_of(kit)
    settings = library.kit_settings(model, kit)
    assert (settings.name, settings.zr, settings.points, settings.frequency_range.stop) == ("AA180-VNACKN", 50, 1800, 18e9)
    standards = {s.id: s for s in library.kit_standards(model, kit)}
    data = library.standard_data(standards["abierto_m"], settings)
    assert data.standard_uncertainty()[0][at(data, 1), 0, 0] == pytest.approx(0.009267964675119167, rel=1e-12)  # as in VNA Tools
    load = library.standard_data(standards["carga_m"], settings)
    assert load.standard_uncertainty()[0][0, 0, 0] == pytest.approx(10 ** (-31.45 / 20) / 2)
    assert library.kit_genders(kit) == ["macho", "hembra"] and library.kit_issues(kit) == []
    # the unit: its serial numbers, a damaged open and the data of a load from its certificate
    relative, digest = library.copy_standard_file(LOAD_SDATB, kit.id)
    assert relative == "../patrones/KIT-N-02/Example_Loadf_18831_20150824.sdatb" and len(digest) == 64
    kit.calibration = Calibration(fecha=date(2026, 4, 1), proxima=date(2027, 4, 1))
    kit.standards = {
        "abierto_m": UnitStandard(numero_serie="975", estado="danado"),
        "carga_h": UnitStandard(
            numero_serie="18831",
            definicion=StandardDefinition(kind="datos", file=relative, sha256=digest, reference="Certificado 2015", data_date=date(2015, 8, 24)),
        ),
        "cortocircuito_h": UnitStandard(numero_serie="747", estado="sustituido", fecha_estado=date(2026, 6, 1)),
    }
    library.save_equipment(kit)
    library.reload()
    kit = library.equipment["KIT-N-02"]
    standards = {s.id: s for s in library.kit_standards(library.model_of(kit), kit)}
    assert standards["carga_h"].own_definition and standards["carga_h"].name(settings.name) == "Load(f) 18831"
    data = library.standard_data(standards["carga_h"], settings)
    assert data.n_points == 533 and cross_covariance(data.vector, [2], read_sdatb(LOAD_SDATB).vector, [2])[0, 0] > 0
    assert library.kit_genders(kit) == ["hembra"]  # the male open is damaged
    issues = {issue.code: issue for issue in library.kit_issues(kit)}
    assert set(issues) == {"patron.danado", "patron.sustituido", "patron.datos_antiguos"}
    assert "975" not in issues["patron.danado"].message and "OPEN -M-" in issues["patron.danado"].message
    # in a setup: planes N female need male standards
    library.setups["cable_N_18G"].assignments["kit_cal"] = "KIT-N-02"
    analysis = analyze_setup(setup_with_dut(library, "cable_N_18G", "CAB-DUT-0001"), library, TODAY)
    messages = [i.message for i in analysis.issues if i.severity is Severity.ERROR]
    assert any("necesita estándares macho" in m and "KIT-N-02" in m for m in messages)
    assert "patron.danado" in {i.code for i in analysis.issues if i.severity is Severity.WARNING}


def test_a_missing_data_file(library, tmp_path):
    kit = library.equipment["KIT-N-02"]
    shutil.copy(LOAD_SDATB, tmp_path / "carga.sdatb")
    relative, _ = library.copy_standard_file(tmp_path / "carga.sdatb", kit.id)
    kit.standards = {"carga_h": UnitStandard(definicion=StandardDefinition(kind="datos", file=relative))}
    library.save_equipment(kit)
    (library.root / "catalogo" / "patrones" / "KIT-N-02" / "carga.sdatb").unlink()
    library.reload()
    issues = library.kit_issues(library.equipment["KIT-N-02"])
    assert [i.code for i in issues] == ["patron.fichero"] and issues[0].severity is Severity.ERROR
