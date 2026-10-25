"""Simulated VNA (behaves like an R&S ZNB) used until the real instruments are integrated.

From the point of view of the high-level layer it behaves like the analyzer:

* it rejects settings outside the specifications taken from the equipment
  fields (frequency range, power, IFBW, points, calibration methods);
* every test port has its own systematic errors (directivity, source match,
  reflection tracking including the delay of the test port), so *raw*
  measurements are wrong until the ports are calibrated;
* a calibration is defined, its standards are acquired one by one and then
  applied, like ``SENS:CORR:COLL:…`` on the ZNB. The error terms are solved
  with :mod:`calelektra.core.calibration` and correct every later sweep
  (interpolated when the sweep changes → status ``CAI``);
* the *bench* says what is physically connected to each port: a calibration
  standard, a through, a DUT network or a one-port load. The procedure engine
  changes it when the VNA is simulated, at the same points where it asks the
  operator to connect things.

Error model: 8 terms with an ideal switch (the load match of each direction is
the source match of the other port) plus trace noise that depends on IFBW,
source power and averaging.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Sequence

import numpy as np

from calelektra.core.calibration import (
    STANDARD_GAMMA,
    CalibrationError,
    OnePortTerms,
    TwoPortTerms,
    cascade,
    error_box,
    solve_one_port,
    solve_tosm,
    solve_uosm,
)
from calelektra.core.connectors import Connector
from calelektra.core.frequency import FrequencyRange
from calelektra.core.units import format_frequency
from calelektra.instruments.base import (
    REFLECTION_STANDARDS,
    STANDARD_NAMES,
    TRANSMISSION_STANDARDS,
    InstrumentError,
    VectorNetworkAnalyzer,
    calibration_plan,
)
from calelektra.instruments.simulation import (
    BenchState,
    OnePortModel,
    TwoPortModel,
    interp_complex,
    reflection_model,
    through_model,
)


@dataclass
class _CalibrationSession:
    method: str
    ports: tuple[int, ...]
    plan: list[tuple[str, tuple[int, ...]]]
    frequencies: np.ndarray
    connectors: dict[int, Connector]
    kit_name: str | None
    acquired: dict[tuple[str, tuple[int, ...]], np.ndarray] = field(default_factory=dict)

    def missing(self) -> list[tuple[str, tuple[int, ...]]]:
        return [step for step in self.plan if step not in self.acquired]


@dataclass
class _Correction:
    method: str
    ports: tuple[int, ...]
    frequencies: np.ndarray
    one_port: dict[int, OnePortTerms]
    two_port: TwoPortTerms | None
    created: datetime
    normalization: np.ndarray | None = None  # raw forward transmission of the through (transmission normalization)


def _interp_terms(terms: OnePortTerms, f_cal: np.ndarray, f: np.ndarray) -> OnePortTerms:
    return OnePortTerms(
        directivity=interp_complex(f, f_cal, terms.directivity),
        source_match=interp_complex(f, f_cal, terms.source_match),
        tracking=interp_complex(f, f_cal, terms.tracking),
    )


def _interp_two_port(terms: TwoPortTerms, f_cal: np.ndarray, f: np.ndarray) -> TwoPortTerms:
    return TwoPortTerms(
        port1=_interp_terms(terms.port1, f_cal, f),
        port2=_interp_terms(terms.port2, f_cal, f),
        forward_tracking=interp_complex(f, f_cal, terms.forward_tracking),
        reverse_tracking=interp_complex(f, f_cal, terms.reverse_tracking),
        forward_load_match=interp_complex(f, f_cal, terms.forward_load_match),
        reverse_load_match=interp_complex(f, f_cal, terms.reverse_load_match),
    )


def _pair(ports: tuple[int, int]) -> tuple[list[list[int]], list[int]]:
    """Fancy index selecting the 2×2 sub-matrix of two analyzer ports."""
    a, b = ports
    return [[a - 1], [b - 1]], [a - 1, b - 1]


class DummyVna(VectorNetworkAnalyzer):
    simulated = True

    # calibrations saved «in the instrument» (shared by every simulated VNA of the session)
    _calibration_pool: dict[str, _Correction] = {}

    def __init__(
        self,
        name: str = "VNA dummy",
        frequency_range: FrequencyRange = FrequencyRange(100e3, 43.5e9),
        port_count: int = 2,
        power_range: tuple[float, float] = (-40.0, 0.0),
        if_bandwidth_range: tuple[float, float] = (1.0, 10e6),
        max_points: int = 100001,
        calibration_methods: Sequence[str] = ("OSM", "TOSM", "UOSM", "normalizacion"),
        noise_floor_dbm_at_10hz: float = -130.0,
        seed: int | None = None,
    ):
        super().__init__(name)
        self.frequency_range = frequency_range
        self.port_count = port_count
        self.power_range = power_range
        self.if_bandwidth_range = if_bandwidth_range
        self.max_points = max_points
        self.calibration_methods = tuple(calibration_methods)
        self.noise_floor_dbm_at_10hz = noise_floor_dbm_at_10hz
        self._rng = np.random.default_rng(seed)
        hardware = np.random.default_rng(4242)  # fixed «hardware»: same raw errors every run
        self._port_delay = {p: 3.2e-9 + 0.45e-9 * p for p in range(1, port_count + 1)}
        self._port_phase = {p: hardware.uniform(-np.pi, np.pi, 3) for p in range(1, port_count + 1)}
        self._bench = BenchState(reflections={})
        self._labels: dict[int, str] = {}
        self._session: _CalibrationSession | None = None
        self._correction: _Correction | None = None
        self._correction_on = False
        self.disconnect_ports(record=False)
        self.preset_state()

    # ── state ──────────────────────────────────────────────────────────────
    def preset_state(self) -> None:
        self.start = self.frequency_range.start
        self.stop = self.frequency_range.stop
        self.points = 201
        self.sweep_type = "lineal"
        self.if_bandwidth = 1e3
        self.power = -10.0
        self.averages = 1
        self._list: np.ndarray | None = None
        self._frequencies: np.ndarray | None = None
        self._data: np.ndarray | None = None

    def settings(self) -> dict[str, str]:
        return {
            "inicio": format_frequency(self.start),
            "fin": format_frequency(self.stop),
            "puntos": str(self.points),
            "tipo": self.sweep_type,
            "ifbw": format_frequency(self.if_bandwidth),
            "potencia": f"{self.power:g} dBm",
            "promedios": str(self.averages),
        }

    # ── simulated bench: what is physically connected to the ports ─────────
    @property
    def bench(self) -> BenchState:
        return self._bench

    def bench_description(self) -> str:
        parts = []
        if self._bench.two_port is not None:
            (a, b), _ = self._bench.two_port
            parts.append(f"P{a}–P{b}: {self._labels.get(a, '?')}")
        for port in sorted(self._bench.reflections):
            parts.append(f"P{port}: {self._labels.get(port, '?')}")
        return "; ".join(parts)

    def _check_port(self, port: int) -> None:
        if not 1 <= port <= self.port_count:
            raise InstrumentError(f"{self.name}: el puerto {port} no existe (el VNA tiene {self.port_count})")

    def _release_two_port(self, port: int) -> None:
        """Disconnecting one end of a two-port network leaves its other port open."""
        if self._bench.two_port is not None and port in self._bench.two_port[0]:
            for other in self._bench.two_port[0]:
                self._bench.reflections[other] = reflection_model(1.0)
                self._labels[other] = "abierto"
            self._bench.two_port = None

    def connect_one_port(self, model: OnePortModel, port: int, label: str = "carga") -> None:
        self._check_port(port)
        self._release_two_port(port)
        self._bench.reflections[port] = model
        self._labels[port] = label
        self._record("simulacion.conectar", puerto=port, dispositivo=label)

    def connect_standard(self, standard: str, port: int) -> None:
        if standard not in REFLECTION_STANDARDS:
            raise InstrumentError(f"Estándar de reflexión desconocido: {standard}")
        self.connect_one_port(reflection_model(STANDARD_GAMMA[standard]), port, STANDARD_NAMES[standard])

    def connect_network(self, model: TwoPortModel, ports: Sequence[int] = (1, 2), label: str = "DUT") -> None:
        a, b = ports
        self._check_port(a)
        self._check_port(b)
        if a == b:
            raise InstrumentError("Una red de dos puertos necesita dos puertos distintos del VNA")
        self._release_two_port(a)
        self._release_two_port(b)
        for port in (a, b):
            self._bench.reflections.pop(port, None)
            self._labels[port] = label
        self._bench.two_port = ((a, b), model)
        self._record("simulacion.conectar", puertos=f"{a}-{b}", dispositivo=label)

    def connect_through(self, ports: Sequence[int] = (1, 2), model: TwoPortModel | None = None, label: str | None = None) -> None:
        """Flush through by default; ``model`` simulates an unknown through (adapter, cable…)."""
        default_label = "through" if model is None else "through desconocido"
        self.connect_network(model or through_model(), ports, label or default_label)

    def disconnect_ports(self, record: bool = True) -> None:
        self._bench = BenchState(reflections={p: reflection_model(1.0) for p in range(1, self.port_count + 1)})
        self._labels = {p: "abierto" for p in range(1, self.port_count + 1)}
        if record:
            self._record("simulacion.desconectar_todo")

    # ── Instrument ─────────────────────────────────────────────────────────
    def connect(self) -> None:
        self.connected = True
        self._record("conectar", result="conectado (simulación)")

    def disconnect(self) -> None:
        self.connected = False
        self._record("desconectar")

    def identify(self) -> str:
        self._require_connection()
        text = f"CAL-ELEKTRA,{self.name},SIMULADO,0.3"
        self._record("identificar", result=text)
        return text

    def _fail(self, operation: str, message: str, **arguments) -> None:
        self._record(operation, error=message, **arguments)
        raise InstrumentError(f"{self.name}: {message}")

    # ── sweep settings ─────────────────────────────────────────────────────
    def preset(self) -> None:
        self._require_connection()
        self.preset_state()
        self._session = None
        self._correction_on = False  # like a preset: the calibration stays in the pool, correction off
        self._record("preset")

    def configure_sweep(self, start: float, stop: float, points: int, sweep_type: str = "lineal") -> None:
        self._require_connection()
        args = dict(inicio=format_frequency(start), fin=format_frequency(stop), puntos=points, tipo=sweep_type)
        problem = self.sweep_problem(start, stop, points, sweep_type)
        if problem:
            self._fail("configurar_barrido", problem, **args)
        self.start, self.stop, self.points, self.sweep_type = start, stop, points, sweep_type
        self._list = None
        self._data = None
        self._record("configurar_barrido", result="ok", **args)

    def configure_frequency_list(self, frequencies: Sequence[float]) -> None:
        self._require_connection()
        f = np.asarray(frequencies, dtype=float)
        args = dict(puntos=len(f), inicio=format_frequency(f[0]) if len(f) else "—", fin=format_frequency(f[-1]) if len(f) else "—")
        problem = self.frequency_list_problem(f)
        if problem:
            self._fail("configurar_lista", problem, **args)
        self._list = f.copy()
        self.start, self.stop, self.points, self.sweep_type = float(f[0]), float(f[-1]), len(f), "lista"
        self._data = None
        self._record("configurar_lista", result="ok", **args)

    def set_if_bandwidth(self, hz: float) -> None:
        self._require_connection()
        problem = self.if_bandwidth_problem(hz)
        if problem:
            self._fail("configurar_ifbw", problem, valor=format_frequency(hz))
        self.if_bandwidth = hz
        self._record("configurar_ifbw", result="ok", valor=format_frequency(hz))

    def set_power(self, dbm: float) -> None:
        self._require_connection()
        problem = self.power_problem(dbm)
        if problem:
            self._fail("configurar_potencia", problem, valor=f"{dbm:g} dBm")
        self.power = dbm
        self._record("configurar_potencia", result="ok", valor=f"{dbm:g} dBm")

    def set_averaging(self, count: int) -> None:
        self._require_connection()
        if count < 1:
            self._fail("configurar_promedios", "el número de promedios debe ser ≥ 1", valor=count)
        self.averages = int(count)
        self._record("configurar_promedios", result="ok", valor=self.averages)

    def _sweep_frequencies(self) -> np.ndarray:
        if self.sweep_type == "lista" and self._list is not None:
            return self._list.copy()
        if self.sweep_type == "logaritmico":
            return np.geomspace(self.start, self.stop, self.points)
        return np.linspace(self.start, self.stop, self.points)

    # ── raw measurement through the systematic errors of the ports ─────────
    def _port_errors(self, port: int, f: np.ndarray) -> tuple[OnePortTerms, np.ndarray]:
        """Raw error terms of a port (couplers + test port) and its one-way transmission."""
        phases = self._port_phase[port]
        f_ghz = f / 1e9
        span = max(self.frequency_range.stop / 1e9, 1e-9)
        directivity = 10 ** ((-27 + 9 * f_ghz / span + 2 * np.sin(f_ghz * 1.7 + port)) / 20)
        directivity = directivity * np.exp(1j * (phases[0] + 2 * np.pi * f * 0.21e-9))
        source_match = 10 ** ((-17 + 5 * f_ghz / span + 1.5 * np.cos(f_ghz * 0.9 + port)) / 20)
        source_match = source_match * np.exp(1j * (phases[1] - 2 * np.pi * f * 0.73e-9))
        loss_db = 0.35 * np.sqrt(f_ghz) + 0.025 * f_ghz + 0.6 * port
        transmission = 10 ** (-loss_db / 20) * np.exp(1j * (phases[2] - 2 * np.pi * f * self._port_delay[port]))
        return OnePortTerms(directivity, source_match, transmission**2), transmission

    def _raw_sweep(self, f: np.ndarray) -> np.ndarray:
        n = self.port_count
        raw = np.zeros((len(f), n, n), dtype=complex)
        errors = {p: self._port_errors(p, f) for p in range(1, n + 1)}
        inside_network: set[int] = set()
        if self._bench.two_port is not None:
            (a, b), model = self._bench.two_port
            box_a = error_box(errors[a][0], errors[a][1], facing_dut_at_port1=False)
            box_b = error_box(errors[b][0], errors[b][1], facing_dut_at_port1=True)
            raw[(slice(None), *_pair((a, b)))] = cascade(cascade(box_a, model(f)), box_b)
            inside_network = {a, b}
        for port in range(1, n + 1):
            if port not in inside_network:
                gamma = self._bench.reflections.get(port, reflection_model(1.0))(f)
                raw[:, port - 1, port - 1] = errors[port][0].measure(gamma)
        # receiver noise referred to the source power and reduced by averaging, plus a small trace noise
        noise_db = self.noise_floor_dbm_at_10hz + 10 * np.log10(self.if_bandwidth / 10) - self.power
        sigma = 10 ** (noise_db / 20) / np.sqrt(2 * self.averages)
        noise = self._rng.standard_normal(raw.shape) + 1j * self._rng.standard_normal(raw.shape)
        jitter = self._rng.standard_normal(raw.shape) + 1j * self._rng.standard_normal(raw.shape)
        return raw * (1 + 2e-5 / np.sqrt(self.averages) * jitter) + sigma * noise

    # ── correction ─────────────────────────────────────────────────────────
    def _correction_for(self, f: np.ndarray) -> tuple[str, _Correction | None]:
        correction = self._correction
        if correction is None:
            return "NONE", None
        if not self._correction_on:
            return "OFF", None
        f_cal = correction.frequencies
        if len(f) == len(f_cal) and np.allclose(f, f_cal, rtol=1e-12, atol=1e-6):
            return "CAL", correction
        if f[0] >= f_cal[0] * (1 - 1e-12) and f[-1] <= f_cal[-1] * (1 + 1e-12):
            interpolated = _Correction(
                method=correction.method,
                ports=correction.ports,
                frequencies=f,
                one_port={p: _interp_terms(t, f_cal, f) for p, t in correction.one_port.items()},
                two_port=_interp_two_port(correction.two_port, f_cal, f) if correction.two_port else None,
                created=correction.created,
                normalization=interp_complex(f, f_cal, correction.normalization) if correction.normalization is not None else None,
            )
            return "CAI", interpolated
        return "OFF", None  # sweep outside the calibrated range: the correction cannot be applied

    @staticmethod
    def _apply_correction(raw: np.ndarray, correction: _Correction) -> np.ndarray:
        data = raw.copy()
        if correction.normalization is not None:  # only the forward transmission is corrected
            a, b = correction.ports
            data[:, b - 1, a - 1] = raw[:, b - 1, a - 1] / correction.normalization
        elif correction.two_port is not None:
            index = (slice(None), *_pair(correction.ports))  # type: ignore[arg-type]
            data[index] = correction.two_port.correct(raw[index])
        else:
            for port, terms in correction.one_port.items():
                data[:, port - 1, port - 1] = terms.correct(raw[:, port - 1, port - 1])
        return data

    def single_sweep(self) -> None:
        self._require_connection()
        f = self._sweep_frequencies()
        raw = self._raw_sweep(f)
        status, correction = self._correction_for(f)
        data = self._apply_correction(raw, correction) if correction is not None else raw
        if status == "OFF" and self._correction is not None and self._correction_on:
            self._record("aviso", result="barrido fuera del rango calibrado: corrección no aplicada")
        self._frequencies, self._data = f, data
        self._record("barrido_unico", result=f"{len(f)} puntos, {status}, banco: {self.bench_description()}")

    def frequencies(self) -> np.ndarray:
        self._require_connection()
        f = self._frequencies if self._frequencies is not None else self._sweep_frequencies()
        self._record("leer_frecuencias", result=f"{len(f)} valores")
        return f.copy()

    def read_s_parameter(self, name: str) -> np.ndarray:
        self._require_connection()
        match = re.fullmatch(r"[Ss](\d)(\d)", name)
        if not match:
            self._fail("leer_parametro_s", f"parámetro no válido: {name}", parametro=name)
        i, j = int(match.group(1)) - 1, int(match.group(2)) - 1
        if not (0 <= i < self.port_count and 0 <= j < self.port_count):
            self._fail("leer_parametro_s", f"{name} no existe en un VNA de {self.port_count} puertos", parametro=name)
        if self._data is None:
            self._fail("leer_parametro_s", "no hay ningún barrido realizado", parametro=name)
        self._record("leer_parametro_s", result=f"{self._data.shape[0]} puntos complejos", parametro=name)
        return self._data[:, i, j].copy()

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
        if problem:
            self._fail("iniciar_calibracion", problem, **args)
        plan = calibration_plan(method, ports)
        self._session = _CalibrationSession(
            method=method,
            ports=ports,
            plan=plan,
            frequencies=self._sweep_frequencies(),
            connectors=dict(connectors or {}),
            kit_name=kit_name,
        )
        connector_text = ", ".join(f"P{p}: {c.label()}" for p, c in sorted((connectors or {}).items()))
        self._record("iniciar_calibracion", result=f"{len(plan)} estándares; {connector_text or 'conectores sin indicar'}", **args)

    def acquire_standard(self, standard: str, ports: Sequence[int]) -> None:
        self._require_connection()
        ports = tuple(int(p) for p in ports)
        args = dict(estandar=standard, puertos=",".join(map(str, ports)))
        session = self._session
        if session is None:
            self._fail("adquirir_estandar", "no hay ninguna calibración iniciada", **args)
        if (standard, ports) not in session.plan:
            self._fail("adquirir_estandar", f"{standard} en {ports} no forma parte de la calibración {session.method}", **args)
        f = self._sweep_frequencies()
        if len(f) != len(session.frequencies) or not np.allclose(f, session.frequencies):
            self._fail("adquirir_estandar", "el barrido ha cambiado desde que se inició la calibración", **args)
        raw = self._raw_sweep(f)
        if standard in TRANSMISSION_STANDARDS:
            session.acquired[(standard, ports)] = raw[(slice(None), *_pair(ports))]  # type: ignore[arg-type]
        else:
            session.acquired[(standard, ports)] = raw[:, ports[0] - 1, ports[0] - 1]
        done = len(session.plan) - len(session.missing())
        self._record("adquirir_estandar", result=f"{done}/{len(session.plan)} — banco: {self.bench_description()}", **args)

    def calibration_progress(self) -> tuple[int, int] | None:
        if self._session is None:
            return None
        return len(self._session.plan) - len(self._session.missing()), len(self._session.plan)

    def apply_calibration(self) -> None:
        self._require_connection()
        session = self._session
        if session is None:
            self._fail("aplicar_calibracion", "no hay ninguna calibración iniciada")
        missing = session.missing()
        if missing:
            names = ", ".join(f"{STANDARD_NAMES[s]} ({'-'.join(map(str, p))})" for s, p in missing)
            self._fail("aplicar_calibracion", f"faltan estándares: {names}")
        normalization = None
        try:
            one_port = {
                port: solve_one_port({s: session.acquired[(s, (port,))] for s in REFLECTION_STANDARDS})
                for port in session.ports
                if session.method != "normalizacion"
            }
            two_port = None
            if session.method == "normalizacion":
                normalization = session.acquired[("THROUGH", session.ports)][:, 1, 0]
            elif session.method in ("TOSM", "UOSM"):
                a, b = session.ports
                if session.method == "TOSM":
                    two_port = solve_tosm(one_port[a], one_port[b], session.acquired[("THROUGH", (a, b))])
                else:
                    through = session.acquired[("UNKNOWN_THROUGH", (a, b))]
                    two_port = solve_uosm(one_port[a], one_port[b], through, session.frequencies)
        except (CalibrationError, np.linalg.LinAlgError, ZeroDivisionError) as exc:
            self._fail("aplicar_calibracion", f"no se pueden calcular los términos de error ({exc})")
        terms = [t.tracking for t in one_port.values()]
        if two_port is not None:
            terms += [two_port.forward_tracking, two_port.reverse_tracking]
        if normalization is not None:
            terms.append(normalization)
        if not all(np.all(np.isfinite(t)) and np.all(np.abs(t) > 1e-9) for t in terms):
            self._fail("aplicar_calibracion", "términos de error no válidos (¿estándares mal conectados?)")
        self._correction = _Correction(
            method=session.method,
            ports=session.ports,
            frequencies=session.frequencies,
            one_port=one_port,
            two_port=two_port,
            created=datetime.now(),
            normalization=normalization,
        )
        self._correction_on = True
        self._session = None
        ports_text = "-".join(f"P{p}" for p in self._correction.ports)
        self._record("aplicar_calibracion", result=f"{self._correction.method} aplicada en {ports_text}")

    def calibration_status(self) -> str:
        self._require_connection()
        status, _ = self._correction_for(self._sweep_frequencies())
        self._record("estado_calibracion", result=status)
        return status

    def calibration_info(self) -> dict[str, str] | None:
        if self._correction is None:
            return None
        c = self._correction
        return {
            "metodo": c.method,
            "puertos": ",".join(map(str, c.ports)),
            "rango": f"{format_frequency(c.frequencies[0])} – {format_frequency(c.frequencies[-1])}",
            "puntos": str(len(c.frequencies)),
            "fecha": f"{c.created:%Y-%m-%d %H:%M:%S}",
        }

    def set_correction(self, enabled: bool) -> None:
        self._require_connection()
        if enabled and self._correction is None:
            self._fail("activar_correccion", "no hay ninguna calibración que activar")
        self._correction_on = bool(enabled)
        self._record("activar_correccion", result="ON" if enabled else "OFF")

    def save_calibration(self, name: str) -> None:
        self._require_connection()
        if self._correction is None:
            self._fail("guardar_calibracion", "no hay ninguna calibración activa", nombre=name)
        self._calibration_pool[name] = self._correction
        self._record("guardar_calibracion", result="ok", nombre=name)

    def load_calibration(self, name: str) -> None:
        self._require_connection()
        if name not in self._calibration_pool:
            self._fail("cargar_calibracion", f"no existe la calibración «{name}»", nombre=name)
        self._correction = self._calibration_pool[name]
        self._correction_on = True
        self._record("cargar_calibracion", result="ok", nombre=name)
