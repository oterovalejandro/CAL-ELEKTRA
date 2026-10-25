import numpy as np
import pytest

from calelektra.core.connectors import Connector
from calelektra.core.frequency import FrequencyRange
from calelektra.instruments.base import InstrumentError, calibration_plan
from calelektra.instruments.dummy_vna import DummyVna
from calelektra.instruments.factory import open_vna
from calelektra.instruments.rs_znx import RsZnxVna
from calelektra.instruments.simulation import cable_model, lisn_model, touchstone_model, unknown_through_model


def make_vna(**kw):
    vna = DummyVna(seed=1, **kw)
    vna.connect()
    return vna


def db(values):
    return 20 * np.log10(np.abs(values))


def calibrate(vna: DummyVna, method: str, ports=(1, 2)) -> None:
    vna.begin_calibration(method, ports, {p: Connector.parse("2.92 mm macho") for p in ports}, "kit")
    for standard, standard_ports in calibration_plan(method, ports):
        if standard == "THROUGH":
            vna.connect_through(standard_ports)
        elif standard == "UNKNOWN_THROUGH":
            vna.connect_through(standard_ports, model=unknown_through_model())
        else:
            vna.connect_standard(standard, standard_ports[0])
        vna.acquire_standard(standard, standard_ports)
    vna.apply_calibration()


def test_requires_connection():
    with pytest.raises(InstrumentError):
        DummyVna().identify()


def test_rejects_settings_outside_the_fields_and_logs_it():
    vna = make_vna(frequency_range=FrequencyRange(100e3, 43.5e9), max_points=1001)
    with pytest.raises(InstrumentError, match="fuera de las especificaciones"):
        vna.configure_sweep(9e3, 30e6, 201)
    assert vna.log[-1].operation == "configurar_barrido" and vna.log[-1].error
    with pytest.raises(InstrumentError, match="puntos"):
        vna.configure_sweep(1e6, 1e9, 5000)
    with pytest.raises(InstrumentError):
        vna.set_if_bandwidth(100e6)
    with pytest.raises(InstrumentError):
        vna.set_power(20)
    with pytest.raises(InstrumentError):
        vna.set_averaging(0)


def test_raw_measurement_is_wrong_until_calibrated():
    vna = make_vna()
    vna.configure_sweep(1e6, 18e9, 201)
    dut = cable_model(1.0, 0.6, seed=2)
    vna.connect_network(dut, (1, 2), "cable")
    vna.single_sweep()
    truth = dut(vna.frequencies())
    raw = vna.read_s_matrix((1, 2))
    assert np.max(np.abs(db(raw[:, 1, 0]) - db(truth[:, 1, 0]))) > 1.0
    assert vna.calibration_status() == "NONE"


@pytest.mark.parametrize("method", ["TOSM", "UOSM"])
def test_two_port_calibration_corrects_the_dut(method):
    vna = make_vna()
    vna.configure_sweep(1e6, 18e9, 201)
    vna.set_if_bandwidth(100)
    calibrate(vna, method)
    assert vna.calibration_status() == "CAL"
    dut = cable_model(1.0, 0.6, seed=2)
    vna.connect_network(dut, (1, 2), "cable")
    vna.single_sweep()
    measured = vna.read_s_matrix((1, 2))
    truth = dut(vna.frequencies())
    assert np.max(np.abs(measured - truth)) < 1e-3
    assert np.max(np.abs(db(measured[:, 1, 0]) - db(truth[:, 1, 0]))) < 0.01


def test_reversed_dut_and_interpolated_calibration():
    vna = make_vna()
    vna.configure_sweep(1e6, 18e9, 401)
    calibrate(vna, "TOSM")
    dut = cable_model(0.5, 0.6, seed=4)
    vna.connect_network(dut, (2, 1), "cable invertido")
    vna.configure_sweep(10e6, 10e9, 333)
    assert vna.calibration_status() == "CAI"
    vna.single_sweep()
    measured = vna.read_s_matrix((1, 2))
    truth = dut(vna.frequencies())
    np.testing.assert_allclose(db(measured[:, 0, 1]), db(truth[:, 1, 0]), atol=0.05)  # DUT port 1 on VNA port 2
    vna.configure_sweep(1e6, 20e9, 101)
    assert vna.calibration_status() == "OFF"  # outside the calibrated range


def test_one_port_calibration_of_a_lisn():
    vna = make_vna(frequency_range=FrequencyRange(5, 3e9))
    vna.configure_sweep(9e3, 30e6, 201, "logaritmico")
    calibrate(vna, "OSM", (1,))
    model = lisn_model("50Ω‖(50µH+5Ω)")
    vna.connect_one_port(model, 1, "LISN")
    vna.single_sweep()
    assert np.max(np.abs(vna.read_s_parameter("S11") - model(vna.frequencies()))) < 1e-3


def test_calibration_errors():
    vna = make_vna(calibration_methods=("OSM", "UOSM"))
    vna.configure_sweep(1e6, 1e9, 101)
    with pytest.raises(InstrumentError, match="TOSM"):
        vna.begin_calibration("TOSM", (1, 2))
    with pytest.raises(InstrumentError):
        vna.acquire_standard("OPEN", (1,))  # nothing started
    vna.begin_calibration("UOSM", (1, 2))
    vna.connect_standard("OPEN", 1)
    vna.acquire_standard("OPEN", (1,))
    with pytest.raises(InstrumentError, match="faltan estándares"):
        vna.apply_calibration()
    with pytest.raises(InstrumentError, match="no forma parte"):
        vna.acquire_standard("THROUGH", (1, 2))
    vna.configure_sweep(1e6, 2e9, 101)
    with pytest.raises(InstrumentError, match="barrido ha cambiado"):
        vna.acquire_standard("SHORT", (1,))


def test_correction_switch_and_calibration_pool():
    vna = make_vna()
    vna.configure_sweep(1e6, 1e9, 51)
    calibrate(vna, "TOSM")
    vna.set_correction(False)
    assert vna.calibration_status() == "OFF"
    vna.save_calibration("prueba")
    other = make_vna()
    other.configure_sweep(1e6, 1e9, 51)
    other.load_calibration("prueba")
    assert other.calibration_status() == "CAL"


def test_touchstone_network_on_the_bench(library):
    data = library.touchstone_of(library.equipment["CAB-PT-N-01"])
    vna = make_vna()
    vna.configure_sweep(1e9, 18e9, 50, "logaritmico")
    calibrate(vna, "TOSM")
    vna.connect_network(touchstone_model(data), (1, 2), "CAB-PT-N-01")
    vna.single_sweep()
    measured = db(vna.read_s_parameter("S21"))
    expected = db(np.interp(vna.frequencies(), data.frequencies, np.abs(data.s_param("S21"))))
    np.testing.assert_allclose(measured, expected, atol=0.05)


def test_factory_builds_the_vna_from_its_fields(library):
    vna = open_vna(library.equipment["VNA-01"], library)
    assert isinstance(vna, DummyVna)
    assert vna.frequency_range == FrequencyRange(100e3, 43.5e9) and vna.port_count == 2
    assert vna.power_range == (-40, 0) and vna.if_bandwidth_range == (1, 10e6) and vna.max_points == 100001
    assert set(vna.calibration_methods) == {"OSM", "TOSM", "UOSM", "normalizacion"}
    low = open_vna(library.equipment["VNA-BF-01"], library)
    assert low.max_points == 1601 and low.frequency_range == FrequencyRange(5, 3e9)

    library.equipment["VNA-01"].connection.simulated = False
    real = open_vna(library.equipment["VNA-01"], library)
    assert isinstance(real, RsZnxVna) and real.resource.startswith("TCPIP::")
    library.equipment["VNA-01"].connection.resource = None
    with pytest.raises(InstrumentError, match="recurso VISA"):
        open_vna(library.equipment["VNA-01"], library)
    with pytest.raises(InstrumentError):
        open_vna(library.equipment["CAB-PT-N-01"], library)
