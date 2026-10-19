"""Frequency ranges and frequency-coverage analysis of a set of equipment.

The effective range of a hardware setup is the intersection of the ranges of
every equipment that takes part in the measurement. If that intersection does
not contain the required range, the setup must raise an alarm that names the
equipment limiting each edge.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from functools import reduce
from typing import Any, Iterable

from calelektra.core.units import UnitError, format_frequency, parse_frequency

_REL_TOL = 1e-9


def _same(a: float, b: float) -> bool:
    return math.isclose(a, b, rel_tol=_REL_TOL, abs_tol=1e-9)


def _edge(hz: float, decimal: str = ",") -> str:
    return "DC" if hz == 0 else format_frequency(hz, decimal=decimal)


@dataclass(frozen=True)
class FrequencyRange:
    start: float
    stop: float

    def __post_init__(self) -> None:
        if self.start < 0 or self.stop < self.start:
            raise ValueError(
                f"Rango de frecuencia no válido: {format_frequency(self.start)} – {format_frequency(self.stop)}"
            )

    @classmethod
    def parse(cls, value: Any) -> "FrequencyRange":
        """Build a range from ``[start, stop]``, ``{inicio, fin}`` or ``"start – stop"``."""
        if isinstance(value, FrequencyRange):
            return value
        if isinstance(value, dict):
            start = value.get("inicio", value.get("start"))
            stop = value.get("fin", value.get("stop"))
        elif isinstance(value, (list, tuple)) and len(value) == 2:
            start, stop = value
        elif isinstance(value, str):
            for separator in ("–", ";", " - ", " a "):
                if separator in value:
                    start, stop = value.split(separator, 1)
                    break
            else:
                raise UnitError(f"Rango de frecuencia no válido: {value!r}")
        else:
            raise UnitError(f"Rango de frecuencia no válido: {value!r}")
        if start is None or stop is None:
            raise UnitError(f"Rango de frecuencia incompleto: {value!r}")
        return cls(parse_frequency(start), parse_frequency(stop))

    def intersect(self, other: "FrequencyRange") -> "FrequencyRange | None":
        start, stop = max(self.start, other.start), min(self.stop, other.stop)
        if start > stop and not _same(start, stop):
            return None
        return FrequencyRange(start, max(start, stop))

    def contains(self, other: "FrequencyRange") -> bool:
        return (self.start < other.start or _same(self.start, other.start)) and (
            self.stop > other.stop or _same(self.stop, other.stop)
        )

    def contains_frequency(self, hz: float) -> bool:
        return self.contains(FrequencyRange(hz, hz))

    def to_yaml(self) -> list[str]:
        return [_edge(self.start, "."), _edge(self.stop, ".")]

    def __str__(self) -> str:
        return f"{_edge(self.start)} – {_edge(self.stop)}"


@dataclass(frozen=True)
class RangeContribution:
    """Frequency range that one assigned equipment brings to a setup."""

    role: str
    role_label: str
    equipment_id: str
    range: FrequencyRange
    sources: tuple[str, ...] = ()
    is_dut: bool = False

    @property
    def label(self) -> str:
        return f"{self.role_label} · {self.equipment_id}"


@dataclass
class CoverageResult:
    required: FrequencyRange
    contributions: list[RangeContribution]
    effective: FrequencyRange | None
    lower_limiting: list[RangeContribution] = field(default_factory=list)
    upper_limiting: list[RangeContribution] = field(default_factory=list)
    uncovered: list[FrequencyRange] = field(default_factory=list)
    dut_outside: list[RangeContribution] = field(default_factory=list)

    @property
    def covered(self) -> bool:
        return self.effective is not None and self.effective.contains(self.required)


def intersect_all(ranges: Iterable[FrequencyRange]) -> FrequencyRange | None:
    """Intersection of every range; ``None`` if it is empty or nothing is given."""
    ranges = list(ranges)
    if not ranges:
        return None
    return reduce(lambda acc, r: acc.intersect(r) if acc is not None else None, ranges[1:], ranges[0])


def analyze_coverage(required: FrequencyRange, contributions: Iterable[RangeContribution]) -> CoverageResult:
    """Compare the required range with the intersection of the measuring equipment.

    DUT contributions do not restrict the effective range (the DUT is what is
    being calibrated), but they are reported when the requirement goes beyond them.
    """
    contributions = list(contributions)
    measuring = [c for c in contributions if not c.is_dut]
    result = CoverageResult(required=required, contributions=contributions, effective=None)
    result.dut_outside = [c for c in contributions if c.is_dut and not c.range.contains(required)]
    if not measuring:
        result.uncovered = [required]
        return result

    max_start = max(c.range.start for c in measuring)
    min_stop = min(c.range.stop for c in measuring)
    result.lower_limiting = [c for c in measuring if _same(c.range.start, max_start)]
    result.upper_limiting = [c for c in measuring if _same(c.range.stop, min_stop)]
    result.effective = intersect_all(c.range for c in measuring)

    if result.effective is None or result.effective.intersect(required) is None:
        result.uncovered = [required]
        return result
    if result.effective.start > required.start and not _same(result.effective.start, required.start):
        result.uncovered.append(FrequencyRange(required.start, result.effective.start))
    if result.effective.stop < required.stop and not _same(result.effective.stop, required.stop):
        result.uncovered.append(FrequencyRange(result.effective.stop, required.stop))
    return result
