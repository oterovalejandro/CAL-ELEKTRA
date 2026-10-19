"""Parsing and formatting of frequency values.

Accepted inputs: plain numbers (Hz) or strings such as ``"100 kHz"``,
``"43.5 GHz"``, ``"43,5 GHz"``, ``"18G"``, ``"10M"``, ``"5 Hz"`` or ``"DC"``.
"""

from __future__ import annotations

import math
import re

_FREQUENCY_MULTIPLIERS = {
    "": 1.0,
    "hz": 1.0,
    "k": 1e3,
    "khz": 1e3,
    "m": 1e6,  # only reached for "M"/"MHz"; lowercase "m" alone is rejected below
    "mhz": 1e6,
    "g": 1e9,
    "ghz": 1e9,
    "t": 1e12,
    "thz": 1e12,
}

_FREQUENCY_PATTERN = re.compile(
    r"^\s*(?P<number>[-+]?\d+(?:[.,]\d*)?(?:[eE][-+]?\d+)?)\s*(?P<unit>[a-zA-Z]*)\s*$"
)


class UnitError(ValueError):
    """Raised when a value cannot be interpreted with the expected unit."""


def parse_frequency(value: str | float | int) -> float:
    """Return the frequency in Hz represented by ``value``."""
    if isinstance(value, bool):
        raise UnitError(f"Frecuencia no válida: {value!r}")
    if isinstance(value, (int, float)):
        hz = float(value)
    elif isinstance(value, str):
        text = value.strip()
        if text.upper() == "DC":
            return 0.0
        match = _FREQUENCY_PATTERN.match(text)
        if not match:
            raise UnitError(f"Frecuencia no válida: {value!r}")
        unit = match.group("unit")
        if unit == "m":
            raise UnitError(f"Unidad ambigua en {value!r}: use 'M' o 'MHz' para megahercios")
        key = unit.lower()
        if key not in _FREQUENCY_MULTIPLIERS:
            raise UnitError(f"Unidad de frecuencia desconocida en {value!r}")
        hz = float(match.group("number").replace(",", ".")) * _FREQUENCY_MULTIPLIERS[key]
    else:
        raise UnitError(f"Frecuencia no válida: {value!r}")
    if not math.isfinite(hz) or hz < 0:
        raise UnitError(f"Frecuencia fuera de rango: {value!r}")
    return hz


def format_frequency(hz: float, significant: int = 6, decimal: str = ",") -> str:
    """Format ``hz`` with the most readable unit (``"43,5 GHz"``)."""
    if hz == 0:
        return "0 Hz"
    for unit, factor in (("GHz", 1e9), ("MHz", 1e6), ("kHz", 1e3)):
        if abs(hz) >= factor:
            break
    else:
        unit, factor = "Hz", 1.0
    number = f"{hz / factor:.{significant}g}"
    if "e" in number:
        number = f"{hz / factor:.3f}".rstrip("0").rstrip(".")
    return f"{number.replace('.', decimal)} {unit}"
