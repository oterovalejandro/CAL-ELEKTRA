"""High-level instrument interfaces.

The measurement procedures and the GUI only talk to these interfaces. Each
real instrument has an adapter that implements them on top of the
manufacturer's driver commands (the R&S ZNB43 adapter uses the SCPI command
set of the R&S RsZnx driver); the dummy VNA implements them in software.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Sequence

import numpy as np

from calelektra.core.connectors import Connector
from calelektra.core.frequency import FrequencyRange
from calelektra.core.units import format_frequency


class InstrumentError(RuntimeError):
    """Error reported by an instrument (or its driver) to the high-level layer."""


@dataclass
class CommandLogEntry:
    timestamp: datetime
    operation: str
    arguments: dict[str, Any] = field(default_factory=dict)
    result: str = ""
    error: str | None = None

    def __str__(self) -> str:
        args = ", ".join(f"{k}={v}" for k, v in self.arguments.items())
        text = f"{self.timestamp:%H:%M:%S.%f}"[:-3] + f"  {self.operation}({args})"
        if self.error:
            return f"{text}  ✗ {self.error}"
        return f"{text}  → {self.result}" if self.result else text


class Instrument(ABC):
    """Common behaviour: connection state and a log of every high-level operation."""

    simulated: bool = False

    def __init__(self, name: str):
        self.name = name
        self.connected = False
        self.log: list[CommandLogEntry] = []

    def _record(self, operation: str, result: str = "", error: str | None = None, **arguments: Any) -> None:
        self.log.append(CommandLogEntry(datetime.now(), operation, arguments, result, error))

    def _require_connection(self) -> None:
        if not self.connected:
            raise InstrumentError(f"{self.name}: el equipo no está conectado")

    @abstractmethod
    def connect(self) -> None: ...

    @abstractmethod
    def disconnect(self) -> None: ...

    @abstractmethod
    def identify(self) -> str: ...


SWEEP_TYPES = ("lineal", "logaritmico")  # «lista»: configure_frequency_list()
REFLECTION_STANDARDS = ("OPEN", "SHORT", "MATCH")
TRANSMISSION_STANDARDS = ("THROUGH", "UNKNOWN_THROUGH")
STANDARD_NAMES = {
    "OPEN": "Abierto (Open)",
    "SHORT": "Cortocircuito (Short)",
    "MATCH": "Carga adaptada (Match)",
    "THROUGH": "Through (unión directa)",
    "UNKNOWN_THROUGH": "Through desconocido",
}
# Labels of SENS:CORR:SST? on R&S ZNx ('CAL OFF' → OFF, '' → NONE)
CALIBRATION_STATUS = {
    "CAL": "Calibrado",
    "CAI": "Calibrado (interpolado)",
    "CA?": "Calibración dudosa (revisar en el VNA)",
    "CAV": "Calibración aplicada (CAV, revisar en el VNA)",
    "OFF": "Corrección desactivada",
    "NONE": "Sin calibrar",
}


def calibration_plan(method: str, ports: Sequence[int]) -> list[tuple[str, tuple[int, ...]]]:
    """Standards that a manual calibration acquires, in order."""
    plan: list[tuple[str, tuple[int, ...]]] = []
    if method not in ("OSM", "TOSM", "UOSM"):
        raise InstrumentError(f"Método de calibración no soportado: {method}")
    if method != "OSM" and len(ports) != 2:
        raise InstrumentError(f"{method} necesita exactamente 2 puertos")
    for port in ports:
        for standard in REFLECTION_STANDARDS:
            plan.append((standard, (port,)))
    if method == "TOSM":
        plan.append(("THROUGH", tuple(ports)))
    elif method == "UOSM":
        plan.append(("UNKNOWN_THROUGH", tuple(ports)))
    return plan


class VectorNetworkAnalyzer(Instrument):
    """Operations of the ``vna`` category (see catalogo/categorias/vna.yaml).

    The limits come from the equipment fields (``rango_frecuencia``,
    ``potencia_salida_dbm``, ``ifbw``, ``puntos_max``, ``metodos_calibracion``)
    and are checked here before anything is sent to the instrument.
    """

    frequency_range: FrequencyRange
    port_count: int
    power_range: tuple[float, float] | None = None
    if_bandwidth_range: tuple[float, float] | None = None
    max_points: int | None = None
    calibration_methods: tuple[str, ...] = ("OSM", "TOSM", "UOSM")

    # ── limits from the equipment fields ───────────────────────────────────
    def sweep_problem(self, start: float, stop: float, points: int, sweep_type: str) -> str | None:
        if sweep_type not in SWEEP_TYPES:
            return f"tipo de barrido no soportado: {sweep_type}"
        if stop <= start:
            return "la frecuencia final debe ser mayor que la inicial"
        requested = FrequencyRange(start, stop)
        if not self.frequency_range.contains(requested):
            return f"rango {requested} fuera de las especificaciones del VNA ({self.frequency_range})"
        if points < 2 or (self.max_points is not None and points > self.max_points):
            return f"número de puntos fuera de rango (2 – {self.max_points or '∞'})"
        if sweep_type == "logaritmico" and start <= 0:
            return "el barrido logarítmico no admite 0 Hz"
        return None

    def frequency_list_problem(self, frequencies: Sequence[float]) -> str | None:
        f = np.asarray(frequencies, dtype=float)
        if f.ndim != 1 or not len(f):
            return "la lista de frecuencias está vacía"
        if np.any(np.diff(f) <= 0):
            return "las frecuencias de la lista deben ser crecientes y sin repetir"
        requested = FrequencyRange(float(f[0]), float(f[-1]))
        if not self.frequency_range.contains(requested):
            return f"lista {requested} fuera de las especificaciones del VNA ({self.frequency_range})"
        if self.max_points is not None and len(f) > self.max_points:
            return f"la lista tiene {len(f)} puntos y el VNA admite {self.max_points}"
        return None

    def if_bandwidth_problem(self, hz: float) -> str | None:
        if self.if_bandwidth_range is None:
            return None if hz > 0 else "la IFBW debe ser positiva"
        low, high = self.if_bandwidth_range
        if not low <= hz <= high:
            return f"IFBW fuera de rango ({format_frequency(low)} – {format_frequency(high)})"
        return None

    def power_problem(self, dbm: float) -> str | None:
        if self.power_range is None:
            return None
        low, high = self.power_range
        if not low <= dbm <= high:
            return f"potencia fuera de rango ({low:g} – {high:g} dBm)"
        return None

    def calibration_problem(self, method: str, ports: Sequence[int]) -> str | None:
        if method not in self.calibration_methods:
            return f"el VNA no tiene el método {method} ({', '.join(self.calibration_methods)})"
        if len(set(ports)) != len(ports):
            return "puertos repetidos"
        for port in ports:
            if not 1 <= port <= self.port_count:
                return f"el puerto {port} no existe (el VNA tiene {self.port_count})"
        try:
            calibration_plan(method, ports)
        except InstrumentError as exc:
            return str(exc)
        return None

    # ── sweep ──────────────────────────────────────────────────────────────
    @abstractmethod
    def preset(self) -> None: ...

    @abstractmethod
    def configure_sweep(self, start: float, stop: float, points: int, sweep_type: str = "lineal") -> None: ...

    @abstractmethod
    def configure_frequency_list(self, frequencies: Sequence[float]) -> None:
        """Sweep exactly these frequencies (segmented sweep on the R&S ZNx)."""

    @abstractmethod
    def set_if_bandwidth(self, hz: float) -> None: ...

    @abstractmethod
    def set_power(self, dbm: float) -> None: ...

    @abstractmethod
    def set_averaging(self, count: int) -> None: ...

    @abstractmethod
    def single_sweep(self) -> None:
        """Run one sweep (with the configured averaging) and wait until it finishes."""

    @abstractmethod
    def frequencies(self) -> np.ndarray: ...

    @abstractmethod
    def read_s_parameter(self, name: str) -> np.ndarray:
        """Complex trace of ``"S11"``, ``"S21"``… from the last sweep (corrected if calibrated)."""

    def read_s_matrix(self, ports: Sequence[int]) -> np.ndarray:
        """All S-parameters between ``ports`` as an array ``(points, n, n)``."""
        traces = {(i, j): self.read_s_parameter(f"S{p}{q}") for i, p in enumerate(ports) for j, q in enumerate(ports)}
        points = len(next(iter(traces.values())))
        matrix = np.empty((points, len(ports), len(ports)), dtype=complex)
        for (i, j), trace in traces.items():
            matrix[:, i, j] = trace
        return matrix

    # ── calibration (manual, guided by the software) ───────────────────────
    @abstractmethod
    def begin_calibration(
        self,
        method: str,
        ports: Sequence[int],
        connectors: dict[int, Connector] | None = None,
        kit_name: str | None = None,
        kit_family: str | None = None,
    ) -> None:
        """Define the calibration (type, ports, connectors, cal kit) before acquiring standards."""

    @abstractmethod
    def acquire_standard(self, standard: str, ports: Sequence[int]) -> None:
        """Measure one standard: OPEN/SHORT/MATCH on one port, THROUGH/UNKNOWN_THROUGH between two."""

    @abstractmethod
    def apply_calibration(self) -> None:
        """Compute the error terms from the acquired standards and switch the correction on."""

    @abstractmethod
    def calibration_status(self) -> str:
        """``CAL``, ``CAI`` (interpolated), ``OFF`` or ``NONE``."""

    @abstractmethod
    def set_correction(self, enabled: bool) -> None:
        """Switch the active system error correction on or off."""

    def calibration_active(self) -> bool:
        return self.calibration_status() in ("CAL", "CAI")

    def save_calibration(self, name: str) -> None:
        raise InstrumentError(f"{self.name}: guardar la calibración en el equipo no está soportado")

    def load_calibration(self, name: str) -> None:
        raise InstrumentError(f"{self.name}: cargar una calibración del equipo no está soportado")
