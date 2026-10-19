"""Command sequence of the R&S ZNx adapter (recorded, no instrument needed)."""

import numpy as np
import pytest

from calelektra.core.connectors import Connector
from calelektra.core.frequency import FrequencyRange
from calelektra.instruments.base import InstrumentError
from calelektra.instruments.rs_znx import RecordingTransport, RsZnxVna

POINTS = 5


def make(responses=None) -> tuple[RsZnxVna, RecordingTransport]:
    answers = {
        "CALC1:DATA:STIM?": lambda _c: ",".join(str(x) for x in np.linspace(1e6, 2e6, POINTS)),
        "CALC1:DATA? SDAT": lambda _c: ",".join(["0.5,-0.25"] * POINTS),
    }
    answers.update(responses or {})
    transport = RecordingTransport(answers)
    vna = RsZnxVna(
        "ZNB43",
        None,
        FrequencyRange(100e3, 43.5e9),
        power_range=(-40, 0),
        if_bandwidth_range=(1, 10e6),
        max_points=100001,
        transport=transport,
    )
    vna.connect()
    return vna, transport


def sent(transport: RecordingTransport) -> list[str]:
    return [c for c in transport.commands if c != "SYST:ERR?"]


def test_preset_and_sweep_settings():
    vna, transport = make()
    vna.preset()
    vna.configure_sweep(1e6, 2e6, POINTS, "logaritmico")
    vna.set_if_bandwidth(1e3)
    vna.set_power(-10)
    vna.set_averaging(4)
    commands = sent(transport)
    assert commands[:3] == ["*IDN?", "*CLS", ":FORM ASC"]
    for expected in (
        "*RST;*OPC?",
        "INIT1:CONT OFF",
        "CALC1:PAR:SDEF 'CE_S21','S21'",
        "CALC1:PAR:DEL 'Trc1'",
        "SENS1:SWE:TYPE LOG",
        "SENS1:FREQ:STAR 1000000",
        "SENS1:FREQ:STOP 2000000",
        f"SENS1:SWE:POIN {POINTS}",
        "SENS1:BAND 1000",
        "SOUR1:POW -10",
        "SENS1:AVER:COUN 4",
        "SENS1:SWE:COUN 4",
    ):
        assert expected in commands


def test_uosm_calibration_sequence_and_data():
    vna, transport = make()
    vna.preset()
    vna.configure_sweep(1e6, 2e6, POINTS)
    connectors = {1: Connector.parse("2.92 mm macho"), 2: Connector.parse("2.92 mm macho")}
    vna.begin_calibration("UOSM", (1, 2), connectors, "ZV-Z229")
    for port in (1, 2):
        for standard in ("OPEN", "SHORT", "MATCH"):
            vna.acquire_standard(standard, (port,))
    vna.acquire_standard("UNKNOWN_THROUGH", (1, 2))
    vna.apply_calibration()
    commands = sent(transport)
    for expected in (
        "SENS1:CORR:COLL:CONN1 PC292MALE",
        "SENS:CORR:CKIT:PC292:SEL 'ZV-Z229';*OPC?",
        "SENS1:CORR:COLL:METH:DEF 'CALELEKTRA',UOSM,1,2",
        "SENS1:CORR:COLL:SEL OPEN,1;*OPC?",
        "SENS1:CORR:COLL:SEL MATC,2;*OPC?",
        "SENS1:CORR:COLL:SEL UTHR,1,2,OFF,AUTO;*OPC?",
        "SENS1:CORR:COLL:SAVE:SEL;*OPC?",
    ):
        assert expected in commands
    assert vna.calibration_status() == "CAL"
    vna.single_sweep()
    s = vna.read_s_matrix((1, 2))
    assert s.shape == (POINTS, 2, 2) and np.allclose(s, 0.5 - 0.25j)
    assert np.allclose(vna.frequencies(), np.linspace(1e6, 2e6, POINTS))


def test_instrument_errors_are_raised():
    vna, transport = make()
    errors = iter(['-222,"Data out of range"', '0,"No error"'])
    transport.responses["SYST:ERR?"] = lambda _c: next(errors, '0,"No error"')
    with pytest.raises(InstrumentError, match="Data out of range"):
        vna.set_power(-5)
    assert vna.log[-1].error


def test_status_labels():
    for answer, status in (("'CAI'", "CAI"), ("'CAL OFF'", "OFF"), ("''", "NONE")):
        vna, _ = make({"SENS1:CORR:SST?": answer})
        assert vna.calibration_status() == status


def test_limits_are_checked_before_sending():
    vna, transport = make()
    before = len(transport.commands)
    with pytest.raises(InstrumentError):
        vna.configure_sweep(1e3, 1e6, 11)
    with pytest.raises(InstrumentError, match="un solo puerto"):
        vna.begin_calibration("OSM", (1, 2))
    assert len(transport.commands) == before
