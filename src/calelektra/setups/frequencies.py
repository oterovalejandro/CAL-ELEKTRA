"""Frequency points of a hardware setup, from its frequency definition and required range.

* lineal: by number of points, or by frequency step;
* logarítmico: by number of points;
* lista: CSV file with the frequencies (first column; Hz unless the value or
  the header gives the unit, e.g. ``Frecuencia (GHz)`` or ``1,5 GHz``). Only the
  points inside the required range are measured.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from calelektra.core.frequency import FrequencyRange
from calelektra.core.units import UnitError, format_frequency, parse_frequency
from calelektra.setups.model import SWEEP_MODES, FrequencySettings

HEADER_UNITS = {"thz": 1e12, "ghz": 1e9, "mhz": 1e6, "khz": 1e3, "hz": 1.0}


class FrequencyError(ValueError):
    pass


@dataclass
class FrequencyPlan:
    frequencies: np.ndarray
    mode: str
    note: str = ""
    list_path: Path | None = None

    @property
    def start(self) -> float:
        return float(self.frequencies[0])

    @property
    def stop(self) -> float:
        return float(self.frequencies[-1])

    @property
    def points(self) -> int:
        return len(self.frequencies)

    @property
    def range(self) -> FrequencyRange:
        return FrequencyRange(self.start, self.stop)

    @property
    def step(self) -> float | None:
        """Step of a linear sweep."""
        return float(self.frequencies[1] - self.frequencies[0]) if self.mode == "lineal" and self.points > 1 else None

    def describe(self) -> str:
        text = f"{SWEEP_MODES[self.mode]}: {format_frequency(self.start)} – {format_frequency(self.stop)}, {self.points} puntos"
        if self.step is not None:
            text += f" (paso {format_frequency(self.step)})"
        if self.list_path is not None:
            text += f" — {self.list_path.name}"
        return text + (f". {self.note}" if self.note else "")


def _header_unit(line: str) -> float | None:
    lowered = line.lower()
    for unit, factor in HEADER_UNITS.items():
        if re.search(rf"\b{unit}\b", lowered):
            return factor
    return None


def read_frequency_list(path: Path) -> np.ndarray:
    """Frequencies (Hz) of a CSV list: first column, sorted, without repeated values."""
    try:
        text = Path(path).read_text(encoding="utf-8-sig", errors="replace")
    except OSError as exc:
        raise FrequencyError(f"No se puede leer la lista de frecuencias {Path(path).name}: {exc}") from exc
    unit = 1.0
    values: list[float] = []
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        first = re.split(r"[;\t]", line)[0].strip().strip('"')
        try:
            value = parse_frequency(first)
        except UnitError:
            token = first.split(",")[0].strip()
            try:
                value = parse_frequency(token)
            except UnitError:
                if not values:  # header line, may give the unit
                    unit = _header_unit(line) or unit
                    continue
                raise FrequencyError(f"{Path(path).name}, línea {number}: «{first}» no es una frecuencia") from None
        has_unit = bool(re.search(r"[a-zA-Z]", first.split(",")[0])) and first.strip().upper() != "DC"
        values.append(value if has_unit else value * unit)
    if not values:
        raise FrequencyError(f"{Path(path).name}: no contiene frecuencias")
    frequencies = np.unique(np.asarray(values, dtype=float))
    if frequencies[0] <= 0:
        raise FrequencyError(f"{Path(path).name}: las frecuencias deben ser positivas")
    return frequencies


def plan_frequencies(settings: FrequencySettings, required: FrequencyRange, list_path: Path | None = None) -> FrequencyPlan:
    """Frequency points of a setup: its frequency definition inside its required range.

    ``list_path`` is the resolved path of the CSV list (``Library.setup_list_path``).
    """
    if settings.mode == "lista":
        path = Path(list_path) if list_path is not None else Path(settings.list_file or "")
        listed = read_frequency_list(path)
        low, high = required.start * (1 - 1e-12), required.stop * (1 + 1e-12)
        frequencies = listed[(listed >= low) & (listed <= high)]
        if not len(frequencies):
            raise FrequencyError(f"Ninguna frecuencia de la lista {path.name} está dentro del rango requerido ({required})")
        outside = len(listed) - len(frequencies)
        note = f"{outside} frecuencias de la lista quedan fuera del rango requerido ({required}) y no se miden" if outside else ""
        return FrequencyPlan(frequencies, "lista", note=note, list_path=path)
    start, stop = required.start, required.stop
    if stop <= start:
        raise FrequencyError(f"El rango requerido ({required}) no tiene anchura")
    if settings.mode == "logaritmico":
        if start <= 0:
            raise FrequencyError("El barrido logarítmico no puede empezar en DC")
        return FrequencyPlan(np.geomspace(start, stop, settings.points), "logaritmico")
    if settings.by_step:
        step = float(settings.step or 0)
        count = int(math.floor((stop - start) / step + 1e-9)) + 1
        if count < 2:
            raise FrequencyError(f"El paso de {format_frequency(step)} es mayor que el rango requerido ({required})")
        frequencies = start + step * np.arange(count)
        note = ""
        if abs(frequencies[-1] - stop) > 1e-6 * step:
            note = f"El último punto es {format_frequency(frequencies[-1])}: el rango no es múltiplo del paso"
        return FrequencyPlan(frequencies, "lineal", note=note)
    return FrequencyPlan(np.linspace(start, stop, settings.points), "lineal")
