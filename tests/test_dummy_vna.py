import numpy as np
import pytest

from calelektra.core.frequency import FrequencyRange
from calelektra.instruments.base import InstrumentError
from calelektra.instruments.dummy_vna import DummyVna
from calelektra.instruments.factory import open_vna


def make_vna(**kw):
    vna = DummyVna(seed=1, **kw)
    vna.connect()
    return vna


def test_requires_connection():
    with pytest.raises(InstrumentError):
        DummyVna().identify()


def test_rejects_out_of_range_sweep_and_logs_it():
    vna = make_vna(frequency_range=FrequencyRange(100e3, 43.5e9))
    with pytest.raises(InstrumentError, match="fuera de las especificaciones"):
        vna.configure_sweep(9e3, 30e6, 201)
    assert vna.log[-1].operation == "configurar_barrido" and vna.log[-1].error
    with pytest.raises(InstrumentError):
        vna.set_if_bandwidth(100e6)
    with pytest.raises(InstrumentError):
        vna.set_power(20)


def test_through_sweep():
    vna = make_vna()
    vna.configure_sweep(1e6, 1e9, 101)
    vna.set_if_bandwidth(10)
    vna.single_sweep()
    f = vna.frequencies()
    s21 = vna.read_s_parameter("S21")
    assert len(f) == 101 and f[0] == 1e6
    assert np.allclose(np.abs(s21), 1, atol=1e-3)
    with pytest.raises(InstrumentError):
        vna.read_s_parameter("S31")


def test_sweep_of_touchstone_network(library):
    data = library.touchstone_of(library.equipment["CAB-PT-N-01"])
    vna = make_vna()
    vna.connect_device(data, "CAB-PT-N-01")
    vna.configure_sweep(1e9, 18e9, 50, "logaritmico")
    vna.set_if_bandwidth(10)
    vna.single_sweep()
    measured = 20 * np.log10(np.abs(vna.read_s_parameter("S21")))
    expected = 20 * np.log10(np.abs(np.interp(vna.frequencies(), data.frequencies, np.abs(data.s_param("S21")))))
    np.testing.assert_allclose(measured, expected, atol=0.05)


def test_factory_uses_dummy_for_simulated_equipment(library):
    vna = open_vna(library.equipment["VNA-01"], library)
    assert vna.simulated and vna.frequency_range == FrequencyRange(100e3, 43.5e9) and vna.port_count == 2
    library.equipment["VNA-01"].connection.simulated = False
    with pytest.raises(InstrumentError, match="todavía no está integrado"):
        open_vna(library.equipment["VNA-01"], library)
    with pytest.raises(InstrumentError):
        open_vna(library.equipment["CAB-PT-N-01"], library)
