from __future__ import annotations

import numpy as np
import pytest
from conftest import TODAY

from calelektra.core.frequency import FrequencyRange
from calelektra.procedures.cable_specs import (
    CableSpecification,
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
    assert spec.connector_vswr == (1.15, 1.15)  # B: the one of A
    gamma = 2 * 0.15 / 2.15
    assert spec.cable_gamma == pytest.approx(gamma)
    assert spec.cable_vswr == pytest.approx((1 + gamma) / (1 - gamma))  # 1.3243
    assert spec.max_vswr == pytest.approx(spec.cable_vswr * 1.10)
    assert spec.min_return_loss_db == pytest.approx(-20 * np.log10(gamma_of_vswr(spec.max_vswr)))  # 14.61 dB
    different = cable_specification({**FIELDS, "vswr_conector_b": 1.25}, 0, 0, None)
    assert different.cable_gamma == pytest.approx(0.15 / 2.15 + 0.25 / 2.25)


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
    assert np.allclose(again.max_insertion_loss(f), spec.max_insertion_loss(f)) and again.max_vswr == pytest.approx(spec.max_vswr)
    curves = again.curves(("S21", "S11"))
    assert [c.parameter for c in curves] == ["S21", "S11"] and curves[0].frequencies[-1] == 18e9
    assert np.allclose(curves[1].values, -spec.min_return_loss_db)


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
