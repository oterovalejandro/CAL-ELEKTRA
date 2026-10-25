"""Uncertainty of whole traces, point by point, as the laboratory processing does.

For each S-parameter trace and each frequency point (``processing.py`` of the
laboratory):

- magnitude: the value in dB is both the measured value and the level that
  selects the ranges of the file; the result is the expanded uncertainty in dB;
- phase: the measured phase in degrees, with the magnitude in dB as level; the
  result is the expanded uncertainty in degrees.

A point gets NaN when its value is not finite, when it is outside the ranges of
the file (the engine does not extrapolate) or when the engine fails. The first
problem of each trace is kept to explain the gaps.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable

import numpy as np

from calelektra.core.units import format_frequency
from calelektra.uncertainty.engine import UncertaintyCalculationError, UncertaintyEngine

PARAMETERS = ("S11", "S21", "S12", "S22")


@dataclass
class UncertaintyOptions:
    coverage_factor: float = 2.0
    trace_noise: bool = True
    temperature_stability: bool = True
    temperature_c: tuple[float, float] | None = None  # minimum and maximum ambient temperature of the measurement


@dataclass
class TraceUncertainty:
    """Expanded uncertainty of one trace: magnitude in dB and phase in degrees (NaN where there is none)."""

    parameter: str
    magnitude_db: np.ndarray
    phase_deg: np.ndarray
    if_bandwidth: float | None = None
    problems: list[str] = field(default_factory=list)

    @property
    def missing(self) -> int:
        """Points without uncertainty (magnitude or phase)."""
        return int(np.count_nonzero(~(np.isfinite(self.magnitude_db) & np.isfinite(self.phase_deg))))

    def summary(self) -> dict[str, Any]:
        def extremes(values: np.ndarray, digits: int) -> dict[str, float] | None:
            finite = values[np.isfinite(values)]
            if not len(finite):
                return None
            return {"min": round(float(finite.min()), digits), "max": round(float(finite.max()), digits)}

        entry: dict[str, Any] = {}
        if self.if_bandwidth is not None:
            entry["ifbw"] = format_frequency(self.if_bandwidth, decimal=".")
        entry["U_db"] = extremes(self.magnitude_db, 4)
        entry["U_grados"] = extremes(self.phase_deg, 3)
        entry["puntos"] = len(self.magnitude_db)
        if self.missing:
            entry["puntos_sin_incertidumbre"] = self.missing
        if self.problems:
            entry["problemas"] = list(self.problems)
        return entry


def describe_problem(parameter: str, frequency_hz: float, problem: dict[str, Any]) -> str:
    """One line explaining why a point has no uncertainty."""
    where = f"{parameter} a {format_frequency(frequency_hz)}"
    source = f" (fuente {problem['source_id']})" if problem.get("source_id") else ""
    kind = problem.get("type", "")
    if kind == "input_level_out_of_limits":
        family = problem.get("level_limit_family", "")
        value = problem.get("converted_level_value")
        unit = " dB" if family == "magnitude_db" else ""
        ranges = problem.get("allowed_level_ranges") or []
        limits = [v for r in ranges for v in r.values() if v is not None]
        span = f"; el fichero cubre de {min(limits):g} a {max(limits):g}{unit}" if limits else ""
        return f"{where}: nivel {value:.4g}{unit} fuera de los rangos{span}{source}"
    if kind in ("source_frequency_out_of_limits", "equipment_frequency_out_of_limits"):
        return f"{where}: frecuencia fuera de los rangos del fichero{source}"
    return f"{where}: {problem.get('message', kind)}{source}"


def trace_uncertainty(
    engine: UncertaintyEngine,
    equipment: str,
    parameter: str,
    frequencies: np.ndarray,
    values: np.ndarray,
    if_bandwidth: float | None = None,
    settings: UncertaintyOptions | None = None,
    today: date | None = None,
) -> TraceUncertainty:
    """Expanded uncertainty of a complex trace, point by point."""
    settings = settings or UncertaintyOptions()
    temp_min, temp_max = settings.temperature_c or (None, None)
    with np.errstate(divide="ignore"):
        magnitude = 20 * np.log10(np.abs(values))
    phase = np.degrees(np.angle(values))
    u_db = np.full(len(values), np.nan)
    u_deg = np.full(len(values), np.nan)
    problems: dict[str, str] = {}

    def one(frequency: float, value: float, unit: str, level: float | None) -> float:
        try:
            result = engine.calculate(
                equipment=equipment,
                parameter=parameter,
                frequency_hz=float(frequency),
                value=float(value),
                unit=unit,
                temp_min_c=temp_min,
                temp_max_c=temp_max,
                coverage_factor=settings.coverage_factor,
                include_trace_noise=settings.trace_noise,
                include_temperature_stability=settings.temperature_stability,
                if_bandwidth_hz=if_bandwidth,
                output_unit=unit,
                level_value=level,
                level_unit=None if level is None else "dB",
                today=today,
            )
        except (UncertaintyCalculationError, ValueError, TypeError) as exc:
            problems.setdefault(unit, f"{parameter} a {format_frequency(frequency)}: {exc}")
            return float("nan")
        if result.out_of_limits:
            problems.setdefault(unit, describe_problem(parameter, frequency, result.out_of_limits[0]))
        return result.expanded_uncertainty

    for k, frequency in enumerate(frequencies):
        if not np.isfinite(magnitude[k]):
            continue
        u_db[k] = one(frequency, magnitude[k], "dB", None)
        if np.isfinite(phase[k]):
            u_deg[k] = one(frequency, phase[k], "deg", magnitude[k])
    return TraceUncertainty(parameter, u_db, u_deg, if_bandwidth, list(problems.values()))


def dataset_uncertainty(
    engine: UncertaintyEngine,
    equipment: str,
    frequencies: np.ndarray,
    s: np.ndarray,
    if_bandwidth_of: Callable[[str], float | None] | None = None,
    settings: UncertaintyOptions | None = None,
    today: date | None = None,
    parameters: tuple[str, ...] | None = None,
) -> dict[str, TraceUncertainty]:
    """Uncertainty of the S-parameters of a measurement ``s`` (points, n, n), n = 1 or 2 (only ``parameters`` if given)."""
    traces = {}
    n = s.shape[1]
    for i in range(n):
        for j in range(n):
            parameter = f"S{i + 1}{j + 1}"
            if parameter not in PARAMETERS or (parameters is not None and parameter not in parameters):
                continue
            ifbw = if_bandwidth_of(parameter) if if_bandwidth_of else None
            traces[parameter] = trace_uncertainty(engine, equipment, parameter, frequencies, s[:, i, j], ifbw, settings, today)
    return {p: traces[p] for p in PARAMETERS if p in traces}
