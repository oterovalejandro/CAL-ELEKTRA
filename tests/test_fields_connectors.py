"""Typed category fields (resolution model → equipment) and connector rules."""

from datetime import date

import pytest

from calelektra.catalog.fields import FieldError, FieldSpec, NumberRange, coerce, display, resolve, validate_values
from calelektra.core.connectors import Connector, ConnectorError, mating_issue
from calelektra.core.frequency import FrequencyRange
from calelektra.core.issues import Severity


def spec(kind, **kw):
    return FieldSpec(etiqueta="x", tipo=kind, **kw)


def test_coerce_types():
    assert coerce(spec("numero"), "2,5") == 2.5
    assert coerce(spec("entero"), 3.0) == 3
    with pytest.raises(FieldError):
        coerce(spec("entero"), 2.5)
    with pytest.raises(FieldError):
        coerce(spec("numero", minimo=0), -1)
    assert coerce(spec("booleano"), "sí") is True
    assert coerce(spec("frecuencia"), "1,5 GHz") == 1.5e9
    assert coerce(spec("rango_frecuencia"), ["DC", "18 GHz"]) == FrequencyRange(0, 18e9)
    assert coerce(spec("rango_numero"), [-40, 0]) == NumberRange(-40, 0)
    assert coerce(spec("conector"), "N hembra") == Connector("N", "hembra")
    assert coerce(spec("familia_conector"), "K") == "2.92 mm"
    assert coerce(spec("fecha"), "2026-10-19") == date(2026, 10, 19)
    with pytest.raises(FieldError):
        coerce(spec("opcion", opciones=["a", "b"]), "c")
    assert coerce(spec("multiopcion", opciones=["H", "V"]), "H") == ["H"]
    assert display(spec("numero", unidad="dB/m"), 0.45) == "0.45 dB/m"


def test_resolution_order_and_levels():
    specs = {
        "a": spec("numero", nivel="modelo"),
        "b": spec("numero"),
        "c": spec("numero", nivel="equipo", por_defecto=7),
    }
    resolved = resolve(specs, {"a": 1, "b": 2, "c": 99}, {"a": 10, "b": 20})
    assert (resolved["a"].value, resolved["a"].source) == (1, "modelo")  # level «modelo»: the unit cannot override
    assert (resolved["b"].value, resolved["b"].source) == (20, "equipo")
    assert (resolved["c"].value, resolved["c"].source) == (7, "defecto")  # model value not allowed at this level
    issues = validate_values(specs, {"a": 1, "z": 3, "b": "x"}, "equipo", "Equipo E")
    assert {i.code for i in issues} == {"campo.nivel", "campo.desconocido", "campo.valor"}


def test_library_fields_and_ports(library):
    vna = library.equipment["VNA-01"]
    assert library.field(vna, "num_puertos") == 2
    assert library.field(vna, "conector_puertos") == Connector("2.92 mm", "macho")
    assert [p.name for p in library.ports_of(vna)] == ["P1", "P2"]
    assert library.port_connector(vna, "P2") == Connector("2.92 mm", "macho")
    cable = library.equipment["CAB-DUT-0002"]
    assert library.field(cable, "longitud_m") == 0.5  # equipment value
    assert library.port_connector(cable, "B") == Connector("2.92 mm", "hembra")


def test_connector_parsing_and_mating():
    assert Connector.parse({"familia": "2,92 mm", "genero": "m"}) == Connector("2.92 mm", "macho")
    assert Connector.parse("SMA(f)") == Connector("SMA", "hembra")
    assert Connector.parse("PC-7").gender == "sin_genero"
    with pytest.raises(ConnectorError):
        Connector.parse("N")
    n_m, n_f = Connector("N", "macho"), Connector("N", "hembra")
    assert mating_issue(n_m, n_f) is None
    assert mating_issue(n_f, n_f)[0] is Severity.ERROR
    assert mating_issue(Connector("3.5 mm", "macho"), Connector("2.92 mm", "hembra"))[0] is Severity.WARNING
    assert mating_issue(n_m, Connector("2.92 mm", "hembra"))[0] is Severity.ERROR
    assert Connector("2.92 mm", "macho").znx_name() == "PC292MALE"
