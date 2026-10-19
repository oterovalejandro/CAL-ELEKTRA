"""Simulated VNA used until the real instruments are integrated.

It behaves like a real analyzer from the point of view of the high-level layer:
it rejects settings outside its specifications, keeps a sweep state and returns
complex S-parameters of a simulated device connected between its ports
(ideal standards or the S-parameters of a Touchstone file) plus noise that
depends on the IF bandwidth.
"""

from __future__ import annotations

import re

import numpy as np

from calelektra.core.frequency import FrequencyRange
from calelektra.core.touchstone import Touchstone
from calelektra.core.units import format_frequency
from calelektra.instruments.base import SWEEP_TYPES, InstrumentError, VectorNetworkAnalyzer

STANDARD_DEVICES = ("through", "abierto", "cortocircuito", "carga")


class DummyVna(VectorNetworkAnalyzer):
    simulated = True

    def __init__(
        self,
        name: str = "VNA dummy",
        frequency_range: FrequencyRange = FrequencyRange(100e3, 43.5e9),
        port_count: int = 2,
        power_range: tuple[float, float] = (-40.0, 0.0),
        if_bandwidth_range: tuple[float, float] = (1.0, 10e6),
        max_points: int = 100001,
        noise_floor_db_at_10hz: float = -130.0,
        seed: int | None = None,
    ):
        super().__init__(name)
        self.frequency_range = frequency_range
        self.port_count = port_count
        self.power_range = power_range
        self.if_bandwidth_range = if_bandwidth_range
        self.max_points = max_points
        self.noise_floor_db_at_10hz = noise_floor_db_at_10hz
        self._rng = np.random.default_rng(seed)
        self._device: str | Touchstone = "through"
        self._device_label = "through"
        self.preset_state()

    # ── state ──────────────────────────────────────────────────────────────
    def preset_state(self) -> None:
        self.start = self.frequency_range.start
        self.stop = self.frequency_range.stop
        self.points = 201
        self.sweep_type = "lineal"
        self.if_bandwidth = 1e3
        self.power = -10.0
        self._frequencies: np.ndarray | None = None
        self._data: np.ndarray | None = None

    def connect_device(self, device: str | Touchstone, label: str | None = None) -> None:
        """Simulated DUT between the ports: a standard name or a Touchstone network."""
        if isinstance(device, str) and device not in STANDARD_DEVICES:
            raise InstrumentError(f"Dispositivo simulado desconocido: {device}")
        if isinstance(device, Touchstone) and device.n_ports > self.port_count:
            raise InstrumentError(f"El VNA tiene {self.port_count} puertos y el dispositivo {device.n_ports}")
        self._device = device
        self._device_label = label or (device if isinstance(device, str) else "red Touchstone")
        self._record("conectar_dispositivo_simulado", dispositivo=self._device_label)

    # ── Instrument ─────────────────────────────────────────────────────────
    def connect(self) -> None:
        self.connected = True
        self._record("conectar", result="conectado (simulación)")

    def disconnect(self) -> None:
        self.connected = False
        self._record("desconectar")

    def identify(self) -> str:
        self._require_connection()
        text = f"CAL-ELEKTRA,{self.name},SIMULADO,0.1"
        self._record("identificar", result=text)
        return text

    # ── VectorNetworkAnalyzer ──────────────────────────────────────────────
    def _fail(self, operation: str, message: str, **arguments) -> None:
        self._record(operation, error=message, **arguments)
        raise InstrumentError(f"{self.name}: {message}")

    def preset(self) -> None:
        self._require_connection()
        self.preset_state()
        self._record("preset")

    def configure_sweep(self, start: float, stop: float, points: int, sweep_type: str = "lineal") -> None:
        self._require_connection()
        args = dict(inicio=format_frequency(start), fin=format_frequency(stop), puntos=points, tipo=sweep_type)
        if sweep_type not in SWEEP_TYPES:
            self._fail("configurar_barrido", f"tipo de barrido no soportado: {sweep_type}", **args)
        if stop <= start:
            self._fail("configurar_barrido", "la frecuencia final debe ser mayor que la inicial", **args)
        requested = FrequencyRange(start, stop)
        if not self.frequency_range.contains(requested):
            self._fail(
                "configurar_barrido",
                f"rango {requested} fuera de las especificaciones del VNA ({self.frequency_range})",
                **args,
            )
        if not 2 <= points <= self.max_points:
            self._fail("configurar_barrido", f"número de puntos fuera de rango (2 – {self.max_points})", **args)
        if sweep_type == "logaritmico" and start <= 0:
            self._fail("configurar_barrido", "el barrido logarítmico no admite 0 Hz", **args)
        self.start, self.stop, self.points, self.sweep_type = start, stop, points, sweep_type
        self._data = None
        self._record("configurar_barrido", result="ok", **args)

    def set_if_bandwidth(self, hz: float) -> None:
        self._require_connection()
        low, high = self.if_bandwidth_range
        if not low <= hz <= high:
            self._fail("configurar_ifbw", f"IFBW fuera de rango ({format_frequency(low)} – {format_frequency(high)})", valor=hz)
        self.if_bandwidth = hz
        self._record("configurar_ifbw", result="ok", valor=format_frequency(hz))

    def set_power(self, dbm: float) -> None:
        self._require_connection()
        low, high = self.power_range
        if not low <= dbm <= high:
            self._fail("configurar_potencia", f"potencia fuera de rango ({low:g} – {high:g} dBm)", valor=dbm)
        self.power = dbm
        self._record("configurar_potencia", result="ok", valor=f"{dbm:g} dBm")

    def _sweep_frequencies(self) -> np.ndarray:
        if self.sweep_type == "logaritmico":
            return np.geomspace(self.start, self.stop, self.points)
        return np.linspace(self.start, self.stop, self.points)

    def _device_s(self, frequencies: np.ndarray) -> np.ndarray:
        n = self.port_count
        s = np.zeros((len(frequencies), n, n), dtype=complex)
        device = self._device
        if isinstance(device, Touchstone):
            m = device.n_ports
            for i in range(m):
                for j in range(m):
                    trace = device.s[:, i, j]
                    # magnitude and unwrapped phase: real/imag interpolation fails on fast-rotating phasors
                    magnitude = np.interp(frequencies, device.frequencies, np.abs(trace), left=np.nan, right=np.nan)
                    phase = np.interp(frequencies, device.frequencies, np.unwrap(np.angle(trace)))
                    s[:, i, j] = magnitude * np.exp(1j * phase)
            return s
        if device == "through" and n >= 2:
            s[:, 1, 0] = s[:, 0, 1] = 1.0
        elif device == "abierto":
            s[:, 0, 0] = 1.0
        elif device == "cortocircuito":
            s[:, 0, 0] = -1.0
        elif device == "carga":
            s[:, 0, 0] = 0.003
        return s

    def single_sweep(self) -> None:
        self._require_connection()
        frequencies = self._sweep_frequencies()
        data = self._device_s(frequencies)
        # noise floor (dBm) referred to the source power → relative noise on the S-parameters
        noise_db = self.noise_floor_db_at_10hz + 10 * np.log10(self.if_bandwidth / 10) - self.power
        sigma = 10 ** (noise_db / 20) / np.sqrt(2)
        data = data + sigma * (self._rng.standard_normal(data.shape) + 1j * self._rng.standard_normal(data.shape))
        self._frequencies, self._data = frequencies, data
        self._record("barrido_unico", result=f"{len(frequencies)} puntos, dispositivo: {self._device_label}")

    def frequencies(self) -> np.ndarray:
        if self._frequencies is None:
            self._fail("leer_frecuencias", "no hay ningún barrido realizado")
        self._record("leer_frecuencias", result=f"{len(self._frequencies)} valores")
        return self._frequencies.copy()

    def read_s_parameter(self, name: str) -> np.ndarray:
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

    def calibration_active(self) -> bool:
        self._record("estado_calibracion", result="activa (simulación)")
        return True
