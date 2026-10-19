"""High-level instrument interfaces.

The measurement engine and the GUI only talk to these interfaces. Each real
instrument gets an adapter that implements them on top of the manufacturer's
driver commands (for the R&S ZNB43, its SCPI command set / R&S driver); the
dummy VNA implements them in software.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import numpy as np

from calelektra.core.frequency import FrequencyRange


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


SWEEP_TYPES = ("lineal", "logaritmico")


class VectorNetworkAnalyzer(Instrument):
    """Operations of the ``vna`` category (see catalogo/categorias/vna.yaml)."""

    frequency_range: FrequencyRange
    port_count: int

    @abstractmethod
    def preset(self) -> None: ...

    @abstractmethod
    def configure_sweep(self, start: float, stop: float, points: int, sweep_type: str = "lineal") -> None: ...

    @abstractmethod
    def set_if_bandwidth(self, hz: float) -> None: ...

    @abstractmethod
    def set_power(self, dbm: float) -> None: ...

    @abstractmethod
    def single_sweep(self) -> None:
        """Run one sweep and wait until it finishes."""

    @abstractmethod
    def frequencies(self) -> np.ndarray: ...

    @abstractmethod
    def read_s_parameter(self, name: str) -> np.ndarray:
        """Complex trace of ``"S11"``, ``"S21"``… from the last sweep."""

    @abstractmethod
    def calibration_active(self) -> bool: ...
