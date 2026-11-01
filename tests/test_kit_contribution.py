"""The contribution of the calibration kit to a calibrated measurement (``kits/contribution.py``): linear
propagation of the uncertainty of the definitions of the standards through OSM, TOSM, UOSM and the
transmission normalization, as METAS VNA Tools does."""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pytest

from calelektra.kits import ResolvedStandard, SpecRow, kit_frequencies
from calelektra.kits.contribution import (
    CalibrationStandards,
    _solve,
    calibration_standards,
    kit_contribution,
    reflection_dut,
    two_port_dut,
)
from calelektra.sdata import load_sparam_data
from calelektra.sdata.correlation import frequency_matrix

METAS_LOAD = Path(__file__).resolve().parent / "datos" / "metas" / "Example_Loadf_18831_20150824.sdatb"
RL = 10 ** (-31.45 / 20)  # the load of the AA180-VNACKN: VSWR 1,055, U = 0,0268 on each part


def model_standards(library, specs_of_through=None):
    """The standards of the kit model (the specifications of its datasheet), optionally with those of the through."""
    model = library.models["aa180_vnackn"]
    if specs_of_through is not None:
        next(s for s in model.standards if s.id == "through_hm").specifications = specs_of_through
    settings = library.kit_settings(model)
    resolved = [ResolvedStandard(s, None, model.source_path.parent) for s in model.standards]
    return resolved, settings, kit_frequencies(settings), (lambda r: library.standard_data(r, settings))


def at(f, ghz: float) -> int:
    return int(np.argmin(abs(f - ghz * 1e9)))


def test_osm_is_the_lagrange_basis_of_the_three_standards(library):
    resolved, _settings, f, data_of = model_standards(library)
    standards = calibration_standards(resolved, "OSM", ("macho",), f, data_of)
    actual = {kind: v.values for kind, v in standards.port1.items()}
    gamma = 0.4 * np.exp(1j * 0.7)
    nominal = [v.values.astype(complex) for v in standards.variables]
    for i, kind in enumerate(("abierto", "cortocircuito", "carga")):
        plus, minus = list(nominal), list(nominal)
        plus[i], minus[i] = nominal[i] + 1e-6, nominal[i] - 1e-6
        derivative = (_solve(standards, plus, reflection_dut(f, gamma))[0] - _solve(standards, minus, reflection_dut(f, gamma))[0])[:, 0, 0] / 2e-6
        others = [actual[k] for k in actual if k != kind]
        lagrange = (gamma - others[0]) * (gamma - others[1]) / ((actual[kind] - others[0]) * (actual[kind] - others[1]))
        assert np.allclose(derivative, lagrange, rtol=1e-6, atol=1e-8), kind
        # holomorphic: the same derivative in the imaginary direction
        plus[i], minus[i] = nominal[i] + 1e-6j, nominal[i] - 1e-6j
        imaginary = (_solve(standards, plus, reflection_dut(f, gamma))[0] - _solve(standards, minus, reflection_dut(f, gamma))[0])[:, 0, 0] / 2e-6j
        assert np.allclose(imaginary, derivative, rtol=1e-6, atol=1e-8)


def test_osm_with_the_specifications_of_the_datasheet(library):
    resolved, _settings, f, data_of = model_standards(library)
    standards = calibration_standards(resolved, "OSM", ("macho",), f, data_of)
    # a matched DUT sees only the load: U = 0,0268 in each part, its effective directivity −31,45 dB
    matched = kit_contribution(standards, reflection_dut(f, 0))
    u = matched.breakdown(0, 0, "re")
    assert np.allclose(u["total"], RL) and np.allclose(u["Puerto 1: BROADBAND LOAD -M-"], RL) and not u["Puerto 1: OPEN -M-"].any()
    terms = {(t.name, t.port): t for t in matched.terms}
    assert np.allclose(terms[("Directividad", "puerto 1")].db, 31.45, atol=1e-6)
    # ideal open (1), short (−1) and load (0): source match (δO + δS)/2 − δL and tracking (δO − δS)/2
    source = terms[("Adaptación de fuente", "puerto 1")].radius
    k = at(f, 1)
    assert source[k] == pytest.approx(math.hypot(RL, math.radians(1.5) / math.sqrt(2)), rel=0.02)
    assert terms[("Seguimiento de reflexión", "puerto 1")].phase_deg[k] == pytest.approx(1.5 / math.sqrt(2), rel=0.02)
    # the open and the short: only through the phase; a DUT equal to the open has the U of the open
    opened = standards.port1["abierto"].values
    same = kit_contribution(standards, opened.reshape(-1, 1, 1))
    assert np.allclose(same.breakdown(0, 0, "fase")["total"], 1.5, rtol=1e-5)
    # one input per specification: the contribution is fully correlated between frequencies
    assert np.allclose(np.abs(frequency_matrix(matched.data, "S11", "re").correlation), 1.0)


def test_tosm_with_a_flush_and_with_a_defined_through(library):
    resolved, _settings, f, data_of = model_standards(library, [SpecRow(frecuencia=0, perdidas_retorno_db=-40, transmision_modulo_db=0.05, transmision_fase_grados=0.5)])
    flush = calibration_standards(resolved, "TOSM", ("macho", "hembra"), f, data_of)
    thru = kit_contribution(flush, two_port_dut(f, 0.0))
    assert not np.any(thru.breakdown(1, 0, "db")["total"]) and not np.any(thru.breakdown(0, 0, "modulo")["total"])  # the through itself
    attenuator = kit_contribution(flush, two_port_dut(f, -20.0, -20.0))
    assert attenuator.breakdown(1, 0, "db")["total"].max() > 0.01 and attenuator.breakdown(0, 0, "modulo")["total"][at(f, 1)] == pytest.approx(RL, rel=0.05)
    terms = {(t.name, t.port): t for t in attenuator.terms}
    assert np.allclose(terms[("Adaptación de carga", "puerto 2")].db, 31.45, atol=1e-3)  # the directivity of port 1
    assert not terms[("Seguimiento de transmisión", "1 → 2")].magnitude_db.any()  # first order: 0 with a flush through
    # a defined through: measured again, it gives its definition: U of S21 = its specification
    defined = calibration_standards(resolved, "TOSM", ("macho", "hembra"), f, data_of, through="through_hm")
    assert set(defined.through) == {"S11", "S21", "S12", "S22"}
    again = kit_contribution(defined, defined.through["S21"].data.s)
    u = again.breakdown(1, 0, "db")
    assert np.allclose(u["total"], 0.05, rtol=1e-4) and np.allclose(u["Through: THRU"], 0.05, rtol=1e-4)
    assert np.allclose(again.breakdown(1, 0, "fase")["total"], 0.5, rtol=1e-4)


def test_uosm_does_not_use_the_through(library):
    resolved, _settings, f, data_of = model_standards(library)
    standards = calibration_standards(resolved, "UOSM", ("macho", "hembra"), f, data_of)
    assert not standards.through
    result = kit_contribution(standards, two_port_dut(f, 0.0))
    # the transmission tracking is √(T₁·T₂): its phase, the mean of those of the two ports (1,5°/√2 each)
    assert result.breakdown(1, 0, "fase")["total"][at(f, 1)] == pytest.approx(1.5 / 2, rel=0.02)
    assert set(result.groups) == {f"Puerto {p}: {name} -{g}-" for p, g in ((1, "M"), (2, "F")) for name in ("OPEN", "SHORT", "BROADBAND LOAD")}
    terms = {(t.name, t.port): t for t in result.terms}
    assert np.allclose(terms[("Adaptación de carga", "puerto 2")].radius, terms[("Adaptación de fuente", "puerto 2")].radius)  # e22f = e22


def test_transmission_normalization(library):
    resolved, _settings, f, data_of = model_standards(library, [SpecRow(frecuencia=0, transmision_modulo_db=0.05, transmision_fase_grados=0.5)])
    flush = calibration_standards(resolved, "normalizacion", (), f, data_of)
    assert not flush.variables and not np.any(kit_contribution(flush, two_port_dut(f, -10.0)).breakdown(1, 0, "db")["total"])
    defined = calibration_standards(resolved, "normalizacion", (), f, data_of, through="through_hm")
    result = kit_contribution(defined, two_port_dut(f, -10.0, -20.0))
    assert np.allclose(result.breakdown(1, 0, "db")["total"], 0.05, rtol=1e-4) and np.allclose(result.breakdown(1, 0, "fase")["total"], 0.5, rtol=1e-4)
    assert not np.any(result.breakdown(0, 0, "modulo")["total"])  # the reflections are not corrected


def test_the_real_values_of_the_kit_in_the_contribution(library):
    """KIT-N-02: the own specifications of its standards (its calibration of 01/09/2026)."""
    kit = library.equipment["KIT-N-02"]
    model = library.model_of(kit)
    standards = library.calibration_standards(model, kit, "OSM", ("macho",))
    f = standards.frequencies
    result = kit_contribution(standards, reflection_dut(f, 0))
    # a matched DUT: the own specification of the male load, by bands
    assert result.breakdown(0, 0, "re")["total"][at(f, 17.75)] == pytest.approx(10 ** (-35.0 / 20), rel=1e-3)
    terms = {(t.name, t.port): t for t in result.terms}
    assert terms[("Adaptación de fuente", "puerto 1")].db[at(f, 18)] < 19  # the male open: 14,1° at 18 GHz
    with pytest.raises(Exception, match="macho en uso"):
        kit.standards["abierto_m"].state = "danado"
        library.calibration_standards(model, kit, "OSM", ("macho",))


def test_interpolation_keeps_the_uncertainty():
    data = load_sparam_data(METAS_LOAD)
    f = data.frequencies
    k = int(np.argmin(abs(f - 10e9)))
    middle = (f[k] + f[k + 1]) / 2
    resampled = data.interpolated([f[k], middle, f[k + 1]])
    assert resampled.s[1, 0, 0] == pytest.approx((data.s[k, 0, 0] + data.s[k + 1, 0, 0]) / 2)
    u = data.standard_uncertainty()[0][:, 0, 0]
    assert resampled.standard_uncertainty()[0][0, 0, 0] == pytest.approx(u[k])
    assert resampled.standard_uncertainty()[0][1, 0, 0] == pytest.approx(math.hypot(u[k], u[k + 1]) / 2)  # own inputs at each frequency
    assert data.interpolated(f) is data and data.interpolated([1.0]).s[0, 0, 0] == data.s[0, 0, 0]  # below the data: the first point


def test_a_dut_of_the_wrong_size(library):
    resolved, _settings, f, data_of = model_standards(library)
    standards = calibration_standards(resolved, "OSM", ("hembra",), f, data_of)
    with pytest.raises(Exception, match="1 puerto"):
        kit_contribution(standards, two_port_dut(f))
    assert isinstance(standards, CalibrationStandards) and standards.ports == 1
