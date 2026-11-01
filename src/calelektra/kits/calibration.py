"""The calibration of a kit: the real values of its standards, their deviations from their definition and
the uncertainty that follows from them.

When the calibration of a unit of the kit is entered («Calibración del kit…», tab «Patrones» of the
unit), each standard gets its real values: a file of its certificate, ``.sdatb`` (with its uncertainty)
or Touchstone (without it). They are compared with the definition of the standard, the model of Keysight
(``kits/agilent.py``), at the frequencies of the file and in the quantities of its electrical
specifications (``SPEC_COLUMNS``):

* open and short, and the transmission of a through (S21 and S12): δM = 20·log10(|S|/|S₀|) dB and
  δφ = ∠(S/S₀) in degrees (−180° … 180°);
* load, and the reflections of a through (S11 and S22): the distance r = |S − S₀|, shown as a return
  loss, 20·log10 r dB, as the specifications write it (−40 dB is r = 0,01).

U is the expanded uncertainty (k = 2) of each deviation, from that of the real values: a Touchstone has
none (U = 0). U(δM) = 20/ln10 · U(|S|)/|S|, U(δφ) = U(∠S) and U(r) = k·√(gᵀ·C·g), with C the covariance
of the real and imaginary parts of S and g the unit vector of S − S₀ (the radial part).

* **Verdict** against the specification L of the manufacturer (that of the model of the kit, k = 2): it
  meets it where |δ| + U ≤ L, it does not where |δ| − U > L and in between it is undetermined. A
  specification absent or null (0, e.g. the magnitude of an open when the datasheet only gives the
  phase) gives no verdict. The verdict of a standard is the worst of its quantities.
* **Own specifications** of the standard in the unit: in each band of frequencies (k·w, (k+1)·w], the
  largest |δ| + U, rounded up (4 significant figures; the return loss to 0,01 dB toward 0), in two rows,
  at the first and the last frequency of the file in the band; without bands, one value for all the
  frequencies. They are used as those of the manufacturer (k = 2, ``kits/specs.py``): the uncertainty of
  the standard covers its deviation from its definition.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from calelektra.core.units import format_frequency
from calelektra.kits.agilent import model_s
from calelektra.kits.model import (
    SPEC_COLUMNS,
    KitError,
    KitStandard,
    SpecRow,
    StandardDefinition,
)
from calelektra.kits.specs import spec_values
from calelektra.sdata.model import SParamData

COVERAGE = 2.0
FLOOR = 1e-6  # distance of a deviation of 0 (−120 dB)
ORDER = ("cumple", "indeterminado", "no_cumple")  # from the best to the worst
QUANTITY_NAMES = {
    "modulo_db": "desviación de módulo",
    "fase_grados": "desviación de fase",
    "perdidas_retorno_db": "desviación |S − S₀|",
    "transmision_modulo_db": "desviación de módulo",
    "transmision_fase_grados": "desviación de fase",
}
UNITS = {"modulo_db": "dB", "fase_grados": "°", "perdidas_retorno_db": "dB", "transmision_modulo_db": "dB", "transmision_fase_grados": "°"}
_TYPE_WORDS = {
    "OPEN": "abierto", "ABIERTO": "abierto",
    "SHORT": "cortocircuito", "CORTOCIRCUITO": "cortocircuito", "CORTO": "cortocircuito",
    "LOAD": "carga", "CARGA": "carga", "MATCH": "carga",
    "THRU": "through", "THROUGH": "through",
}  # fmt: skip
_GENDER_WORDS = {"M": "macho", "MALE": "macho", "MACHO": "macho", "F": "hembra", "H": "hembra", "FEMALE": "hembra", "HEMBRA": "hembra"}
_GENDER = r"(MALE|FEMALE|MACHO|HEMBRA|M|F|H)"
_NAME = re.compile(rf"^({'|'.join(sorted(_TYPE_WORDS, key=len, reverse=True))})[\s_()-]*(?:{_GENDER}(?![A-Z]))?(?:[\s_()-]*{_GENDER}(?![A-Z]))?")


def _parameters(kind: str, column: str) -> tuple[tuple[int, int], ...]:
    """(receiver, source) of the parameters of a quantity."""
    if column.startswith("transmision"):
        return ((1, 0), (0, 1))
    if column == "perdidas_retorno_db" and kind == "through":
        return ((0, 0), (1, 1))
    return ((0, 0),)


@dataclass
class QuantityDeviation:
    """A quantity of a parameter of a standard at each frequency of its real values."""

    column: str  # of the specifications: modulo_db, fase_grados, perdidas_retorno_db…
    parameter: str  # S11, S21…
    deviation: np.ndarray  # δM (dB), δφ (°) or the distance r = |S − S₀|
    uncertainty: np.ndarray  # U (k = 2) of the deviation, in its units
    limit: np.ndarray | None  # specification of the manufacturer (dB, °, or the distance 10^(RL/20)); None: not specified

    @property
    def distance(self) -> bool:
        return self.column == "perdidas_retorno_db"

    @property
    def name(self) -> str:
        return f"{self.parameter}, {QUANTITY_NAMES[self.column]}"

    @property
    def bound(self) -> np.ndarray:
        """|δ| + U: what the specification must cover."""
        return np.abs(self.deviation) + self.uncertainty

    def verdicts(self) -> np.ndarray:
        """At each frequency: ``cumple``, ``no_cumple``, ``indeterminado`` or ``sin_especificacion``."""
        if self.limit is None:
            return np.full(len(self.deviation), "sin_especificacion", dtype=object)
        result = np.full(len(self.deviation), "indeterminado", dtype=object)
        result[self.bound <= self.limit] = "cumple"
        result[np.abs(self.deviation) - self.uncertainty > self.limit] = "no_cumple"
        return result

    @property
    def verdict(self) -> str:
        if self.limit is None:
            return "sin_especificacion"
        found = set(self.verdicts())
        return next(v for v in reversed(ORDER) if v in found)

    @property
    def worst(self) -> int:
        """The frequency (index) with the largest deviation, or with the largest excess over the specification."""
        if self.limit is None:
            return int(np.argmax(self.bound))
        return int(np.argmax(self.bound - self.limit))

    def shown(self, values: np.ndarray | float) -> np.ndarray:
        """In the units of the specifications: the distance as a return loss (dB)."""
        values = np.asarray(values, float)
        return 20 * np.log10(np.maximum(np.abs(values), FLOOR)) if self.distance else values

    def value_text(self, value: float, signed: bool = True) -> str:
        """A value of the quantity as the specifications write it: «−14,114°», «+0,025 dB», «−35,01 dB»."""
        shown = float(self.shown(value))
        if self.distance:
            text = f"{shown:.2f} dB"
        else:
            text = (f"{shown:+.3f}" if signed else f"{shown:.3f}") + ("°" if UNITS[self.column] == "°" else " dB")
        return text.replace(".", ",").replace("-", "−")


@dataclass
class StandardDeviations:
    """The real values of a standard against its definition, quantity by quantity."""

    standard: KitStandard
    frequencies: np.ndarray
    real: np.ndarray  # (frequencies, receiver, source)
    nominal: np.ndarray  # its definition at the same frequencies
    quantities: list[QuantityDeviation]

    @property
    def verdict(self) -> str:
        """The worst of its quantities with a specification; ``sin_especificacion`` if none has one."""
        found = {q.verdict for q in self.quantities} - {"sin_especificacion"}
        return next((v for v in reversed(ORDER) if v in found), "sin_especificacion")

    @property
    def uncertain(self) -> bool:
        """The real values have an uncertainty."""
        return any(np.any(q.uncertainty > 0) for q in self.quantities)

    def summary(self) -> list[str]:
        """A line per quantity: its largest deviation, where, and against the specification of the manufacturer."""
        lines = []
        for q in self.quantities:
            k = q.worst
            text = f"{q.name}: {q.value_text(q.deviation[k])} a {format_frequency(self.frequencies[k])}"
            if q.limit is not None:
                limit = q.value_text(q.limit[k], signed=False)
                text += f" (especificación {'±' if not q.distance else ''}{limit})"
            lines.append(text)
        return lines

    def specifications(self, band_width: float | None) -> list[SpecRow]:
        """The own specifications of the standard (k = 2): by bands, the largest |δ| + U of each quantity."""
        f = self.frequencies
        bands = np.ceil(f / band_width * (1 - 1e-12)).astype(np.int64) if band_width else np.zeros(len(f), np.int64)
        rows: list[SpecRow] = []
        for band in np.unique(bands):
            points = np.flatnonzero(bands == band)
            values = {}
            for column in SPEC_COLUMNS[self.standard.type]:
                top = max(float(q.bound[points].max()) for q in self.quantities if q.column == column)
                values[column] = _return_loss_up(top) if column == "perdidas_retorno_db" else _round_up(top)
            first, last = f[points[0]], f[points[-1]]
            rows.append(SpecRow.model_validate({"frecuencia": first, **values}))
            if last > first:
                rows.append(SpecRow.model_validate({"frecuencia": last, **values}))
        return rows


def _round_up(value: float, digits: int = 4) -> float:
    """``value`` rounded up to ``digits`` significant figures."""
    if value <= 0 or not math.isfinite(value):
        return 0.0
    exponent = math.floor(math.log10(value)) - digits + 1
    scaled = math.ceil(value / 10**exponent - 1e-9)
    return round(scaled * 10**exponent, max(0, -exponent))


def _return_loss_up(distance: float) -> float:
    """The return loss (dB) of a distance, rounded to 0,01 dB toward 0 (a larger distance)."""
    return math.ceil(20 * math.log10(max(distance, FLOOR)) * 100 - 1e-9) / 100


def _uncertainty(data: SParamData, points: np.ndarray, receiver: int, source: int, column: str, nominal: np.ndarray) -> np.ndarray:
    """U (k = 2) of the deviation of a quantity, from the covariance of the real values at each frequency."""
    result = np.zeros(len(points))
    if not data.vector.inputs:
        return result
    for i, k in enumerate(points):
        cov = data.vector.covariance([data.element(k, receiver, source, 0), data.element(k, receiver, source, 1)])
        value = complex(data.s[k, receiver, source])
        if column == "perdidas_retorno_db":
            d = value - complex(nominal[i])
            if abs(d) > 0:
                g = np.array([d.real, d.imag]) / abs(d)
                variance = float(g @ cov @ g)
            else:
                variance = float(cov[0, 0] + cov[1, 1]) / 2
        elif abs(value) == 0:
            variance = math.nan
        elif "fase" in column:
            h = np.array([-value.imag, value.real]) / abs(value) ** 2
            variance = float(h @ cov @ h) * (180 / math.pi) ** 2
        else:
            g = np.array([value.real, value.imag]) / abs(value)
            variance = float(g @ cov @ g) * (20 / math.log(10) / abs(value)) ** 2
        result[i] = COVERAGE * math.sqrt(max(variance, 0.0))
    return result


def standard_deviations(
    standard: KitStandard,
    data: SParamData,
    zr: complex = 50.0,
    definition: StandardDefinition | None = None,
    limits: list[SpecRow] | None = None,
) -> StandardDeviations:
    """The deviations of the real values ``data`` of a standard from its definition (by the model of Keysight),
    with ``limits`` the specifications of the manufacturer (by default, those of the standard)."""
    definition = definition or standard.definition
    label = standard.label or standard.id
    if definition.kind != "modelo":
        raise KitError(f"«{label}»: las desviaciones se calculan frente a su definición por el modelo de Keysight")
    if data.n_ports != standard.ports:
        raise KitError(f"«{label}» es de {standard.ports} puerto{'s' if standard.ports > 1 else ''} y sus valores reales de {data.n_ports}")
    points = np.flatnonzero(data.frequencies > 0)  # the model is not defined at DC
    if not len(points):
        raise KitError(f"«{label}»: los valores reales no tienen frecuencias por encima de DC")
    f = data.frequencies[points]
    real = data.s[points]
    nominal = model_s(standard, f, zr, definition)
    limits = standard.specifications if limits is None else limits
    quantities = []
    for column in SPEC_COLUMNS[standard.type]:
        limit = spec_values(limits, column, f)
        if limit is not None and column == "perdidas_retorno_db":
            limit = 10 ** (-np.abs(limit) / 20)
        elif limit is not None and not np.any(limit > 0):
            limit = None  # a null specification: not specified
        for receiver, source in _parameters(standard.type, column):
            s, s0 = real[:, receiver, source], nominal[:, receiver, source]
            with np.errstate(divide="ignore", invalid="ignore"):
                if column == "perdidas_retorno_db":
                    deviation = np.abs(s - s0)
                elif "fase" in column:
                    deviation = np.degrees(np.angle(s / s0))
                else:
                    deviation = 20 * np.log10(np.abs(s) / np.abs(s0))
            if not np.all(np.isfinite(deviation)):
                raise KitError(f"«{label}»: S{receiver + 1}{source + 1} o su definición valen 0: su {QUANTITY_NAMES[column]} no está definida")
            uncertainty = _uncertainty(data, points, receiver, source, column, s0)
            quantities.append(QuantityDeviation(column, f"S{receiver + 1}{source + 1}", deviation, uncertainty, limit))
    return StandardDeviations(standard, f, real, nominal, quantities)


def match_standard(name: str, standards: list[KitStandard]) -> KitStandard | None:
    """The standard a file of real values is for, by its name: «OPENM resultados.s1p» is the male open.

    The name starts with the type (OPEN, SHORT, LOAD, THRU or ABIERTO, CORTOCIRCUITO, CARGA…) and its
    genders (M, MALE, MACHO; F, H, FEMALE, HEMBRA), or contains the identifier or the label of the
    standard. None if it is not one and only one standard."""
    stem = Path(name).stem.upper()
    match = _NAME.match(stem.strip())
    if match:
        kind = _TYPE_WORDS[match.group(1)]
        genders = [_GENDER_WORDS[g] for g in match.groups()[1:] if g]
        candidates = [s for s in standards if s.type == kind]
        if genders:
            candidates = [s for s in candidates if s.genders[: len(genders)] == genders or sorted(s.genders) == sorted(genders)]
        if len(candidates) == 1:
            return candidates[0]
    compact = re.sub(r"[^A-Z0-9]", "", stem)
    found = [s for s in standards if any(key and key in compact for key in (re.sub(r"[^A-Z0-9]", "", s.id.upper()), re.sub(r"[^A-Z0-9]", "", s.label.upper())))]
    return found[0] if len(found) == 1 else None
