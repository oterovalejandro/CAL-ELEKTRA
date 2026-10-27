"""A value that changes with the frequency, by bands (e.g. the VSWR of a connector in its datasheet).

In YAML it is a number (the same at every frequency) or a list of bands,
each up to a frequency, in increasing order; the last one may have no upper
frequency (up to any frequency)::

    vswr_conector_a: 1.15
    vswr_conector_a:
      - { hasta: 18 GHz, valor: 1.12 }      # up to 18 GHz, 18 GHz included
      - { hasta: 26.5 GHz, valor: 1.16 }    # from 18 GHz to 26.5 GHz

In the GUI the same is written ``1,12 hasta 18 GHz; 1,16 hasta 26,5 GHz``.
Above the last upper frequency there is no value (NaN).
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import pairwise
from typing import Any

import numpy as np

from calelektra.core.units import UnitError, format_frequency, parse_frequency

Band = tuple[float | None, float]  # (upper frequency in Hz or None, value)


def _number(value: Any) -> float:
    if isinstance(value, bool):
        raise ValueError("se esperaba un número")
    try:
        return float(str(value).replace(",", ".")) if isinstance(value, str) else float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"«{value}» no es un número") from exc


def _text(value: float) -> str:
    return f"{value:g}".replace(".", ",")


@dataclass(frozen=True)
class BandValues:
    bands: tuple[Band, ...]

    @classmethod
    def single(cls, value: float) -> BandValues:
        return cls(((None, float(value)),))

    @classmethod
    def parse(cls, value: Any) -> BandValues:
        """From a number, a list of ``{hasta, valor}`` or the text ``1,12 hasta 18 GHz; 1,16 hasta 26,5 GHz``."""
        if isinstance(value, BandValues):
            return value
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return cls.single(value)
        if isinstance(value, str):
            items: list[Any] = []
            for part in value.replace("\n", ";").split(";"):
                number, separator, upper = part.strip().partition("hasta")
                if number.strip() or separator:
                    items.append({"valor": number.strip(), "hasta": upper.strip() if separator else None})
            value = items
        if not isinstance(value, (list, tuple)) or not value:
            raise ValueError(f"valor por bandas no válido: {value!r}")
        bands: list[Band] = []
        for item in value:
            if not isinstance(item, dict) or "valor" not in item:
                raise ValueError("cada banda lleva «valor» y, salvo la última, «hasta» (la frecuencia donde acaba)")
            upper = item.get("hasta")
            try:
                frequency = parse_frequency(upper) if upper not in (None, "") else None
            except UnitError as exc:
                raise ValueError(str(exc)) from exc
            bands.append((frequency, _number(item["valor"])))
        uppers = [upper for upper, _ in bands]
        if any(upper is None for upper in uppers[:-1]):
            raise ValueError("solo la última banda puede ir sin frecuencia «hasta»")
        limits = [upper for upper in uppers if upper is not None]
        if any(b <= a for a, b in pairwise(limits)):
            raise ValueError("las bandas van en orden de frecuencia creciente")
        return cls(tuple(bands))

    @property
    def values(self) -> list[float]:
        return [value for _, value in self.bands]

    @property
    def is_single(self) -> bool:
        return len(self.bands) == 1 and self.bands[0][0] is None

    @property
    def limits(self) -> list[float]:
        """Upper frequencies of the bands (the edges where the value changes, and the last one)."""
        return [upper for upper, _ in self.bands if upper is not None]

    def at(self, frequencies: Any) -> np.ndarray:
        """The value at each frequency: that of the first band that reaches it (NaN above the last)."""
        f = np.asarray(frequencies, dtype=float)
        out = np.full(f.shape, np.nan)
        lower = -np.inf
        for upper, value in self.bands:
            high = np.inf if upper is None else upper * (1 + 1e-12)
            out[(f > lower) & (f <= high) & np.isnan(out)] = value
            lower = high
        return out

    def map(self, function) -> BandValues:
        return BandValues(tuple((upper, float(function(value))) for upper, value in self.bands))

    def to_yaml(self) -> Any:
        if self.is_single:
            return self.bands[0][1]
        return [
            ({"hasta": format_frequency(upper, decimal=".")} if upper is not None else {}) | {"valor": value}
            for upper, value in self.bands
        ]

    def __str__(self) -> str:
        if self.is_single:
            return _text(self.bands[0][1])
        return "; ".join(_text(value) + (f" hasta {format_frequency(upper)}" if upper is not None else "") for upper, value in self.bands)


def merge_limits(*items: BandValues | None) -> list[float]:
    """The frequencies where any of the values changes (sorted, without repetitions)."""
    return sorted({upper for item in items if item is not None for upper in item.limits})
