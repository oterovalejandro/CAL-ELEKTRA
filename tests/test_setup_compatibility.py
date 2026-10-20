"""Field-based requirements, connector checks and calibration design of the setups."""

from calelektra.core.issues import Severity
from calelektra.setups.analysis import analyze_setup
from calelektra.setups.connections import design_calibration, effective_connections
from calelektra.setups.model import Requirement
from calelektra.setups.requirements import evaluate

from conftest import TODAY, setup_with_dut


def test_setup_without_dut_is_valid(library):
    analysis = analyze_setup(library.setups["cable_vna_292"], library, TODAY)
    assert analysis.ok and analysis.covered
    info = [i for i in analysis.issues if i.code == "rol.dut_en_ensayo"]
    assert info and info[0].severity is Severity.INFO and "se elige al crear cada ensayo" in info[0].message


def test_vna_only_cable_setup_is_valid_and_uses_uosm(library):
    analysis = analyze_setup(setup_with_dut(library, "cable_vna_292", "CAB-DUT-0002"), library, TODAY)
    assert analysis.ok and analysis.covered
    assert analysis.calibration.method == "UOSM"  # both VNA ports are male: the planes cannot be mated
    assert analysis.calibration.ports == (1, 2)
    assert analysis.calibration.kit_name == "2.92 mm Ideal Kit"
    assert {c.group for c in analysis.checks} == {"Requisito", "Conexión", "Calibración"}
    assert all(c.status == "ok" for c in analysis.checks)


def test_insertable_planes_give_tosm(library):
    analysis = analyze_setup(setup_with_dut(library, "componente_292_tosm", "CAB-DUT-0003"), library, TODAY)
    assert analysis.ok and analysis.calibration.method == "TOSM"


def test_wrong_connectors_and_kit_are_errors(library):
    kit_only = analyze_setup(library.setups["cable_vna_N_alarma"], library, TODAY)
    assert not kit_only.ok and not any(i.code == "conexion.conectores" for i in kit_only.issues)
    analysis = analyze_setup(setup_with_dut(library, "cable_vna_N_alarma", "CAB-DUT-0001"), library, TODAY)
    assert not analysis.ok
    connector_errors = [i for i in analysis.issues if i.code == "conexion.conectores" and i.severity is Severity.ERROR]
    assert len(connector_errors) == 2 and "familias incompatibles" in connector_errors[0].message
    assert any(i.code == "calibracion.vna" and "KIT-N-01" in i.message for i in analysis.issues)


def test_kit_genders_are_checked(library):
    library.models["kit_292_tosm"].values["generos_disponibles"] = ["macho"]  # male planes need female standards
    template = library.resolved_template("cable_vna")
    design = design_calibration(template, library.setups["cable_vna_292"], library)
    assert any(s is Severity.ERROR and "estándares hembra" in m for s, m in design.checks)


def test_optional_roles_are_bridged(library):
    template = library.resolved_template("componente_2p")
    connections = effective_connections(template, setup_with_dut(library, "cable_N_18G", "CAB-DUT-0001"), "medida")
    pairs = {(str(c.a), str(c.b)) for c in connections}
    assert ("cable_p1.B", "dut.A") in pairs  # adapter not assigned
    assert any(c.bridged == ("adaptador_p1",) for c in connections)


def test_requirement_failures_use_the_fields(library):
    library.equipment["MAST-02"].values["motorizado"] = False  # level «modelo»: ignored
    setup = library.setups["sac_nsa_3m"]
    setup.assignments["soporte_rx"] = "TRIP-01"
    analysis = analyze_setup(setup, library, TODAY)
    failed = [i for i in analysis.issues if i.code == "requisito"]
    assert failed and failed[0].severity is Severity.WARNING and "TRIP-01" in failed[0].message


def test_requirement_operators(library):
    equipment = {"vna": library.equipment["VNA-01"], "kit": library.equipment["KIT-292-01"]}
    labels = {"vna": "VNA", "kit": "Kit"}

    def check(**kw):
        return evaluate(Requirement(**kw), equipment, library, labels).ok

    assert check(campo="vna.num_puertos", operador=">=", valor=2)
    assert not check(campo="vna.num_puertos", operador=">", valor=2)
    assert check(campo="vna.metodos_calibracion", operador="contiene", valor=["TOSM", "UOSM"])
    assert check(campo="vna.rango_frecuencia.fin", operador=">=", valor="40 GHz")
    assert check(campo="vna.rango_frecuencia", operador="cubre", valor=["1 MHz", "40 GHz"])
    assert check(campo="kit.familia_conector", operador="==", valor="@vna.conector_puertos.familia")
    assert check(campo="vna.conector_puertos.genero", operador="en", valor=["macho"])
    assert check(campo="vna.opciones_instaladas", operador="existe") is False
    assert check(campo="vna.no_existe", operador="existe") is None
