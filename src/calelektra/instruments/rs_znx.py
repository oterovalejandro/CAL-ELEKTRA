"""Adapter of the R&S ZNB / ZNx analyzers (ZNB43 in the lab) to the high-level VNA interface.

Every high-level operation sends the SCPI commands that the manufacturer's
driver (R&S RsZnx IVI.NET driver 4.25.0) sends for it; the correspondence is
listed in ``docs/04_algoritmo_medida_vna.md``. The transport is VISA (HiSLIP or
VXI-11) through pyvisa, installed with the optional dependency
``pip install -e .[instrumentos]`` plus a VISA backend (R&S VISA, NI-VISA or
pyvisa-py).

PENDING: this adapter has not been run against the real ZNB43 yet. The tests
check the command sequence with :class:`RecordingTransport`.
"""

from __future__ import annotations

import re
from contextlib import contextmanager
from typing import Any, Callable, Iterator, Protocol, Sequence

import numpy as np

from calelektra.core.connectors import ZNX_KIT_CONNECTOR, Connector
from calelektra.core.frequency import FrequencyRange
from calelektra.core.units import format_frequency
from calelektra.instruments.base import InstrumentError, VectorNetworkAnalyzer

STANDARD_SCPI = {"OPEN": "OPEN", "SHORT": "SHOR", "MATCH": "MATC", "THROUGH": "THR", "UNKNOWN_THROUGH": "UTHR"}
METHOD_SCPI = {"OSM": "FOP", "TOSM": "TOSM", "UOSM": "UOSM"}
SWEEP_SCPI = {"lineal": "LIN", "logaritmico": "LOG"}
STATUS_FROM_SCPI = {"CAL": "CAL", "CAI": "CAI", "CA?": "CA?", "CAV": "CAV", "CAL OFF": "OFF", "": "NONE"}
TRACE_PREFIX = "CE_"
CALIBRATION_NAME = "CALELEKTRA"


class ScpiTransport(Protocol):
    binary: bool  # True: numeric blocks as REAL,32; False: ASCII

    def write(self, command: str) -> None: ...

    def query(self, command: str, timeout_ms: int | None = None) -> str: ...

    def query_floats(self, command: str, timeout_ms: int | None = None) -> np.ndarray: ...

    def close(self) -> None: ...


class PyVisaTransport:
    """VISA session (pyvisa) with binary transfer of the traces."""

    binary = True

    def __init__(self, resource: str, timeout_ms: int = 10_000):
        try:
            import pyvisa
        except ImportError as exc:
            raise InstrumentError(
                "Falta pyvisa. Instale las dependencias de instrumentos: pip install -e .[instrumentos] "
                "y un backend VISA (R&S VISA, NI-VISA o pyvisa-py)."
            ) from exc
        try:
            self._manager = pyvisa.ResourceManager()
            self._session = self._manager.open_resource(resource)
        except Exception as exc:  # pyvisa raises several error types
            raise InstrumentError(f"No se puede abrir la sesión VISA «{resource}»: {exc}") from exc
        self._session.timeout = timeout_ms
        self._session.read_termination = "\n"
        self._session.write_termination = "\n"

    @contextmanager
    def _timeout(self, timeout_ms: int | None) -> Iterator[None]:
        if timeout_ms is None:
            yield
            return
        previous = self._session.timeout
        self._session.timeout = timeout_ms
        try:
            yield
        finally:
            self._session.timeout = previous

    def write(self, command: str) -> None:
        self._session.write(command)

    def query(self, command: str, timeout_ms: int | None = None) -> str:
        with self._timeout(timeout_ms):
            return str(self._session.query(command)).strip()

    def query_floats(self, command: str, timeout_ms: int | None = None) -> np.ndarray:
        with self._timeout(timeout_ms):
            values = self._session.query_binary_values(command, datatype="f", is_big_endian=False, container=np.array)
        return np.asarray(values, dtype=float)

    def close(self) -> None:
        self._session.close()


Response = str | Callable[[str], str]

DEFAULT_RESPONSES: dict[str, Response] = {
    "*IDN?": "Rohde-Schwarz,ZNB43-2Port,000000,(grabación)",
    "SYST:ERR?": '0,"No error"',
    "SENS1:CORR:SST?": "'CAL'",
    "SENS1:SWE:TIME?": "0.05",
    "CALC1:PAR:CAT?": "'Trc1,S21'",
}


class RecordingTransport:
    """Transport for tests and dry runs: records every command and answers queries from a table.

    ``responses`` maps a command prefix to a fixed answer or to a function of
    the command; the longest matching prefix wins. ``…*OPC?`` answers ``1``.
    """

    binary = False

    def __init__(self, responses: dict[str, Response] | None = None):
        self.commands: list[str] = []
        self.responses: dict[str, Response] = {**DEFAULT_RESPONSES, **(responses or {})}

    def _answer(self, command: str) -> str:
        for prefix in sorted(self.responses, key=len, reverse=True):
            if command.startswith(prefix):
                answer = self.responses[prefix]
                return answer(command) if callable(answer) else answer
        return "1" if command.endswith("*OPC?") else "0"

    def write(self, command: str) -> None:
        self.commands.append(command)

    def query(self, command: str, timeout_ms: int | None = None) -> str:
        self.commands.append(command)
        return self._answer(command)

    def query_floats(self, command: str, timeout_ms: int | None = None) -> np.ndarray:
        text = self.query(command, timeout_ms)
        return np.array([float(item) for item in text.split(",") if item.strip()])

    def close(self) -> None:
        self.commands.append("<cerrar sesión>")


def _number(value: float) -> str:
    return f"{value:.12g}"


def frequency_segments(frequencies: Sequence[float]) -> list[tuple[float, float, int]]:
    """Group a frequency list into segments of equal step: ``(start, stop, points)``."""
    f = [float(x) for x in frequencies]
    segments: list[tuple[float, float, int]] = []
    i = 0
    while i < len(f):
        if i == len(f) - 1:
            segments.append((f[i], f[i], 1))
            break
        step = f[i + 1] - f[i]
        j = i + 1
        while j + 1 < len(f) and abs((f[j + 1] - f[j]) - step) <= 1e-6 * step:
            j += 1
        segments.append((f[i], f[j], j - i + 1))
        i = j + 1
    return segments


class RsZnxVna(VectorNetworkAnalyzer):
    """R&S ZNB43 (and other ZNx) on channel 1, with one trace per S-parameter (``CE_S11``…)."""

    def __init__(
        self,
        name: str,
        resource: str | None,
        frequency_range: FrequencyRange,
        port_count: int = 2,
        power_range: tuple[float, float] | None = None,
        if_bandwidth_range: tuple[float, float] | None = None,
        max_points: int | None = None,
        calibration_methods: Sequence[str] = ("OSM", "TOSM", "UOSM"),
        transport: ScpiTransport | None = None,
        timeout_ms: int = 10_000,
        channel: int = 1,
    ):
        super().__init__(name)
        self.resource = resource
        self.frequency_range = frequency_range
        self.port_count = port_count
        self.power_range = power_range
        self.if_bandwidth_range = if_bandwidth_range
        self.max_points = max_points
        self.calibration_methods = tuple(calibration_methods)
        self.transport = transport
        self.timeout_ms = timeout_ms
        self.channel = channel
        self._averages = 1
        self._power = -10.0
        self._if_bandwidth = 1e3
        self._traces: set[str] = set()

    # ── SCPI helpers ───────────────────────────────────────────────────────
    def _io(self, action: Callable[[], Any], command: str) -> Any:
        if self.transport is None:
            raise InstrumentError(f"{self.name}: el equipo no está conectado")
        try:
            return action()
        except InstrumentError:
            raise
        except Exception as exc:  # VISA timeouts and I/O errors
            self._record("scpi", error=str(exc), comando=command)
            raise InstrumentError(f"{self.name}: error de comunicación en «{command}»: {exc}") from exc

    def _write(self, command: str) -> None:
        self._io(lambda: self.transport.write(command), command)  # type: ignore[union-attr]
        self._record("scpi", comando=command)

    def _query(self, command: str, timeout_ms: int | None = None) -> str:
        answer = self._io(lambda: self.transport.query(command, timeout_ms), command)  # type: ignore[union-attr]
        self._record("scpi", result=answer[:80], comando=command)
        return answer

    def _query_floats(self, command: str, timeout_ms: int | None = None) -> np.ndarray:
        values = self._io(lambda: self.transport.query_floats(command, timeout_ms), command)  # type: ignore[union-attr]
        self._record("scpi", result=f"{len(values)} valores", comando=command)
        return values

    def _write_opc(self, command: str, timeout_ms: int | None = None) -> None:
        """Send an overlapped command and wait for it to finish (``;*OPC?``)."""
        self._query(f"{command};*OPC?", timeout_ms)

    def _errors(self) -> list[str]:
        errors = []
        for _ in range(50):
            answer = self._query("SYST:ERR?")
            if answer.startswith("0") or answer.startswith("+0"):
                break
            errors.append(answer)
        return errors

    def _check(self, operation: str, **arguments: Any) -> None:
        errors = self._errors()
        if errors:
            message = "; ".join(errors)
            self._record(operation, error=message, **arguments)
            raise InstrumentError(f"{self.name}: el VNA ha rechazado «{operation}»: {message}")
        self._record(operation, result="ok", **arguments)

    def _fail(self, operation: str, message: str, **arguments: Any) -> None:
        self._record(operation, error=message, **arguments)
        raise InstrumentError(f"{self.name}: {message}")

    @property
    def _ch(self) -> int:
        return self.channel

    # ── Instrument ─────────────────────────────────────────────────────────
    def connect(self) -> None:
        if self.transport is None:
            if not self.resource:
                raise InstrumentError(f"{self.name}: falta el recurso VISA (conexión → recurso)")
            self.transport = PyVisaTransport(self.resource, self.timeout_ms)
        self.connected = True
        identity = self._query("*IDN?")
        self._write("*CLS")
        if self.transport.binary:
            self._write("FORM:BORD SWAP")
            self._write(":FORM REAL,32")
        else:
            self._write(":FORM ASC")
        self._check("conectar", identificacion=identity)

    def disconnect(self) -> None:
        if self.transport is not None:
            try:
                self._write(f"INIT{self._ch}:CONT ON")
            except InstrumentError:
                pass
            self.transport.close()
        self.connected = False
        self._record("desconectar")

    def identify(self) -> str:
        self._require_connection()
        return self._query("*IDN?")

    # ── sweep ──────────────────────────────────────────────────────────────
    def _trace_name(self, receive: int, source: int) -> str:
        return f"{TRACE_PREFIX}S{receive}{source}"

    def _ensure_traces(self) -> None:
        """One trace per S-parameter of the channel ports (they must exist before the sweep)."""
        wanted = [(i, j) for i in range(1, self.port_count + 1) for j in range(1, self.port_count + 1)]
        for i, j in wanted:
            name = self._trace_name(i, j)
            if name not in self._traces:
                self._write(f"CALC{self._ch}:PAR:SDEF '{name}','S{i}{j}'")
                self._write(f"DISP:WIND1:TRAC:EFE '{name}'")
                self._traces.add(name)

    def preset(self) -> None:
        self._require_connection()
        self._write_opc("*RST", timeout_ms=60_000)
        self._traces.clear()
        self._write(f"INIT{self._ch}:CONT OFF")  # single sweep mode: the software triggers each sweep
        self._ensure_traces()
        catalog = self._query(f"CALC{self._ch}:PAR:CAT?").strip("'\"")
        names = catalog.split(",")[0::2] if catalog else []
        for name in names:
            name = name.strip()
            if name and not name.startswith(TRACE_PREFIX):
                self._write(f"CALC{self._ch}:PAR:DEL '{name}'")
        self._averages = 1
        self._check("preset")

    def configure_sweep(self, start: float, stop: float, points: int, sweep_type: str = "lineal") -> None:
        self._require_connection()
        args = dict(inicio=format_frequency(start), fin=format_frequency(stop), puntos=points, tipo=sweep_type)
        problem = self.sweep_problem(start, stop, points, sweep_type)
        if problem:
            self._fail("configurar_barrido", problem, **args)
        ch = self._ch
        self._write(f"SENS{ch}:SWE:TYPE {SWEEP_SCPI[sweep_type]}")
        self._write(f"SENS{ch}:FREQ:STAR {_number(start)}")
        self._write(f"SENS{ch}:FREQ:STOP {_number(stop)}")
        self._write(f"SENS{ch}:SWE:POIN {points}")
        self._check("configurar_barrido", **args)

    def set_if_bandwidth(self, hz: float) -> None:
        self._require_connection()
        problem = self.if_bandwidth_problem(hz)
        if problem:
            self._fail("configurar_ifbw", problem, valor=format_frequency(hz))
        self._write(f"SENS{self._ch}:BAND {_number(hz)}")
        self._if_bandwidth = hz
        self._check("configurar_ifbw", valor=format_frequency(hz))

    def set_power(self, dbm: float) -> None:
        self._require_connection()
        problem = self.power_problem(dbm)
        if problem:
            self._fail("configurar_potencia", problem, valor=f"{dbm:g} dBm")
        self._write(f"SOUR{self._ch}:POW {_number(dbm)}")
        self._power = dbm
        self._check("configurar_potencia", valor=f"{dbm:g} dBm")

    def configure_frequency_list(self, frequencies: Sequence[float]) -> None:
        """Segmented sweep: equal-step runs of the list become segments (``SENS:SEGM<n>:INS``).

        The channel IFBW and power apply to every segment (``SEGM:BWID:CONT OFF``,
        ``SEGM:POW:CONT OFF``). PENDING: check on the ZNB43 the maximum number of segments.
        """
        self._require_connection()
        f = np.asarray(frequencies, dtype=float)
        args = dict(puntos=len(f))
        problem = self.frequency_list_problem(f)
        if problem:
            self._fail("configurar_lista", problem, **args)
        ch = self._ch
        segments = frequency_segments(f)
        self._write(f"SENS{ch}:SEGM:DEL:ALL")
        for number, (start, stop, points) in enumerate(segments, start=1):
            self._write_opc(
                f"SENS{ch}:SEGM{number}:INS {_number(start)},{_number(stop)},{points},{_number(self._power)},0,0,"
                f"{_number(self._if_bandwidth)},AUTO,NORM,AUTO,STEP"
            )
        self._write(f"SENS{ch}:SEGM:BWID:CONT OFF")
        self._write(f"SENS{ch}:SEGM:POW:CONT OFF")
        self._write(f"SENS{ch}:SWE:TYPE SEGM")
        self._check("configurar_lista", segmentos=len(segments), **args)

    def set_averaging(self, count: int) -> None:
        self._require_connection()
        if count < 1:
            self._fail("configurar_promedios", "el número de promedios debe ser ≥ 1", valor=count)
        ch = self._ch
        if count == 1:
            self._write(f"SENS{ch}:AVER OFF")
            self._write(f"SENS{ch}:SWE:COUN 1")
        else:
            self._write(f"SENS{ch}:AVER:COUN {count}")
            self._write(f"SENS{ch}:AVER ON")
            self._write(f"SENS{ch}:SWE:COUN {count}")  # one INIT runs all the averaged sweeps
        self._averages = int(count)
        self._check("configurar_promedios", valor=count)

    def _sweep_timeout_ms(self) -> int:
        try:
            seconds = float(self._query(f"SENS{self._ch}:SWE:TIME?"))
        except (InstrumentError, ValueError):
            seconds = 10.0
        return int(1000 * (2 * seconds * self._averages + 10))

    def single_sweep(self) -> None:
        self._require_connection()
        self._ensure_traces()
        if self._averages > 1:
            self._write(f"SENS{self._ch}:AVER:CLE")
        self._write_opc(f"INIT{self._ch}", timeout_ms=self._sweep_timeout_ms())
        self._check("barrido_unico", promedios=self._averages)

    def frequencies(self) -> np.ndarray:
        self._require_connection()
        return self._query_floats(f"CALC{self._ch}:DATA:STIM?")

    def read_s_parameter(self, name: str) -> np.ndarray:
        self._require_connection()
        match = re.fullmatch(r"[Ss](\d)(\d)", name)
        if not match:
            self._fail("leer_parametro_s", f"parámetro no válido: {name}", parametro=name)
        i, j = int(match.group(1)), int(match.group(2))
        if not (1 <= i <= self.port_count and 1 <= j <= self.port_count):
            self._fail("leer_parametro_s", f"{name} no existe en un VNA de {self.port_count} puertos", parametro=name)
        trace = self._trace_name(i, j)
        if trace not in self._traces:
            self._fail("leer_parametro_s", f"la traza {trace} no existe: ejecute preset() y un barrido", parametro=name)
        self._write(f"CALC{self._ch}:PAR:SEL '{trace}'")
        values = self._query_floats(f"CALC{self._ch}:DATA? SDAT")
        if len(values) % 2:
            self._fail("leer_parametro_s", f"respuesta con número impar de valores ({len(values)})", parametro=name)
        self._record("leer_parametro_s", result=f"{len(values) // 2} puntos complejos", parametro=name)
        return values[0::2] + 1j * values[1::2]

    # ── calibration ────────────────────────────────────────────────────────
    def begin_calibration(
        self,
        method: str,
        ports: Sequence[int],
        connectors: dict[int, Connector] | None = None,
        kit_name: str | None = None,
        kit_family: str | None = None,
    ) -> None:
        self._require_connection()
        ports = tuple(int(p) for p in ports)
        args = dict(metodo=method, puertos=",".join(map(str, ports)), kit=kit_name or "—")
        problem = self.calibration_problem(method, ports)
        if method == "OSM" and len(ports) != 1:
            problem = problem or "el adaptador R&S calibra OSM en un solo puerto (FOP)"
        if problem:
            self._fail("iniciar_calibracion", problem, **args)
        ch = self._ch
        if connectors:
            self._write(f"SENS{ch}:CORR:COLL:CONN:PORT SING")
            self._write(f"SENS{ch}:CORR:COLL:CONN:GEND SING")
            for port in ports:
                connector = connectors.get(port)
                if connector is None:
                    continue
                name = connector.znx_name()
                if name is None:
                    self._fail("iniciar_calibracion", f"el conector {connector.label()} no tiene nombre en el ZNB", **args)
                self._write(f"SENS{ch}:CORR:COLL:CONN{port} {name}")
        if kit_name:
            family = kit_family or next((c.family for c in (connectors or {}).values()), None)
            selector = ZNX_KIT_CONNECTOR.get(family or "")
            if selector:
                self._write_opc(f"SENS:CORR:CKIT:{selector}:SEL '{kit_name}'")
            elif family:
                self._write(f"SENS:CORR:CKIT:SEL '{family}','{kit_name}'")
        port_list = ",".join(map(str, ports))
        self._write(f"SENS{ch}:CORR:COLL:METH:DEF '{CALIBRATION_NAME}',{METHOD_SCPI[method]},{port_list}")
        self._method = method
        self._check("iniciar_calibracion", **args)

    def acquire_standard(self, standard: str, ports: Sequence[int]) -> None:
        self._require_connection()
        ports = tuple(int(p) for p in ports)
        args = dict(estandar=standard, puertos=",".join(map(str, ports)))
        if standard not in STANDARD_SCPI:
            self._fail("adquirir_estandar", f"estándar desconocido: {standard}", **args)
        command = f"SENS{self._ch}:CORR:COLL:SEL {STANDARD_SCPI[standard]},{','.join(map(str, ports))}"
        if standard == "UNKNOWN_THROUGH":
            command += ",OFF,AUTO"  # no dispersion, delay estimated automatically
        self._write_opc(command, timeout_ms=self._sweep_timeout_ms())
        self._check("adquirir_estandar", **args)

    def apply_calibration(self) -> None:
        self._require_connection()
        self._write_opc(f"SENS{self._ch}:CORR:COLL:SAVE:SEL", timeout_ms=60_000)
        self._check("aplicar_calibracion")

    def calibration_status(self) -> str:
        self._require_connection()
        answer = self._query(f"SENS{self._ch}:CORR:SST?").strip().strip("'\"").strip()
        status = STATUS_FROM_SCPI.get(answer.upper(), answer.upper() or "NONE")
        self._record("estado_calibracion", result=status)
        return status

    def set_correction(self, enabled: bool) -> None:
        self._require_connection()
        self._write(f"SENS{self._ch}:CORR {'ON' if enabled else 'OFF'}")
        self._check("activar_correccion", estado="ON" if enabled else "OFF")

    def save_calibration(self, name: str) -> None:
        self._require_connection()
        self._write_opc(f"MMEM:STOR:CORR {self._ch},'{name}.cal'")
        self._check("guardar_calibracion", nombre=name)

    def load_calibration(self, name: str) -> None:
        self._require_connection()
        self._write_opc(f"MMEM:LOAD:CORR {self._ch},'{name}.cal'")
        self._check("cargar_calibracion", nombre=name)
