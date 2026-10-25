from datetime import date

from calelektra.core.frequency import FrequencyRange
from calelektra.core.issues import Severity
from calelektra.setups.analysis import analyze_setup
from calelektra.setups.model import ConfiguredSetup, Segment

from conftest import TODAY, setup_with_dut


def codes(analysis, severity=None):
    return {i.code for i in analysis.issues if severity is None or i.severity is severity}


def test_cable_alarm_names_limiting_equipment(library):
    analysis = analyze_setup(setup_with_dut(library, "cable_N_26G_alarma", "CAB-DUT-0001"), library, TODAY)
    assert not analysis.ok and not analysis.covered
    coverage = analysis.segments[0].coverage
    assert coverage.effective == FrequencyRange(100e3, 18e9)
    limiting = {i.equipment_id for i in analysis.issues if i.code == "rango.limite_superior"}
    assert limiting == {"CAB-PT-N-01", "CAB-PT-N-02", "KIT-N-01"}
    assert analysis.role_severity["P1-1"] is Severity.ERROR  # the cable of path P1
    assert "rango.dut" in codes(analysis, Severity.WARNING)


def test_cable_ok_but_kit_calibration_due_soon(library):
    analysis = analyze_setup(library.setups["cable_N_18G"], library, TODAY)
    assert analysis.ok and analysis.covered
    assert "calibracion.proxima" in codes(analysis, Severity.WARNING)


def test_expired_calibration_blocks(library):
    analysis = analyze_setup(library.setups["cable_N_18G"], library, date(2026, 12, 1))
    assert "calibracion.caducada" in codes(analysis, Severity.ERROR)


def test_lisn_lower_limit_and_low_frequency_vna(library):
    setup = library.setups["lisn_impedancia_VNA-01"]
    analysis = analyze_setup(setup, library, TODAY)
    lower = [i for i in analysis.issues if i.code == "rango.limite_inferior"]
    assert [i.equipment_id for i in lower] == ["VNA-01"]
    setup.assignments["vna"] = "VNA-BF-01"
    assert analyze_setup(setup, library, TODAY).covered


def test_nsa_segments_and_gaps(library):
    setup = library.setups["sac_nsa_3m"]
    analysis = analyze_setup(setup, library, TODAY)
    assert analysis.ok and analysis.covered
    assert [s.segment.name for s in analysis.segments] == ["Bicónicas", "Log-periódicas"]

    setup.segments[1].range = FrequencyRange(300e6, 1e9)
    gaps = [i for i in analyze_setup(setup, library, TODAY).issues if i.code == "segmento.hueco"]
    assert gaps and "200 MHz – 300 MHz" in gaps[0].message


def test_per_segment_role_missing_in_segment(library):
    setup = library.setups["sac_nsa_3m"]
    del setup.segments[1].assignments["antena_rx"]
    analysis = analyze_setup(setup, library, TODAY)
    missing = [i for i in analysis.issues if i.code == "rol.sin_asignar"]
    assert missing and missing[0].role == "antena_rx" and missing[0].segment == "Log-periódicas"


def test_role_checks(library):
    setup = ConfiguredSetup(
        id="x",
        name="x",
        template="acoplador_4p",
        required_range=FrequencyRange(2e9, 18e9),
        paths={"P1": ["CAB-DUT-0001"], "P2": ["NO-EXISTE"], "P3": ["CAB-PT-N-02"]},  # wrong category, missing, no such path
        assignments={
            "vna": "VNA-01",
            "kit_cal": ["KIT-N-01", "KIT-292-01"],  # only one allowed
            "dut": "NO-EXISTE-2",
            "rol_raro": "VNA-01",
        },  # carga_1 and carga_2 not assigned
    )
    analysis = analyze_setup(setup, library, TODAY)
    found = codes(analysis)
    expected = {"equipo.categoria", "rol.multiple", "equipo.inexistente", "rol.sin_asignar", "rol.desconocido", "camino.desconocido"}
    assert expected <= found
    wrong = next(i for i in analysis.issues if i.code == "equipo.categoria")
    assert wrong.role == "P1-1" and "Camino P1 · 1" in wrong.message


def test_cable_without_s_parameters_is_an_error(library):
    cable = library.equipment["CAB-PT-N-01"]
    cable.s_parameters = None
    analysis = analyze_setup(library.setups["cable_N_18G"], library, TODAY)
    assert "datos_s.faltan" in codes(analysis, Severity.ERROR)


def test_manual_tripod_is_reported(library):
    setup = library.setups["sac_svswr_3m"]
    setup.assignments["soporte_tx"] = "TRIP-01"
    analysis = analyze_setup(setup, library, TODAY)
    assert "posicionador.manual" in codes(analysis, Severity.INFO)
    assert analysis.ok


def test_unknown_template():
    from calelektra.catalog.library import Library

    setup = ConfiguredSetup(id="x", name="x", template="nada", required_range=FrequencyRange(1, 2))
    analysis = analyze_setup(setup, Library("."), TODAY)
    assert "setup.plantilla" in codes(analysis)


def test_segment_outside_required_range(library):
    setup = library.setups["sac_svswr_3m"]
    setup.segments = [Segment(name="todo", range=FrequencyRange(500e6, 18e9))]
    analysis = analyze_setup(setup, library, TODAY)
    assert "segmento.fuera" in codes(analysis, Severity.WARNING)
