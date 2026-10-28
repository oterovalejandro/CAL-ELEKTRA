from __future__ import annotations

import numpy as np
import pytest
from conftest import TODAY, renew_calibration

from calelektra.core.bands import BandValues
from calelektra.core.frequency import FrequencyRange
from calelektra.core.networks import transmission_delay
from calelektra.procedures.cable_specs import (
    CableSpecification,
    cable_delay,
    cable_specification,
    gamma_of_vswr,
)
from calelektra.procedures.engine import AutoOperator, ProcedureRunner, prepare_run
from calelektra.procedures.model import CableTolerances, ProcedureTemplate

FIELDS = {
    "k1_atenuacion": 0.26,
    "k2_atenuacion": 0.00091,
    "unidad_frecuencia_k": "MHz",
    "longitud_normalizacion_k": "100 ft",
    "vswr_conector_a": 1.15,
    "vswr_conector_b": None,
    "longitud_m": 1.5,
}


def test_nominal_attenuation_with_the_units_of_k1_and_k2():
    spec = cable_specification(FIELDS, 0.5, 10, FrequencyRange(0, 40e9))
    # A = (0.26·√40000 + 0.00091·40000) dB/100 ft · 1.5 m / 30.48 m
    assert spec.nominal_attenuation(np.array([40e9]))[0] == pytest.approx((0.26 * 200 + 0.00091 * 40000) * 1.5 / 30.48)
    assert spec.max_insertion_loss(np.array([40e9]))[0] == pytest.approx(4.3504 + 0.5, abs=1e-4)
    # the same cable with K1, K2 per metre and f in GHz
    k1 = 0.26 * np.sqrt(1000) / 30.48
    k2 = 0.00091 * 1000 / 30.48
    same = cable_specification({**FIELDS, "k1_atenuacion": k1, "k2_atenuacion": k2, "unidad_frecuencia_k": "GHz", "longitud_normalizacion_k": "1 m"}, 0, 0, None)
    f = np.array([1e6, 1e9, 18e9, 40e9])
    assert np.allclose(same.nominal_attenuation(f), spec.nominal_attenuation(f))
    inches = cable_specification({**FIELDS, "unidad_frecuencia_k": "Hz", "longitud_normalizacion_k": "1 in"}, 0, 0, None)
    assert inches.nominal_attenuation(np.array([1e6]))[0] == pytest.approx((0.26 * 1000 + 0.00091 * 1e6) * 1.5 / 0.0254)


def test_cable_vswr_is_the_linear_sum_of_the_connector_reflections():
    spec = cable_specification(FIELDS, 0.5, 10, None)
    assert spec.connector_vswr == (BandValues.single(1.15), BandValues.single(1.15))  # B: the one of A
    f = np.array([1e9])
    gamma = 2 * 0.15 / 2.15
    assert spec.cable_gamma(f)[0] == pytest.approx(gamma)
    assert spec.cable_vswr(f)[0] == pytest.approx((1 + gamma) / (1 - gamma))  # 1.3243, as declared
    # the tolerance goes on the VSWR of the assembled cable (the laboratory)
    vswr_max = (1 + gamma) / (1 - gamma) * 1.10
    assert spec.max_vswr(f)[0] == pytest.approx(vswr_max)  # 1.4568
    assert spec.min_return_loss_db(f)[0] == pytest.approx(-20 * np.log10(gamma_of_vswr(vswr_max)))  # 14.61 dB
    # or on each connector, before the sum
    per_connector = cable_specification(FIELDS, 0.5, 10, None, "conectores")
    gamma_max = 2 * gamma_of_vswr(1.15 * 1.10)
    assert per_connector.max_vswr(f)[0] == pytest.approx((1 + gamma_max) / (1 - gamma_max))  # 1.611
    different = cable_specification({**FIELDS, "vswr_conector_b": 1.25}, 0, 0, None)
    assert different.cable_gamma(f)[0] == pytest.approx(0.15 / 2.15 + 0.25 / 2.25)


def test_vswr_by_bands_and_the_certificate_of_the_lab():
    # SUCOFLEX 526S, 2,5 m: a = 0,229 and b = 0,0071 dB/m (f in GHz), connectors 1,12:1 up to 18 GHz and
    # 1,16:1 up to 26,5 GHz, tolerances 1 dB and 5 %
    fields = {
        "k1_atenuacion": 0.229, "k2_atenuacion": 0.0071, "unidad_frecuencia_k": "GHz", "longitud_normalizacion_k": "1 m",
        "longitud_m": 2.5, "vswr_conector_a": "1,12 hasta 18 GHz; 1,16 hasta 26,5 GHz", "vswr_conector_b": None,
    }
    f = np.array([10e6, 17e9, 18e9, 19e9, 26.5e9])
    # on the assembled cable: 1.2553 and 1.3478 (connectors summed) + 5 %
    spec = cable_specification(fields, 1.0, 5.0, FrequencyRange(10e6, 26.5e9))
    assert spec.max_vswr(f) == pytest.approx([1.3181, 1.3181, 1.3181, 1.4152, 1.4152], abs=5e-5)
    assert spec.return_loss_criterion().startswith("VSWR ≤ 1.318 hasta 18 GHz; 1.415 hasta 26.5 GHz: cable 1.255 hasta 18 GHz")
    # on each connector: the limits of the example certificate of the laboratory
    per_connector = cable_specification(fields, 1.0, 5.0, FrequencyRange(10e6, 26.5e9), "conectores")
    assert per_connector.max_vswr(f) == pytest.approx([1.3860, 1.3860, 1.3860, 1.4893, 1.4893], abs=5e-5)
    assert per_connector.return_loss_criterion().startswith("VSWR ≤ 1.386 hasta 18 GHz; 1.489 hasta 26.5 GHz: conectores 1.12 hasta 18 GHz")
    assert -spec.max_insertion_loss(np.array([10e6, 4e9, 26.5e9])) == pytest.approx([-1.0574, -2.2160, -4.4175], abs=5e-5)
    assert np.isnan(spec.max_vswr(np.array([27e9]))[0])  # above the last band
    record = per_connector.to_record()
    assert record["vswr_max"] == [{"hasta": "18 GHz", "valor": 1.38596}, {"hasta": "26.5 GHz", "valor": 1.48934}]
    assert record["tolerancia_vswr_sobre"] == "conectores" and spec.to_record()["tolerancia_vswr_sobre"] == "cable"
    again = CableSpecification.from_record(record)
    assert np.allclose(again.max_vswr(f), per_connector.max_vswr(f)) and again.vswr_tolerance_on == "conectores"
    # a record keeps the limit it was evaluated with
    old = CableSpecification.from_record({"vswr_conector_a": 1.15, "tolerancia_vswr_pct": 10, "vswr_max": 1.45676})
    assert old.max_vswr(np.array([1e9]))[0] == pytest.approx(1.45676)


def test_values_by_frequency_band():
    bands = BandValues.parse("1,12 hasta 18 GHz; 1,16 hasta 26,5 GHz")
    assert bands == BandValues.parse([{"hasta": "18 GHz", "valor": 1.12}, {"hasta": "26.5 GHz", "valor": 1.16}])
    assert str(bands) == "1,12 hasta 18 GHz; 1,16 hasta 26,5 GHz" and bands.limits == [18e9, 26.5e9]
    assert bands.to_yaml() == [{"hasta": "18 GHz", "valor": 1.12}, {"hasta": "26.5 GHz", "valor": 1.16}]
    assert BandValues.parse(1.15).to_yaml() == 1.15 and str(BandValues.parse("1,15")) == "1,15"
    open_end = BandValues.parse("1,1 hasta 6 GHz; 1,2")
    assert open_end.at(np.array([6e9, 40e9])).tolist() == [1.1, 1.2]
    for wrong in ("1,12 hasta 26 GHz; 1,16 hasta 18 GHz", "1,1; 1,2 hasta 6 GHz", "uno"):
        with pytest.raises(ValueError):
            BandValues.parse(wrong)


def test_missing_fields_say_what_is_not_evaluated():
    assert cable_specification({key: None for key in FIELDS}, 0, 0, None) is None  # no formula: S2P files only
    no_length = cable_specification({**FIELDS, "longitud_m": None}, 0, 0, None)
    assert not no_length.has_attenuation and no_length.has_return_loss and "longitud del cable" in no_length.problems[0]
    no_units = cable_specification({**FIELDS, "unidad_frecuencia_k": None}, 0, 0, None)
    assert not no_units.has_attenuation and "unidad de frecuencia" in no_units.problems[0]
    impossible = cable_specification({**FIELDS, "vswr_conector_a": 20.0}, 0, 0, None)
    assert not impossible.has_return_loss and "llega a 1" in impossible.problems[0]


def test_evaluation_inside_the_nominal_range_and_record():
    spec = cable_specification(FIELDS, 0.5, 10, FrequencyRange(0, 18e9))
    f = np.array([1e9, 10e9, 18e9, 30e9])
    s = np.zeros((4, 2, 2), dtype=complex)
    loss = spec.nominal_attenuation(f) + np.array([0.1, 0.2, 0.6, 5.0])  # 18 GHz: 0.1 dB over the limit; 30 GHz: outside
    s[:, 1, 0] = s[:, 0, 1] = 10 ** (-loss / 20)
    s[:, 0, 0] = s[:, 1, 1] = 0.05
    insertion, ret = spec.evaluate(f, s, ("S11", "S21", "S12", "S22"))
    assert insertion["resultado"] == "FALLA" and insertion["parametros"][0]["fallos"] == 1
    assert insertion["parametros"][0]["fuera_de_rango"] == 1 and insertion["margen_minimo"] == pytest.approx(-0.1, abs=1e-6)
    assert ret["resultado"] == "PASA" and ret["criterio"].startswith("VSWR ≤ 1.457")
    only_s21 = spec.evaluate(f, s, ("S21",))
    assert [e["nombre"] for e in only_s21] == [insertion["nombre"], ret["nombre"]] and only_s21[1]["resultado"] == "NO_EVALUADO"
    again = CableSpecification.from_record(spec.to_record())
    assert np.allclose(again.max_insertion_loss(f), spec.max_insertion_loss(f)) and np.allclose(again.max_vswr(f), spec.max_vswr(f))
    curves = again.curves(("S21", "S11"))
    assert [c.parameter for c in curves] == ["S21", "S11"] and curves[0].frequencies[-1] == 18e9
    assert np.allclose(curves[1].values, -spec.min_return_loss_db(curves[1].frequencies))


def test_cable_test_by_formula(library):
    template = library.procedures["cable_CAB-DUT-0004"]
    assert template.cable_tolerances == CableTolerances(perdidas_insercion_db=0.5, vswr_pct=10)
    plan = prepare_run(library, template, TODAY)
    assert plan.ok and plan.cable_spec is not None and plan.cable_spec.length_m == 1.5
    record = ProcedureRunner(library, template, AutoOperator(), today=TODAY, seed=1).run().record
    assert record.verdict == "PASA" and [e["nombre"][:12] for e in record.spec_results] == ["Pérdidas de ", "Pérdidas de "]
    assert record.cable_specification["vswr_max"] == pytest.approx(1.45676, abs=1e-5) and record.cable_specification["longitud_m"] == 1.5
    # without tolerances the limits are the nominal values: the measured loss is a little above A(f)
    strict = template.model_copy(update={"cable_tolerances": CableTolerances()})
    plan = prepare_run(library, strict, TODAY)
    assert any("no fija las tolerancias" in issue.message for issue in plan.issues)
    record = ProcedureRunner(library, strict, AutoOperator(), today=TODAY, seed=1).run().record
    assert record.verdict == "FALLA" and record.spec_results[0]["resultado"] == "FALLA"


def test_tolerances_in_the_template_yaml():
    data = {"id": "x", "nombre": "x", "algoritmo": "vna_2_puertos", "tolerancias_cable": {"perdidas_insercion_db": 0.3, "vswr_pct": 5}}
    template = ProcedureTemplate.model_validate(data)
    assert template.cable_tolerances.insertion_loss_db == 0.3 and template.to_yaml_dict()["tolerancias_cable"] == {"perdidas_insercion_db": 0.3, "vswr_pct": 5.0}
    assert "tolerancias_cable" not in ProcedureTemplate.model_validate({**data, "tolerancias_cable": {}}).to_yaml_dict()
    with pytest.raises(ValueError):
        ProcedureTemplate.model_validate({**data, "tolerancias_cable": {"vswr_pct": -1}})


def test_delay_of_a_transmission_with_a_coarse_list_of_frequencies():
    # the list of the N setup: 10 MHz steps up to 1 GHz, then 100 MHz: a long cable turns > 180° between points
    f = np.concatenate([np.arange(30e6, 1000e6, 10e6), np.arange(1000e6, 18000e6 + 1, 100e6)])
    for tau in (2.4e-9, 9.53e-9, 40e-9):
        s21 = 0.8 * np.exp(-2j * np.pi * f * tau + 0.4j)
        assert transmission_delay(f, s21) == pytest.approx(tau, rel=1e-9)
    # a uniform grid too coarse for the delay: ambiguous by 1/Δf; the nominal picks the turn
    grid = np.arange(1, 801) * 50e6
    s21 = np.exp(-2j * np.pi * grid * 12.996e-9)
    assert transmission_delay(grid, s21) == pytest.approx(12.996e-9 - 20e-9)
    assert transmission_delay(grid, s21, nominal=12.9e-9) == pytest.approx(12.996e-9)
    short = np.exp(-2j * np.pi * grid * 2.38e-9)
    assert transmission_delay(grid, short, nominal=3e-9) == pytest.approx(2.38e-9)  # resolved: the nominal is not used


def test_nominal_delay_from_the_datasheet():
    delay = cable_delay({"retardo_ns_m": 4.3, "velocidad_propagacion_pct": 77, "longitud_m": 3.0})
    assert delay.nominal_ns == pytest.approx(12.9) and delay.source == "retardo nominal" and not delay.problems
    from_velocity = cable_delay({"retardo_ns_m": None, "velocidad_propagacion_pct": 77, "longitud_m": 3.0})
    assert from_velocity.per_metre_ns == pytest.approx(1e9 / (0.77 * 299792458)) == pytest.approx(4.332, abs=1e-3)
    assert "velocidad de propagación 77 %" in from_velocity.text()
    mismatch = cable_delay({"retardo_ns_m": 4.0, "velocidad_propagacion_pct": 77, "longitud_m": 1})
    assert "no concuerda" in mismatch.problems[0]
    assert cable_delay({"retardo_ns_m": 4.3, "longitud_m": None}) is None
    summary = delay.summary(12.996)
    assert summary["diferencia_ns"] == pytest.approx(0.096) and summary["longitud_estimada_m"] == pytest.approx(3.0223, abs=1e-4)


def test_sucoflex_from_its_datasheet(library):
    template = library.procedures["cable_CAB-DUT-0005"]
    renew_calibration(library, "KIT-N-01")
    plan = prepare_run(library, template, TODAY)
    assert plan.ok and plan.cable_delay.nominal_ns == pytest.approx(12.9)
    assert plan.cable_spec.has_attenuation and not plan.cable_spec.has_return_loss  # the datasheet has no connector VSWR
    assert plan.cable_spec.nominal_attenuation(np.array([1e9, 18e9])) == pytest.approx([0.157 * 3, 0.764 * 3], abs=2e-3)  # its table
    operator = AutoOperator()
    record = ProcedureRunner(library, template, operator, today=TODAY, seed=1).run().record
    assert record.verdict == "PASA" and len(record.spec_results) == 1
    delay = record.results["retardo_nominal"]
    assert record.results["retardo_ns"] == pytest.approx(12.996, abs=1e-3)  # the simulated cable propagates at 77 %
    assert delay["nominal_ns"] == 12.9 and delay["diferencia_ns"] == pytest.approx(0.096, abs=1e-3)
    assert [r.details for r in operator.requests if r.step == "medida"] == [["Radio de curvatura mínimo del cable: 70 mm al moverlo, 50 mm fijo"]]
    # the delay of the N cable measured on the same list (10 MHz, then 100 MHz steps) is no longer aliased
    record = ProcedureRunner(library, library.procedures["cable_CAB-DUT-0001"], AutoOperator(), today=TODAY, seed=1).run().record
    assert record.results["retardo_ns"] == pytest.approx(2.0 / (0.7 * 299792458) * 1e9, abs=1e-3)


def test_impedance_of_the_cable_against_the_vna(library):
    model = library.models["hs_sucoflex_118_a"]
    model.values["impedancia_nominal_ohm"] = 75.0
    library.save_model(model)
    plan = prepare_run(library, library.procedures["cable_CAB-DUT-0005"], TODAY)
    assert any(i.code == "ensayo.impedancia" and "75 Ω" in i.message and "50 Ω" in i.message for i in plan.issues)
