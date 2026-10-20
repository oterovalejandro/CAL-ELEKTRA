"""Step-by-step control of a R&S ZNx analyzer to check each SCPI command (no GUI).

Every function sends the SCPI commands of the RsZnx 4.25.0 driver for one
setting or action, reads the value back from the analyzer and then empties the
error queue (``SYST:ERR?``); if the analyzer reports errors the function raises
:class:`ScpiError` with them. Every exchange is kept in ``history`` and passed
to ``listener`` (the console prints it), so what went over the wire can be
checked line by line::

    vna = ZnxDebugger.open_visa("TCPIP::192.168.0.50::hislip0::INSTR")  # or open_socket(ip), simulated()
    vna.set_start("10 MHz")            # → 10000000.0, the value read back
    vna.setup_s_parameters("MLOG")     # traces CE_S11, CE_S21, CE_S12, CE_S22
    vna.restart_sweep()                # after connecting the cables: averaging from scratch
"""

from __future__ import annotations

import re
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterator, Sequence

import numpy as np

from calelektra.core.touchstone import Touchstone, TouchstoneError, read_touchstone, write_touchstone
from calelektra.core.units import UnitError, format_frequency, parse_frequency
from calelektra.instruments.base import InstrumentError
from calelektra.instruments.rs_znx import PyVisaTransport, ScpiTransport, SocketTransport, frequency_segments
from calelektra.instruments.znx_debug.settings import (
    CHANNEL_SETTINGS,
    DATA_FORMATS,
    DATA_KINDS,
    EXPORT_FORMATS,
    IMPEDANCE_MODES,
    SETTINGS,
    TRACE_SETTINGS,
    parse_bool,
    parse_choice,
    parse_scpi_number,
    scpi_number,
)
from calelektra.instruments.znx_debug.simulator import COMPLEX_FORMATS, SimulatedZnb, has_query
from calelektra.setups.frequencies import read_frequency_list

TRACE_PREFIX = "CE_"  # trace names of the ZNx adapter (CE_S11, CE_S21…)
EXPORT_FILE = "calelektra_depuracion"


class ScpiError(InstrumentError):
    """The analyzer reported errors (``SYST:ERR?``) after an operation."""

    def __init__(self, operation: str, errors: Sequence[str], cause: str = ""):
        self.operation = operation
        self.errors = list(errors)
        self.cause = cause  # communication error that came first (e.g. a query without answer)
        prefix = f"{operation}: {cause}; " if cause else f"{operation}: "
        super().__init__(prefix + "el VNA ha devuelto " + "; ".join(self.errors))


@dataclass
class ScpiExchange:
    time: datetime
    command: str
    answer: str | None = None  # None: command without answer
    seconds: float = 0.0
    error: str | None = None

    @property
    def empty_error_queue(self) -> bool:
        return self.command == "SYST:ERR?" and self.answer is not None and _no_error(self.answer)

    def line(self, timestamp: bool = True) -> str:
        text = (f"{self.time:%H:%M:%S.%f}"[:-3] + "  " if timestamp else "") + f"→ {self.command}"
        if self.error:
            return f"{text}   ✗ {self.error}"
        if self.answer is not None:
            answer = self.answer if len(self.answer) <= 100 else self.answer[:99] + "…"
            text += f"   ← {answer}"
        return f"{text}   ({self.seconds * 1000:.0f} ms)"

    def __str__(self) -> str:
        return self.line()


def _logged(answer: str, limit: int = 2000) -> str:
    """Answer as kept in the history (long data answers are cut)."""
    return answer if len(answer) <= limit else f"{answer[:limit]}… ({len(answer)} caracteres)"


def _no_error(answer: str) -> bool:
    return re.match(r"\s*\+?0\s*,", answer) is not None


def _unquote(text: str) -> str:
    return text.strip().strip("'\"")


def _s_parameter(text: str) -> str:
    match = re.fullmatch(r"[Ss](\d)(\d)", str(text).strip())
    if not match:
        raise ValueError(f"«{text}» no es un parámetro S (S11, S21, S12, S22…)")
    return f"S{match.group(1)}{match.group(2)}"


def _ports(ports: Sequence[int] | str) -> tuple[int, ...]:
    items = ports.split(",") if isinstance(ports, str) else ports
    try:
        return tuple(int(p) for p in items)
    except ValueError:
        raise ValueError(f"«{ports}» no es una lista de puertos (por ejemplo 1,2)") from None


class ZnxDebugger:
    """SCPI functions of one channel of a ZNx analyzer, each one checked against the error queue."""

    def __init__(
        self,
        transport: ScpiTransport,
        channel: int = 1,
        check_errors: bool = True,
        listener: Callable[[ScpiExchange], None] | None = None,
        timeout_ms: int = 10_000,
    ):
        self.transport = transport
        self.channel = channel
        self.check_errors = check_errors
        self.listener = listener
        self.timeout_ms = timeout_ms
        self.history: list[ScpiExchange] = []
        self._data_format: str | None = None
        self._byte_order = "SWAP"

    @classmethod
    def simulated(cls, ports: int = 2, calibrated: bool = True, seed: int | None = 1, **options: Any) -> ZnxDebugger:
        return cls(SimulatedZnb(ports=ports, calibrated=calibrated, seed=seed), **options)

    @classmethod
    def open_visa(cls, resource: str, timeout_ms: int = 10_000, **options: Any) -> ZnxDebugger:
        return cls(PyVisaTransport(resource, timeout_ms), timeout_ms=timeout_ms, **options)

    @classmethod
    def open_socket(cls, host: str, port: int = 5025, timeout_ms: int = 10_000, **options: Any) -> ZnxDebugger:
        return cls(SocketTransport(host, port, timeout_ms), timeout_ms=timeout_ms, **options)

    @property
    def is_simulated(self) -> bool:
        return isinstance(self.transport, SimulatedZnb)

    def close(self) -> None:
        self.transport.close()

    # ── SCPI exchanges ─────────────────────────────────────────────────────
    def _exchange(self, command: str, action: Callable[[], Any], describe: Callable[[Any], str] | None) -> Any:
        started, clock = datetime.now(), time.perf_counter()
        try:
            result = action()
        except Exception as exc:  # VISA and socket errors, timeouts
            self._log(ScpiExchange(started, command, seconds=time.perf_counter() - clock, error=str(exc)))
            if isinstance(exc, InstrumentError):
                raise
            raise InstrumentError(f"error de comunicación en «{command}»: {exc}") from exc
        self._log(ScpiExchange(started, command, describe(result) if describe else None, time.perf_counter() - clock))
        return result

    def _log(self, exchange: ScpiExchange) -> None:
        self.history.append(exchange)
        if self.listener:
            self.listener(exchange)

    def write(self, command: str) -> None:
        self._exchange(command, lambda: self.transport.write(command), None)

    def query(self, command: str, timeout_ms: int | None = None) -> str:
        return self._exchange(command, lambda: self.transport.query(command, timeout_ms or self.timeout_ms), _logged)

    def query_block(self, command: str, timeout_ms: int | None = None) -> bytes:
        return self._exchange(
            command,
            lambda: self.transport.query_block(command, timeout_ms or self.timeout_ms),
            lambda payload: f"<bloque binario de {len(payload)} bytes>",
        )

    def query_values(self, command: str, timeout_ms: int | None = None) -> np.ndarray:
        """Numbers of a data query, in the transfer format set in the analyzer (``FORM``)."""
        data_format = self._data_format or self.get_data_format()
        if data_format == "ASC":
            text = self.query(command, timeout_ms)
            try:
                return np.array([float(item) for item in text.split(",") if item.strip()])
            except ValueError:
                raise InstrumentError(f"«{command}»: la respuesta no son números") from None
        dtype = np.dtype(("<" if self._byte_order == "SWAP" else ">") + ("f4" if data_format == "REAL,32" else "f8"))
        payload = self.query_block(command, timeout_ms)
        if len(payload) % dtype.itemsize:
            raise InstrumentError(f"«{command}»: {len(payload)} bytes no es múltiplo de {dtype.itemsize} ({data_format})")
        return np.frombuffer(payload, dtype=dtype).astype(float)

    def errors(self) -> list[str]:
        """Empty the error queue (``SYST:ERR?`` until ``0,"No error"``)."""
        found = []
        for _ in range(100):
            answer = self.query("SYST:ERR?")
            if _no_error(answer):
                break
            found.append(answer)
        return found

    @contextmanager
    def _operation(self, name: str) -> Iterator[None]:
        try:
            yield
        except InstrumentError as exc:
            if self.check_errors and not isinstance(exc, ScpiError):
                try:
                    errors = self.errors()
                except InstrumentError:
                    errors = []
                if errors:
                    raise ScpiError(name, errors, cause=str(exc)) from exc
            raise
        if self.check_errors:
            errors = self.errors()
            if errors:
                raise ScpiError(name, errors)

    def _select(self, trace: str | None) -> None:
        if trace:
            self.write(f"CALC{self.channel}:PAR:SEL '{trace}'")

    # ── session ────────────────────────────────────────────────────────────
    def identify(self) -> str:
        with self._operation("identify"):
            return self.query("*IDN?")

    def reset(self) -> None:
        with self._operation("reset"):
            self.query("*RST;*OPC?", 60_000)
        self._data_format = None

    def clear_status(self) -> None:
        self.write("*CLS")

    def wait(self) -> bool:
        with self._operation("wait"):
            return self.query("*OPC?", 60_000).strip() == "1"

    def scpi(self, command: str) -> str | None:
        """Any command; a query (with «?») returns the answer."""
        if re.search(r"\*RST|FORM", command, re.IGNORECASE):
            self._data_format = None  # read again before the next data transfer
        with self._operation("scpi"):
            if has_query(command):
                return self.query(command)
            self.write(command)
        return None

    # ── generic settings ───────────────────────────────────────────────────
    def get(self, key: str, trace: str | None = None) -> Any:
        """Value of a setting of :data:`SETTINGS` (per-trace settings: of ``trace``, ``"*"`` for all)."""
        setting = SETTINGS[key]
        if setting.per_trace and trace == "*":
            return {name: self.get(key, name) for name in self.trace_names()}
        with self._operation(f"get_{key}"):
            if setting.per_trace:
                self._select(trace)
            return setting.decode(self.query(f"{setting.command(self.channel)}?"))

    def set(self, key: str, value: Any, trace: str | None = None) -> Any:
        """Write a setting and read it back; returns the value the analyzer kept."""
        setting = SETTINGS[key]
        if not setting.writable:
            raise InstrumentError(f"«{setting.label}» solo se puede leer")
        if setting.per_trace and trace == "*":
            return {name: self.set(key, value, name) for name in self.trace_names()}
        header = setting.command(self.channel)
        text = setting.encode(value)
        with self._operation(f"set_{key}"):
            if setting.per_trace:
                self._select(trace)
            self.write(f"{header} {text}")
            return setting.decode(self.query(f"{header}?"))

    # ── frequency and sweep ────────────────────────────────────────────────
    def set_sweep_type(self, sweep_type: str) -> str:
        """LIN, LOG or SEGM (frequency list) — ``SENS<ch>:SWE:TYPE``."""
        return self.set("sweep_type", sweep_type)

    def get_sweep_type(self) -> str:
        return self.get("sweep_type")

    def set_start(self, frequency: str | float) -> float:
        return self.set("start", frequency)

    def get_start(self) -> float:
        return self.get("start")

    def set_stop(self, frequency: str | float) -> float:
        return self.set("stop", frequency)

    def get_stop(self) -> float:
        return self.get("stop")

    def set_center(self, frequency: str | float) -> float:
        return self.set("center", frequency)

    def get_center(self) -> float:
        return self.get("center")

    def set_span(self, frequency: str | float) -> float:
        return self.set("span", frequency)

    def get_span(self) -> float:
        return self.get("span")

    def set_points(self, points: int | str) -> int:
        return self.set("points", points)

    def get_points(self) -> int:
        return self.get("points")

    def set_step(self, frequency: str | float) -> float:
        """Step of a linear sweep; the analyzer recomputes the number of points (and the stop frequency)."""
        return self.set("step", frequency)

    def get_step(self) -> float:
        return self.get("step")

    def set_frequency_list(self, frequencies: str | Path | Sequence[float]) -> list[tuple[float, float, int]]:
        """Segmented sweep with the frequencies of a CSV list (or a sequence), like the ZNx adapter.

        Each run of equal step becomes a segment (``SENS<ch>:SEGM<n>:INS``) with the
        channel power and IFBW; returns the segments read back.
        """
        if isinstance(frequencies, Path) or (isinstance(frequencies, str) and Path(frequencies).is_file()):
            values = read_frequency_list(Path(frequencies))
        elif isinstance(frequencies, str):
            separator = r"[;\s]+" if re.search(r"[;\s]", frequencies.strip()) else ","  # «1,5GHz;2GHz» or «1GHz,2GHz»
            try:
                values = np.array([parse_frequency(item) for item in re.split(separator, frequencies.strip()) if item])
            except UnitError as exc:
                raise ValueError(f"«{frequencies}» no es un fichero ni una lista de frecuencias: {exc}") from None
        else:
            values = np.asarray(frequencies, dtype=float)
        if len(values) == 0:
            raise ValueError("la lista de frecuencias está vacía")
        ch = self.channel
        with self._operation("set_frequency_list"):
            power = parse_scpi_number(self.query(f"SOUR{ch}:POW?"))
            bandwidth = parse_scpi_number(self.query(f"SENS{ch}:BAND?"))
            self.write(f"SENS{ch}:SEGM:DEL:ALL")
            for number, (start, stop, points) in enumerate(frequency_segments(values), start=1):
                self.query(
                    f"SENS{ch}:SEGM{number}:INS {scpi_number(start)},{scpi_number(stop)},{points},"
                    f"{scpi_number(power)},0,0,{scpi_number(bandwidth)},AUTO,NORM,AUTO,STEP;*OPC?"
                )
            self.write(f"SENS{ch}:SEGM:BWID:CONT OFF")
            self.write(f"SENS{ch}:SEGM:POW:CONT OFF")
            self.write(f"SENS{ch}:SWE:TYPE SEGM")
        return self.get_segments()

    def get_segments(self) -> list[tuple[float, float, int]]:
        ch = self.channel
        with self._operation("get_segments"):
            count = int(parse_scpi_number(self.query(f"SENS{ch}:SEGM:COUN?")))
            return [
                (
                    parse_scpi_number(self.query(f"SENS{ch}:SEGM{n}:FREQ:STAR?")),
                    parse_scpi_number(self.query(f"SENS{ch}:SEGM{n}:FREQ:STOP?")),
                    int(parse_scpi_number(self.query(f"SENS{ch}:SEGM{n}:SWE:POIN?"))),
                )
                for n in range(1, count + 1)
            ]

    def get_frequencies(self) -> np.ndarray:
        """Stimulus values of the sweep (``CALC<ch>:DATA:STIM?``)."""
        with self._operation("get_frequencies"):
            return self.query_values(f"CALC{self.channel}:DATA:STIM?")

    # ── power and IF ───────────────────────────────────────────────────────
    def set_power(self, dbm: str | float) -> float:
        return self.set("power", dbm)

    def get_power(self) -> float:
        return self.get("power")

    def set_rf_output(self, state: str | bool) -> bool:
        return self.set("rf_output", state)

    def get_rf_output(self) -> bool:
        return self.get("rf_output")

    def set_if_bandwidth(self, frequency: str | float) -> float:
        """IFBW; the analyzer rounds it to its steps (1, 1.5, 2, 3, 5, 7 × 10ⁿ Hz)."""
        return self.set("if_bandwidth", frequency)

    def get_if_bandwidth(self) -> float:
        return self.get("if_bandwidth")

    def set_if_selectivity(self, selectivity: str) -> str:
        return self.set("if_selectivity", selectivity)

    def get_if_selectivity(self) -> str:
        return self.get("if_selectivity")

    # ── averaging and sweep control ────────────────────────────────────────
    def set_averaging(self, state: str | bool) -> bool:
        return self.set("averaging", state)

    def get_averaging(self) -> bool:
        return self.get("averaging")

    def set_average_count(self, count: int | str) -> int:
        return self.set("average_count", count)

    def get_average_count(self) -> int:
        return self.get("average_count")

    def set_average_mode(self, mode: str) -> str:
        return self.set("average_mode", mode)

    def get_average_mode(self) -> str:
        return self.get("average_mode")

    def set_sweep_count(self, count: int | str) -> int:
        return self.set("sweep_count", count)

    def get_sweep_count(self) -> int:
        return self.get("sweep_count")

    def set_continuous(self, state: str | bool) -> bool:
        return self.set("continuous", state)

    def get_continuous(self) -> bool:
        return self.get("continuous")

    def get_sweep_time(self) -> float:
        return self.get("sweep_time")

    def _sweep_timeout_ms(self) -> int:
        ch = self.channel
        try:
            seconds = parse_scpi_number(self.query(f"SENS{ch}:SWE:TIME?"))
            count = parse_scpi_number(self.query(f"SENS{ch}:SWE:COUN?"))
        except (InstrumentError, ValueError):
            return 120_000
        return int(1000 * (2 * seconds * count + 10))

    def single_sweep(self, timeout_s: float | None = None) -> int:
        """``INIT<ch>;*OPC?`` in single sweep mode (it runs ``sweep_count`` sweeps); returns the sweeps done."""
        ch = self.channel
        with self._operation("single_sweep"):
            timeout = self._sweep_timeout_ms() if timeout_s is None else int(timeout_s * 1000)
            self.query(f"INIT{ch};*OPC?", timeout)
            return int(parse_scpi_number(self.query(f"CALC{ch}:DATA:NSW:COUN?")))

    def restart_sweep(self) -> str:
        """Start again after connecting the cables: the running sweep and the averaging so far are discarded.

        Continuous mode: ``ABOR`` + ``SENS<ch>:AVER:CLE``; the analyzer keeps sweeping from scratch.
        Single mode: ``SENS<ch>:AVER:CLE`` + ``INIT<ch>;*OPC?``, waiting for the whole sweep count.
        """
        ch = self.channel
        with self._operation("restart_sweep"):
            if parse_bool(self.query(f"INIT{ch}:CONT?")):
                self.write("ABOR")
                self.write(f"SENS{ch}:AVER:CLE")
                return "barrido continuo: ABOR + SENS:AVER:CLE, el promediado empieza de cero"
            self.write(f"SENS{ch}:AVER:CLE")
            self.query(f"INIT{ch};*OPC?", self._sweep_timeout_ms())
            sweeps = int(parse_scpi_number(self.query(f"CALC{ch}:DATA:NSW:COUN?")))
            return f"barrido único: SENS:AVER:CLE + INIT, {sweeps} barridos completados"

    def restart_averaging(self) -> None:
        """``SENS<ch>:AVER:CLE``: a new averaging cycle starts with the next sweep."""
        with self._operation("restart_averaging"):
            self.write(f"SENS{self.channel}:AVER:CLE")

    def abort(self) -> None:
        with self._operation("abort"):
            self.write("ABOR")

    def get_completed_sweeps(self) -> int:
        """Sweeps completed by the last INIT in single mode (``CALC<ch>:DATA:NSW:COUN?``)."""
        with self._operation("get_completed_sweeps"):
            return int(parse_scpi_number(self.query(f"CALC{self.channel}:DATA:NSW:COUN?")))

    # ── traces ─────────────────────────────────────────────────────────────
    def list_traces(self) -> list[tuple[str, str]]:
        """``(name, parameter)`` of the traces of the channel (``CALC<ch>:PAR:CAT?``)."""
        with self._operation("list_traces"):
            answer = self.query(f"CALC{self.channel}:PAR:CAT?")
        items = [item.strip() for item in _unquote(answer).split(",") if item.strip()]
        return list(zip(items[0::2], items[1::2]))

    def trace_names(self) -> list[str]:
        return [name for name, _ in self.list_traces()]

    def create_trace(self, name: str, parameter: str = "S21", window: int = 1) -> list[tuple[str, str]]:
        """New trace measuring ``parameter``, shown in diagram ``window``."""
        parameter = _s_parameter(parameter)
        ch = self.channel
        with self._operation("create_trace"):
            self.write(f"CALC{ch}:PAR:SDEF '{name}','{parameter}'")
            self.write(f"DISP:WIND{window}:STAT ON")
            self.write(f"DISP:WIND{window}:TRAC:EFE '{name}'")
        return self.list_traces()

    def delete_trace(self, name: str) -> list[tuple[str, str]]:
        with self._operation("delete_trace"):
            self.write(f"CALC{self.channel}:PAR:DEL '{name}'")
        return self.list_traces()

    def select_trace(self, name: str) -> str:
        """Active trace of the channel: format, smoothing, hold and data act on it."""
        with self._operation("select_trace"):
            self._select(name)
        return self.get_selected_trace()

    def get_selected_trace(self) -> str:
        with self._operation("get_selected_trace"):
            return _unquote(self.query(f"CALC{self.channel}:PAR:SEL?"))

    def set_trace_parameter(self, name: str, parameter: str) -> str:
        """Measured quantity of an existing trace (``CALC<ch>:PAR:MEAS``)."""
        parameter = _s_parameter(parameter)
        with self._operation("set_trace_parameter"):
            self.write(f"CALC{self.channel}:PAR:MEAS '{name}','{parameter}'")
        return self.get_trace_parameter(name)

    def get_trace_parameter(self, name: str) -> str:
        parameter = dict(self.list_traces()).get(name)
        if parameter is None:
            raise InstrumentError(f"no existe la traza «{name}» en el canal {self.channel}")
        return parameter

    def show_trace(self, name: str, window: int = 1) -> None:
        with self._operation("show_trace"):
            self.write(f"DISP:WIND{window}:STAT ON")
            self.write(f"DISP:WIND{window}:TRAC:EFE '{name}'")

    def autoscale(self, name: str) -> None:
        with self._operation("autoscale"):
            self.write(f"DISP:TRAC:Y:AUTO ONCE,'{name}'")

    def setup_s_parameters(
        self, trace_format: str = "MLOG", ports: int = 2, separate_windows: bool = True, delete_others: bool = False
    ) -> list[tuple[str, str, str]]:
        """One trace per S-parameter (``CE_S11``, ``CE_S21``, ``CE_S12``, ``CE_S22``) with the given format.

        Existing ``CE_`` traces are reused. Each trace goes to its own diagram unless
        ``separate_windows`` is false. Returns ``(trace, parameter, format)`` of the channel.
        """
        fmt = SETTINGS["trace_format"].encode(trace_format)
        ch = self.channel
        existing = dict(self.list_traces())
        wanted = [(i, j) for j in range(1, ports + 1) for i in range(1, ports + 1)]  # S11, S21, S12, S22
        names = []
        for number, (i, j) in enumerate(wanted, start=1):
            name, parameter = f"{TRACE_PREFIX}S{i}{j}", f"S{i}{j}"
            window = number if separate_windows else 1
            with self._operation("setup_s_parameters"):
                if name not in existing:
                    self.write(f"CALC{ch}:PAR:SDEF '{name}','{parameter}'")
                elif existing[name].upper() != parameter:
                    self.write(f"CALC{ch}:PAR:MEAS '{name}','{parameter}'")
                self.write(f"DISP:WIND{window}:STAT ON")
                self.write(f"DISP:WIND{window}:TRAC:EFE '{name}'")
                self.write(f"CALC{ch}:PAR:SEL '{name}'")
                self.write(f"CALC{ch}:FORM {fmt}")
            names.append(name)
        if delete_others:
            for name in existing:
                if name not in names:
                    self.delete_trace(name)
        return [(name, parameter, self.get_trace_format(name)) for name, parameter in self.list_traces()]

    # ── trace format and processing (active trace) ─────────────────────────
    def set_trace_format(self, trace: str, trace_format: str) -> str:
        """MLOG (dB), PHAS, UPH, MLIN, SWR, REAL, IMAG, GDEL, SMIT, POL, ISM — ``CALC<ch>:FORM``."""
        return self.set("trace_format", trace_format, trace)

    def get_trace_format(self, trace: str) -> str:
        return self.get("trace_format", trace)

    def set_smoothing(self, trace: str, state: str | bool) -> bool:
        """Smoothing of the trace: it changes the formatted trace (FDAT), not the unformatted data (SDAT)."""
        return self.set("smoothing", state, trace)

    def get_smoothing(self, trace: str) -> bool:
        return self.get("smoothing", trace)

    def set_smoothing_aperture(self, trace: str, percent: str | float) -> float:
        return self.set("smoothing_aperture", percent, trace)

    def get_smoothing_aperture(self, trace: str) -> float:
        return self.get("smoothing_aperture", trace)

    def set_hold(self, trace: str, mode: str) -> str:
        """OFF, MAX (max hold) or MIN (min hold) — ``CALC<ch>:PHOL``."""
        return self.set("hold", mode, trace)

    def get_hold(self, trace: str) -> str:
        return self.get("hold", trace)

    def set_max_hold(self, trace: str, state: str | bool) -> str:
        return self.set("hold", "MAX" if parse_bool(state) else "OFF", trace)

    def restart_hold(self, trace: str) -> str:
        """Restart the max/min hold of the trace (``CALC<ch>:PHOL OFF`` and back to its mode)."""
        setting = SETTINGS["hold"]
        ch = self.channel
        with self._operation("restart_hold"):
            self._select(trace)
            mode = setting.decode(self.query(f"CALC{ch}:PHOL?"))
            if mode != "OFF":
                self.write(f"CALC{ch}:PHOL OFF")
                self.write(f"CALC{ch}:PHOL {mode}")
        return mode

    # ── instrument ─────────────────────────────────────────────────────────
    def set_display_update(self, state: str | bool) -> bool:
        """Keep the analyzer screen updated while it is in remote control (``SYST:DISP:UPD``)."""
        return self.set("display_update", state)

    def get_display_update(self) -> bool:
        return self.get("display_update")

    def set_data_format(self, data_format: str) -> str:
        """Transfer format of the data: ASC, REAL,32 or REAL,64 (binary with ``FORM:BORD SWAP``)."""
        fmt = parse_choice(data_format, DATA_FORMATS)
        with self._operation("set_data_format"):
            if fmt != "ASC":
                self.write("FORM:BORD SWAP")
            self.write(f"FORM {fmt}")
        self._data_format = None
        return self.get_data_format()

    def get_data_format(self) -> str:
        with self._operation("get_data_format"):
            answer = self.query("FORM?")
            order = self.query("FORM:BORD?")
        text = _unquote(answer).upper().replace(" ", "")
        self._data_format = "ASC" if text.startswith("ASC") else text
        self._byte_order = "NORM" if order.strip().upper().startswith("NORM") else "SWAP"
        return self._data_format

    # ── data ───────────────────────────────────────────────────────────────
    def read_trace(self, trace: str, kind: str = "SDAT") -> np.ndarray:
        """Data of a trace: SDAT (complex, unformatted), FDAT (formatted) or MDAT — ``CALC<ch>:DATA?``."""
        kind = parse_choice(kind, DATA_KINDS)
        ch = self.channel
        with self._operation("read_trace"):
            self._select(trace)
            pairs = True
            if kind == "FDAT":
                pairs = SETTINGS["trace_format"].decode(self.query(f"CALC{ch}:FORM?")) in COMPLEX_FORMATS
            values = self.query_values(f"CALC{ch}:DATA? {kind}")
        if not pairs:
            return values
        if len(values) % 2:
            raise InstrumentError(f"{trace}: número impar de valores ({len(values)}) en datos complejos")
        return values[0::2] + 1j * values[1::2]

    def read_s_parameters(self, ports: int = 2) -> Touchstone:
        """S-matrix of the traces of the channel (one trace per parameter, any name; data SDAT)."""
        traces: dict[str, str] = {}
        for name, parameter in self.list_traces():
            parameter = parameter.upper()
            if name == f"{TRACE_PREFIX}{parameter}" or parameter not in traces:
                traces[parameter] = name  # the CE_ trace of the parameter, else the first one
        wanted = [f"S{i}{j}" for j in range(1, ports + 1) for i in range(1, ports + 1)]
        missing = [p for p in wanted if p not in traces]
        if missing:
            raise InstrumentError(f"faltan trazas que midan {', '.join(missing)}: use setup_s_parameters")
        frequencies = self.get_frequencies()
        s = np.empty((len(frequencies), ports, ports), dtype=complex)
        for parameter in wanted:
            values = self.read_trace(traces[parameter], "SDAT")
            if len(values) != len(frequencies):
                raise InstrumentError(f"{parameter}: {len(values)} puntos y {len(frequencies)} frecuencias")
            s[:, int(parameter[1]) - 1, int(parameter[2]) - 1] = values
        return Touchstone(ports, frequencies, s)

    def save_s2p(self, path: str | Path, data_format: str = "RI", ports: int = 2) -> Path:
        """Read the S-parameters (SDAT: no smoothing) and write them as the software does (RI, MA or DB)."""
        data_format = data_format.strip().upper()
        if data_format not in ("RI", "MA", "DB"):
            raise ValueError(f"«{data_format}» no es un formato Touchstone (RI, MA o DB)")
        path = Path(path)
        if not path.suffix:
            path = path.with_suffix(f".s{ports}p")
        data = self.read_s_parameters(ports)
        data.comments = [
            f"CAL-ELEKTRA, depuración del VNA: {self.identify()}",
            f"{datetime.now():%Y-%m-%d %H:%M:%S}, datos SDAT (sin formato ni suavizado)",
        ]
        write_touchstone(path, data, data_format)
        return path

    # ── export in the analyzer ─────────────────────────────────────────────
    def export_touchstone(
        self,
        file_name: str = f"{EXPORT_FILE}.s2p",
        data_format: str = "COMP",
        ports: Sequence[int] | str = (1, 2),
        impedance: str = "CIMP",
    ) -> str:
        """Touchstone file written by the analyzer (``MMEM:STOR:TRAC:PORT``): COMP = RI, LINP = MA, LOGP = DB.

        A file of two or more ports needs a full calibration of those ports.
        """
        fmt = parse_choice(data_format, EXPORT_FORMATS)
        mode = parse_choice(impedance, IMPEDANCE_MODES)
        port_list = ",".join(str(p) for p in _ports(ports))
        with self._operation("export_touchstone"):
            self.query(f"MMEM:STOR:TRAC:PORT {self.channel},'{file_name}',{fmt},{mode},{port_list};*OPC?", 60_000)
        return file_name

    def download_file(self, file_name: str, local_path: str | Path) -> Path:
        """Copy a file of the analyzer to this computer (``MMEM:DATA?``)."""
        with self._operation("download_file"):
            content = self.query_block(f"MMEM:DATA? '{file_name}'", 60_000)
        local = Path(local_path)
        local.write_bytes(content)
        return local

    def export_and_download(
        self, local_path: str | Path = f"{EXPORT_FILE}.s2p", data_format: str = "COMP", ports: Sequence[int] | str = (1, 2)
    ) -> Path:
        """Export the Touchstone file in the analyzer and copy it here (same file name)."""
        local = Path(local_path)
        self.export_touchstone(local.name, data_format, ports)
        return self.download_file(local.name, local)

    def get_current_directory(self) -> str:
        with self._operation("get_current_directory"):
            return _unquote(self.query("MMEM:CDIR?"))

    def delete_file(self, file_name: str) -> None:
        with self._operation("delete_file"):
            self.write(f"MMEM:DEL '{file_name}'")

    def set_touchstone_version(self, version: str) -> str:
        """F11 (Touchstone 1.1), F20 (2.0) or L11 of the files exported by the analyzer."""
        return self.set("touchstone_version", version)

    def get_touchstone_version(self) -> str:
        return self.get("touchstone_version")

    # ── overview and checks ────────────────────────────────────────────────
    def status(self) -> dict[str, Any]:
        """Settings of the channel and, for each trace, its parameter and processing."""
        channel: dict[str, str] = {}
        sweep_type = None
        for setting in CHANNEL_SETTINGS:
            if setting.key == "step" and sweep_type != "LIN":
                channel[setting.label] = "— (solo en barrido lineal)"
                continue
            try:
                value = self.get(setting.key)
            except InstrumentError as exc:
                channel[setting.label] = f"✗ {exc}"
                continue
            channel[setting.label] = setting.show(value)
            if setting.key == "sweep_type":
                sweep_type = value
                if value == "SEGM":
                    segments = self.get_segments()
                    channel["Segmentos"] = "; ".join(
                        f"{format_frequency(a)}–{format_frequency(b)} ({n})" for a, b, n in segments
                    ) or "ninguno"
        channel["Formato de transferencia"] = self.get_data_format()
        channel["Traza activa"] = self.get_selected_trace()
        traces: dict[str, dict[str, str]] = {}
        for name, parameter in self.list_traces():
            row = {"Parámetro": parameter}
            for setting in TRACE_SETTINGS:
                try:
                    row[setting.label] = setting.show(self.get(setting.key, name))
                except InstrumentError as exc:
                    row[setting.label] = f"✗ {exc}"
            traces[name] = row
        return {f"Canal {self.channel}": channel, "Trazas": traces}

    def self_check(self, sweep: bool = True, export: bool = False, restore: bool = True) -> list:
        """Write and read back every setting, then the traces, sweeps and data (see ``checks.py``)."""
        from calelektra.instruments.znx_debug.checks import run_self_check

        return run_self_check(self, sweep=sweep, export=export, restore=restore)

    def save_log(self, path: str | Path) -> Path:
        path = Path(path)
        path.write_text("\n".join(str(exchange) for exchange in self.history) + "\n", encoding="utf-8")
        return path


def read_exported(path: str | Path) -> Touchstone:
    """Touchstone file copied from the analyzer, with a clear message if it cannot be read."""
    try:
        return read_touchstone(path)
    except TouchstoneError as exc:
        raise InstrumentError(f"el fichero exportado no se puede leer: {exc}") from exc
