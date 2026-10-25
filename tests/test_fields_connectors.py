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
    assert library.field(vna, "conector_puertos") == Connector("NMD 2.92 mm", "macho")
    assert [p.name for p in library.ports_of(vna)] == ["P1", "P2"]
    assert library.port_connector(vna, "P2") == Connector("NMD 2.92 mm", "macho")
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


def test_nmd_test_port_connectors():
    port, cable_end = Connector("NMD 2.92 mm", "macho"), Connector.parse("NMD2.92mm (f)")
    assert cable_end == Connector("NMD 2.92 mm", "hembra") and port.label() == "NMD 2,92 mm macho"
    # NMD 2,92 mm follows the rules of 2,92 mm: no other limitation
    assert mating_issue(port, cable_end) is None and mating_issue(port, Connector("2.92 mm", "hembra")) is None
    assert mating_issue(cable_end, Connector("2.92 mm", "macho")) is None
    assert mating_issue(port, Connector("3.5 mm", "hembra"))[0] is Severity.WARNING
    assert mating_issue(cable_end, Connector("NMD 2.92 mm", "hembra"))[0] is Severity.ERROR  # same gender
    assert port.znx_name() == "PC292MALE" and cable_end.znx_name() == "PC292FEMALE"


def test_transitions_cables_and_kit_of_the_catalogue(library):
    def summary(equipment_id):
        equipment = library.equipment[equipment_id]
        a, b = (library.field(equipment, key) for key in ("conector_a", "conector_b"))
        return library.model_of(equipment).display_name, f"{a} – {b}", str(library.frequency_range_of(equipment).range), library.field(equipment, "vswr_max")

    assert summary("ADP-NMD292-02") == ("Sainty-Tech 852D/362D-3767B1", "NMD 2,92 mm hembra – 2,92 mm (K) hembra", "DC – 40 GHz", 1.2)
    assert summary("ADP-NMDN-01") == ("Pasternack PE9983", "NMD 2,92 mm hembra – N 50 Ω hembra", "DC – 18 GHz", 1.25)
    assert summary("ADP-292N-01") == ("Sainty-Tech 362D/272D-6765B", "2,92 mm (K) hembra – N 50 Ω hembra", "DC – 18 GHz", 1.15)
    assert summary("ADP-292RA-01")[:2] == ("Sainty-Tech 361D/362D-6767CW (ángulo recto)", "2,92 mm (K) macho – 2,92 mm (K) hembra")
    assert library.model_of(library.equipment["ADP-292BH-01"]).display_name == "Sainty-Tech 362D/362D-6767BY2 (pasamuros)"
    assert library.field(library.equipment["ADP-292BH-01"], "forma") is None  # the shape is in the name, not a field
    cable = library.equipment["CAB-PT-NMD-02"]
    assert summary("CAB-PT-NMD-02")[1:] == ("NMD 2,92 mm hembra – 2,92 mm (K) macho", "DC – 40 GHz", 1.3)
    assert library.field(cable, "longitud_m") == 0.63 and library.field(cable, "perdidas_insercion_max_db") == 2.8
    assert library.field(cable, "estabilidad_fase_grados") == 2.5
    vna_port = library.port_connector(library.equipment["VNA-01"], "P1")
    assert mating_issue(vna_port, library.field(cable, "conector_a")) is None
    kit = library.equipment["KIT-N-02"]
    assert library.model_of(kit).designation == "AA180-VNACKN" and library.field(kit, "familia_conector") == "N"
    assert library.field(kit, "vswr_carga_max") == 1.055 and len(library.field(kit, "componentes")) == 12
    assert not library.load_issues
