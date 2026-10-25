"""Paths of the hardware setups: from each VNA port to the port of the DUT."""

from calelektra.catalog.model import Equipment, EquipmentModel
from calelektra.core.issues import Severity
from calelektra.setups.analysis import analyze_setup
from calelektra.setups.model import PathItem
from calelektra.setups.paths import expand, resolve_paths

from conftest import TODAY, setup_with_dut


def test_expansion_turns_the_paths_into_roles_connections_and_planes(library):
    setup = setup_with_dut(library, "componente_292_tosm", "CAB-DUT-0003")
    expanded = expand(library.resolved_template(setup.template), setup, library)
    roles = list(expanded.template.roles)
    assert roles[:3] == ["vna", "P1-1", "P2-1"]  # the equipment of the paths right after the VNA
    assert expanded.setup.assignments["P1-1"] == "CAB-PT-292-01" and expanded.dut_ports == ["A", "B"]
    measurement = [(c.a, c.b) for c in expanded.template.connection_states["medida"].connections]
    assert measurement == [("vna.P1", "P1-1.A"), ("vna.P2", "P2-1.A"), ("P1-1.B", "dut.A"), ("dut.B", "P2-1.B")]
    calibration = [(c.a, c.b) for c in expanded.template.connection_states["calibracion"].connections]
    assert calibration == [("vna.P1", "P1-1.A"), ("vna.P2", "P2-1.A")]  # the paths stay connected
    assert expanded.template.reference_planes == {
        "PR1": {"en": ["P1-1.B"], "puerto_vna": 1, "camino": "P1"},
        "PR2": {"en": ["P2-1.B"], "puerto_vna": 2, "camino": "P2"},
    }
    assert library.resolved_template(setup.template).reference_planes == {}  # the template itself is not changed


def test_empty_paths_leave_the_planes_on_the_vna_ports(library):
    [p1, p2] = resolve_paths(library.resolved_template("camino_rf_2p_s"), library.setups["cable_vna_292"], library)
    assert p1.elements == [] and p1.end_endpoint == "vna.P1" and p1.end_connector.label() == "NMD 2,92 mm macho"
    assert p2.dut_connector.label() == "2,92 mm (K) hembra"  # the ports of the VNA are NMD: they take standard 2,92 mm females
    assert p1.describe() == "P1 (NMD 2,92 mm macho) → PR1: NMD 2,92 mm macho"


def test_orientation_follows_the_connectors_or_the_setup(library):
    setup = library.setups["cable_N_18G"]
    setup.paths["P1"].append(PathItem(equipment="PATH-TX-01"))  # A is 2,92 mm female: only B (N male) mates the N female end
    template = library.resolved_template(setup.template)
    path = resolve_paths(template, setup, library)[0]
    chamber = path.elements[1]
    assert (chamber.vna_side, chamber.dut_side, chamber.reversed, chamber.automatic) == ("B", "A", True, True)
    assert path.end_connector.label() == "2,92 mm (K) hembra"
    analysis = analyze_setup(setup, library, TODAY)
    detail = next(c.detail for c in analysis.checks if c.subject == "Camino P1")
    assert "orientación elegida por los conectores: PATH-TX-01" in detail

    setup = library.setups["cable_N_18G"]
    setup.paths["P2"] = [PathItem(equipment="CAB-PT-N-02", reversed=True)]  # N female towards the 2,92 mm male port
    analysis = analyze_setup(setup, library, TODAY)
    errors = [i.message for i in analysis.issues if i.code == "conexion.conectores" and i.severity is Severity.ERROR]
    assert errors and "VNA-01.P2" in errors[0] and "familias incompatibles" in errors[0]
    assert analysis.paths[1].elements[0].vna_side == "B" and analysis.paths[1].elements[0].reversed


def test_directional_equipment_follows_the_direction_of_the_path(library):
    library.models["preamp_x"] = EquipmentModel.model_validate(
        {
            "formato": "calelektra/modelo@0.3",
            "id": "preamp_x",
            "categoria": "preamplificador",
            "fabricante": "Genérico",
            "modelo": "Preamplificador",
            "campos": {
                "rango_frecuencia": ["1 GHz", "18 GHz"],
                "conector_in": {"familia": "N", "genero": "hembra"},
                "conector_out": {"familia": "N", "genero": "hembra"},
            },
        }
    )
    library.equipment["PRE-01"] = Equipment.model_validate({"formato": "calelektra/equipo@0.3", "id": "PRE-01", "modelo": "preamp_x"})
    setup = library.setups["sac_nsa_3m"]
    setup.paths["P2"].insert(0, PathItem(equipment="PRE-01"))
    rx = resolve_paths(library.resolved_template("sac_nsa"), setup, library)[1]
    assert (rx.elements[0].vna_side, rx.elements[0].dut_side) == ("OUT", "IN")  # reception: the output towards the VNA
    assert rx.definition.final == "antena_rx" and rx.label == "Camino RX"

    setup = library.setups["cable_N_18G"]
    setup.paths["P1"].append(PathItem(equipment="PRE-01"))
    analysis = analyze_setup(setup, library, TODAY)
    codes = {i.code for i in analysis.issues}
    assert "camino.direccional" in codes  # the path of a 2-port test has no direction
    assert any(i.code == "equipo.categoria" and i.role == "P1-2" for i in analysis.issues)  # and does not admit preamplifiers


def test_only_two_port_equipment_in_a_path(library):
    setup = library.setups["cable_N_18G"]
    setup.paths["P1"].append(PathItem(equipment="ACP-0001"))
    analysis = analyze_setup(setup, library, TODAY)
    [issue] = [i for i in analysis.issues if i.code == "camino.puertos"]
    assert issue.severity is Severity.ERROR and "tiene 4 puertos" in issue.message and issue.role == "P1-2"


def test_dut_connectors_admitted_by_each_setup(library):
    admitted = {
        setup_id: analyze_setup(library.setups[setup_id], library, TODAY).expanded.dut_connectors_text()
        for setup_id in ("cable_N_18G", "componente_292_tosm", "acoplador_N_18G", "lisn_impedancia_VNA-BF")
    }
    assert admitted == {
        "cable_N_18G": "PR1: N 50 Ω macho; PR2: N 50 Ω macho",
        "componente_292_tosm": "PR1: 2,92 mm (K) hembra; PR2: 2,92 mm (K) macho",
        "acoplador_N_18G": "PR1: N 50 Ω hembra; PR2: N 50 Ω hembra",
        "lisn_impedancia_VNA-BF": "PR1: Terminales / bornas (no coaxial)",
    }
    assert library.references_to("ADP-NN-02") == ["acoplador_N_18G"]  # equipment of the paths are references too
