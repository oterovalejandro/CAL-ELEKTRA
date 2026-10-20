"""VNA debugger (set / read back / error queue) against the simulated ZNB, console and transports."""

from __future__ import annotations

import socket
import threading
from pathlib import Path

import numpy as np
import pytest

from calelektra.core.frequency import FrequencyRange
from calelektra.core.touchstone import read_touchstone
from calelektra.instruments.rs_znx import RsZnxVna
from calelektra.instruments.znx_debug import SETTINGS, ScpiError, SimulatedZnb, ZnxDebugger
from calelektra.instruments.znx_debug.checks import ADJUSTED, FAILED, OK
from calelektra.instruments.znx_debug.console import DebugConsole, main
from calelektra.setups.frequencies import read_frequency_list

EXAMPLE_LIST = Path(__file__).resolve().parents[1] / "biblioteca_ejemplo" / "hardware_setups" / "listas" / "puntos_cable_N.csv"


@pytest.fixture
def vna() -> ZnxDebugger:
    return ZnxDebugger.simulated()


def test_every_setting_is_written_and_read_back(vna):
    vna.setup_s_parameters()
    for setting in SETTINGS.values():
        if not setting.writable or not setting.example:
            continue
        trace = "CE_S21" if setting.per_trace else None
        if setting.key in ("step", "center", "span"):
            continue  # depend on the other frequencies, checked below
        read = vna.set(setting.key, setting.example, trace)
        assert setting.matches(setting.example, read), setting.key
        assert vna.get(setting.key, trace) == read


def test_frequency_settings_are_coupled_like_the_analyzer(vna):
    assert vna.set_start("1 GHz") == 1e9
    assert vna.set_stop("2GHz") == 2e9
    assert vna.set_step("10 MHz") == 10e6
    assert vna.get_points() == 101
    vna.set_center("5 GHz")
    assert vna.set_span("2 GHz") == 2e9
    assert (vna.get_start(), vna.get_stop()) == (4e9, 6e9)
    assert vna.set_sweep_type("logaritmico") == "LOG"
    with pytest.raises(ScpiError) as error:
        vna.get_step()
    assert error.value.errors[0].startswith("-221")


def test_if_bandwidth_rounded_up_and_errors_reported(vna):
    assert vna.set_if_bandwidth("1,2 kHz") == 1.5e3
    with pytest.raises(ScpiError) as error:
        vna.set_power(20)
    assert error.value.errors[0].startswith("-222")
    assert vna.get_power() == -10
    with pytest.raises(ScpiError, match="-113"):
        vna.scpi("SENS1:FOO 1")
    with pytest.raises(ValueError):
        vna.set_trace_format("Trc1", "XYZ")  # rejected before sending anything
    assert vna.errors() == []


def test_traces_formats_and_parameters(vna):
    rows = vna.setup_s_parameters("PHAS", delete_others=True)
    assert rows == [(f"CE_S{p}", f"S{p}", "PHAS") for p in ("11", "21", "12", "22")]
    assert vna.set_trace_format("*", "MLOG") == {name: "MLOG" for name, *_ in rows}
    assert vna.set_trace_parameter("CE_S11", "S22") == "S22"
    assert vna.select_trace("CE_S21") == "CE_S21"
    assert vna.get_trace_format("CE_S21") == "MLOG"
    vna.create_trace("Extra", "S12", window=5)
    assert ("Extra", "S12") in vna.list_traces()
    assert ("Extra", "S12") not in vna.delete_trace("Extra")
    with pytest.raises(ScpiError, match="-224"):
        vna.create_trace("CE_S21", "S21")  # name already used


def test_smoothing_and_max_hold_only_change_the_formatted_trace(vna):
    vna.setup_s_parameters()
    vna.set_continuous("off")
    vna.single_sweep()
    sdat, fdat = vna.read_trace("CE_S11"), vna.read_trace("CE_S11", "FDAT")
    vna.set_smoothing_aperture("CE_S11", 5)
    assert vna.set_smoothing("CE_S11", "on") is True
    assert np.array_equal(vna.read_trace("CE_S11"), sdat)
    assert not np.allclose(vna.read_trace("CE_S11", "FDAT"), fdat)
    vna.set_smoothing("CE_S11", "off")

    assert vna.set_max_hold("CE_S11", "on") == "MAX"
    for _ in range(3):
        vna.single_sweep()
    held = vna.read_trace("CE_S11", "FDAT")
    last = 20 * np.log10(np.abs(vna.read_trace("CE_S11")))
    assert np.all(held >= last - 1e-9) and np.any(held > last + 0.01)
    assert vna.restart_hold("CE_S11") == "MAX"
    vna.single_sweep()
    assert np.allclose(vna.read_trace("CE_S11", "FDAT"), 20 * np.log10(np.abs(vna.read_trace("CE_S11"))))


def test_restart_sweep_in_single_and_continuous_mode(vna):
    vna.set_averaging("on")
    vna.set_average_count(4)
    vna.set_sweep_count(4)
    assert vna.set_continuous("off") is False
    assert "4 barridos" in vna.restart_sweep()
    assert vna.get_completed_sweeps() == 4
    vna.set_continuous("on")
    assert "continuo" in vna.restart_sweep()
    with pytest.raises(ScpiError, match="-213"):
        vna.single_sweep()  # INIT is ignored in continuous mode


def test_frequency_list_from_the_example_csv(vna):
    expected = read_frequency_list(EXAMPLE_LIST)
    segments = vna.set_frequency_list(str(EXAMPLE_LIST))
    assert sum(points for _a, _b, points in segments) == len(expected)
    assert vna.get_sweep_type() == "SEGM"
    assert np.allclose(vna.get_frequencies(), expected)
    assert vna.set_frequency_list("1GHz;1,5GHz;2GHz") == [(1e9, 2e9, 3)]


def test_ascii_and_binary_transfer_give_the_same_data(vna):
    vna.setup_s_parameters()
    vna.set_continuous("off")
    vna.single_sweep()
    ascii_data = vna.read_trace("CE_S21")
    for data_format in ("REAL,32", "REAL,64", "ascii"):
        assert vna.set_data_format(data_format) == ("ASC" if data_format == "ascii" else data_format)
        assert np.allclose(vna.read_trace("CE_S21"), ascii_data, rtol=1e-6)
        assert len(vna.get_frequencies()) == 201


@pytest.mark.parametrize("export_format, touchstone_format", [("COMP", "RI"), ("LINP", "MA"), ("DB", "DB")])
def test_touchstone_export_in_the_analyzer(vna, tmp_path, export_format, touchstone_format):
    vna.setup_s_parameters()
    vna.set_continuous("off")
    vna.single_sweep()
    path = vna.export_and_download(tmp_path / "cable.s2p", export_format)
    assert f"# HZ S {touchstone_format} R 50" in path.read_text()
    exported, measured = read_touchstone(path), vna.read_s_parameters()
    assert np.allclose(exported.s, measured.s, rtol=1e-5, atol=1e-7)
    saved = read_touchstone(vna.save_s2p(tmp_path / "software", touchstone_format))
    assert np.allclose(saved.s, measured.s, rtol=1e-5, atol=1e-7)


def test_touchstone_2_and_missing_calibration(tmp_path):
    vna = ZnxDebugger.simulated(calibrated=False)
    with pytest.raises(ScpiError, match="-200"):
        vna.export_touchstone("cable.s2p")
    vna.export_touchstone("reflexion.s1p", "COMP", "1")  # one port needs no full calibration
    vna.set_touchstone_version("F20")
    vna.export_touchstone("reflexion.s1p", "LOGP", "1")
    data = read_touchstone(vna.download_file("reflexion.s1p", tmp_path / "reflexion.s1p"))
    assert data.n_ports == 1 and len(data.frequencies) == 201
    vna.delete_file("reflexion.s1p")
    with pytest.raises(ScpiError, match="-256"):
        vna.download_file("reflexion.s1p", tmp_path / "otra.s1p")


def test_self_check_passes_and_restores_the_settings(vna):
    vna.set_start("30 MHz")
    vna.set_points(401)
    vna.set_power(-15)
    results = vna.self_check(export=True)
    assert not [r for r in results if r.status == FAILED]
    assert {r.status for r in results} >= {OK, ADJUSTED}
    assert (vna.get_start(), vna.get_points(), vna.get_power()) == (30e6, 401, -15)
    assert vna.get_continuous() is True
    assert not [name for name in vna.trace_names() if name.startswith("CE_CHK_")]


def test_simulator_accepts_long_forms_paths_and_units():
    sim = SimulatedZnb()
    sim.write("SENSe1:FREQuency:STARt 1GHz;STOP 2 GHZ")
    sim.write(":SOURce1:POWer:LEVel:IMMediate:AMPLitude -5")
    sim.write("BWIDth 1.2KHZ")
    assert sim.query("SENS1:FREQ:STAR?;STOP?") == "1000000000;2000000000"
    assert sim.query("SOUR1:POW?") == "-5"
    assert sim.query("SENS:BAND:RES?") == "1500"
    sim.write("SYST:ERR?")  # a query sent as a write: its answer is discarded by the next command
    assert sim.query("*OPC?") == "1"
    assert sim.query("SYST:ERR?").startswith("-410")


def test_zx_adapter_runs_against_the_simulated_znb():
    sim = SimulatedZnb()
    vna = RsZnxVna("ZNB43", None, FrequencyRange(100e3, 43.5e9), power_range=(-40, 0), if_bandwidth_range=(1, 10e6),
                   max_points=100001, transport=sim)
    vna.connect()
    vna.preset()
    vna.configure_sweep(1e9, 2e9, 11, "lineal")
    vna.set_averaging(4)
    vna.single_sweep()
    s21 = vna.read_s_parameter("S21")
    assert len(vna.frequencies()) == 11 and np.all(np.abs(20 * np.log10(np.abs(s21)) + 0.55) < 0.2)
    vna.configure_frequency_list([1e9, 1.1e9, 1.2e9, 2e9, 3e9])
    vna.single_sweep()
    assert np.allclose(vna.frequencies(), [1e9, 1.1e9, 1.2e9, 2e9, 3e9])
    assert not [entry for entry in vna.log if entry.error] and not sim.errors


def _serve(sim: SimulatedZnb) -> int:
    """SCPI socket server (like port 5025 of the analyzer) answering with the simulator."""
    server = socket.create_server(("127.0.0.1", 0))

    def run() -> None:
        connection, _ = server.accept()
        buffer = b""
        with connection, server:
            while chunk := connection.recv(4096):
                buffer += chunk
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    answer = sim.process(line.decode("latin-1"))
                    if answer:
                        connection.sendall(answer)

    threading.Thread(target=run, daemon=True).start()
    return server.getsockname()[1]


def test_socket_transport_with_binary_blocks(tmp_path):
    sim = SimulatedZnb()
    vna = ZnxDebugger.open_socket("127.0.0.1", _serve(sim), timeout_ms=5000)
    assert "ZNB43" in vna.identify()
    vna.setup_s_parameters()
    vna.set_data_format("REAL,32")
    vna.set_continuous("off")
    vna.single_sweep()
    assert len(vna.read_trace("CE_S21")) == 201
    assert vna.get_points() == 201  # text answer right after a binary block
    vna.export_touchstone("cable.s2p")
    assert read_touchstone(vna.download_file("cable.s2p", tmp_path / "cable.s2p")).n_ports == 2
    vna.close()


def test_console_script(tmp_path, capsys):
    script = tmp_path / "pruebas.txt"
    script.write_text(
        "# barrido del cable\nset_start 10MHz\nset_stop 18GHz\nset_points 801\nset_if_bandwidth 1kHz\n"
        "setup_s_parameters MLOG\nset_smoothing CE_S21 on\nset_continuous off\nsingle_sweep\nread_trace CE_S21 FDAT\n"
        f"save_s2p {tmp_path / 'cable'} DB\nscpi CALC1:PAR:SEL 'CE_S11'\nscpi SENS1:FREQ:STAR?\nerrors\n",
        encoding="utf-8",
    )
    assert main(["--simulado", "--script", str(script), "--log", str(tmp_path / "scpi.log")]) == 0
    out = capsys.readouterr().out
    assert "Frecuencia final: 18 GHz" in out and "801 valores" in out and "cola de errores vacía" in out
    assert "→ SENS1:FREQ:STOP 18000000000" in out
    assert "SENS1:SWE:POIN 801" in (tmp_path / "scpi.log").read_text(encoding="utf-8")
    assert read_touchstone(tmp_path / "cable.s2p").n_ports == 2


def test_console_reports_errors_and_bad_arguments(capsys):
    console = DebugConsole(ZnxDebugger.simulated(), echo="off")
    for line in ("set_power 30", "set_points", "foo", "ayuda set_hold", "set_trace_format Trc1 PHAS",
                 "setup_s_parameters separate_windows=off PHAS"):
        console.onecmd(line)
    out = capsys.readouterr().out
    assert '-222,"Data out of range' in out
    assert "Uso: set_points <valor>" in out and "«foo» no es una función" in out
    assert "SCPI: CALC1:PAR:SEL '<traza>', CALC1:PHOL <valor>" in out
    assert "Formato de la traza: PHAS" in out
    assert "CE_S22  S22        PHAS" in out
    assert console.failures == 3
    assert console.traces[-1] == "CE_S22"


def test_console_self_check_report(tmp_path, capsys):
    report = tmp_path / "informe.txt"
    assert main(["--simulado", "--comprobar", "--exportar", "--informe", str(report)]) == 0
    text = report.read_text(encoding="utf-8")
    assert "comprobaciones" in text and "FALLO" not in text and "MMEM:STOR:TRAC:PORT" in text
