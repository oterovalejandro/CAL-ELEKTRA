"""Pass / fail of a measurement against S2P (S1P) specification files.

The specifications belong to the model of the EUT (``especificaciones_s`` of
the catalogue model). Each one names a Touchstone file, the parameters to
compare and the criterion:

* ``minimo``      medida ≥ especificación   (e.g. |S21| against the maximum insertion loss)
* ``maximo``      medida ≤ especificación   (e.g. |S11| against the return-loss mask)
* ``tolerancia``  |medida − especificación| ≤ tolerancia (reference curve ± tolerance)

in magnitude (dB) or phase (degrees). The specification is interpolated at
the measured frequencies inside its own range; points outside it are not
evaluated. The margin is positive when the point passes.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from calelektra.catalog.model import SpecFile
from calelektra.core.touchstone import Touchstone, TouchstoneError, read_touchstone
from calelektra.core.units import format_frequency

VERDICTS = {"PASA": "Pasa", "FALLA": "Falla", "NO_EVALUADO": "No evaluado", "SIN_ESPECIFICACIONES": "Sin especificaciones"}


@dataclass
class ParameterResult:
    parameter: str
    passed: bool | None  # None: no point could be evaluated
    evaluated: int = 0
    failed: int = 0
    not_evaluated: int = 0
    worst_margin: float | None = None
    worst_frequency: float | None = None
    frequencies: np.ndarray = field(default_factory=lambda: np.empty(0))
    limit: np.ndarray = field(default_factory=lambda: np.empty(0))  # spec values (dB or °) at those frequencies

    def text(self, unit: str) -> str:
        if self.passed is None:
            return f"{self.parameter}: sin puntos comunes con la especificación"
        state = "pasa" if self.passed else f"FALLA en {self.failed} de {self.evaluated} puntos"
        worst = f"; margen mínimo {self.worst_margin:+.3f} {unit} a {format_frequency(self.worst_frequency)}" if self.worst_margin is not None else ""
        extra = f"; {self.not_evaluated} puntos fuera del rango de la especificación" if self.not_evaluated else ""
        return f"{self.parameter}: {state}{worst}{extra}"


@dataclass
class SpecResult:
    spec: SpecFile
    path: Path
    passed: bool | None
    parameters: list[ParameterResult] = field(default_factory=list)
    message: str = ""
    sha256: str = ""

    @property
    def unit(self) -> str:
        return "dB" if self.spec.quantity == "magnitud_db" else "°"

    def to_record(self) -> dict:
        return {
            "nombre": self.spec.name,
            "fichero": self.path.name,
            "sha256": self.sha256,
            "criterio": self.spec.describe(),
            "unidad": self.unit,
            "resultado": _state(self.passed),
            "margen_minimo": min((p.worst_margin for p in self.parameters if p.worst_margin is not None), default=None),
            "parametros": [
                {
                    "parametro": p.parameter,
                    "resultado": _state(p.passed),
                    "margen_minimo": None if p.worst_margin is None else round(p.worst_margin, 4),
                    "frecuencia": None if p.worst_frequency is None else format_frequency(p.worst_frequency, decimal="."),
                    "evaluados": p.evaluated,
                    "fallos": p.failed,
                    "fuera_de_rango": p.not_evaluated,
                }
                for p in self.parameters
            ],
            "detalle": [p.text(self.unit) for p in self.parameters] or [self.message],
        }


def _state(passed: bool | None) -> str:
    return "PASA" if passed else ("FALLA" if passed is False else "NO_EVALUADO")


def _values(trace: np.ndarray, quantity: str) -> np.ndarray:
    if quantity == "magnitud_db":
        with np.errstate(divide="ignore"):
            return 20 * np.log10(np.maximum(np.abs(trace), 1e-15))
    return np.degrees(np.unwrap(np.angle(trace)))


def _index(parameter: str, ports: int) -> tuple[int, int] | None:
    i, j = int(parameter[1]) - 1, int(parameter[2]) - 1
    return (i, j) if 0 <= i < ports and 0 <= j < ports else None


@dataclass
class SpecCurve:
    """Curve of a specification file to draw on the chart."""

    name: str
    parameter: str
    quantity: str  # magnitud_db | fase_grados
    criterion: str
    tolerance: float | None
    frequencies: np.ndarray
    values: np.ndarray  # dB or degrees


def spec_curves(spec: SpecFile, path: Path) -> list[SpecCurve]:
    """Curves of the parameters of a specification file (empty if the file cannot be read)."""
    try:
        data = read_touchstone(path)
    except (TouchstoneError, OSError):
        return []
    curves = []
    for parameter in spec.parameters:
        index = _index(parameter, data.n_ports)
        if index is not None:
            values = _values(data.s[:, index[0], index[1]], spec.quantity)
            curves.append(SpecCurve(spec.name, parameter, spec.quantity, spec.criterion, spec.tolerance, data.frequencies, values))
    return curves


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def evaluate_spec(spec: SpecFile, path: Path, frequencies: np.ndarray, measured: np.ndarray) -> SpecResult:
    """Compare ``measured`` (points, n, n) with one specification file."""
    try:
        data: Touchstone = read_touchstone(path)
        digest = file_hash(path)
    except (TouchstoneError, OSError) as exc:
        return SpecResult(spec, path, None, message=f"No se puede leer la especificación {path.name}: {exc}")
    result = SpecResult(spec, path, None, sha256=digest)
    for parameter in spec.parameters:
        measured_index = _index(parameter, measured.shape[1])
        spec_index = _index(parameter, data.n_ports)
        if measured_index is None or spec_index is None:
            result.parameters.append(ParameterResult(parameter, None))
            continue
        inside = (frequencies >= data.frequencies[0] * (1 - 1e-12)) & (frequencies <= data.frequencies[-1] * (1 + 1e-12))
        f = frequencies[inside]
        if not len(f):
            result.parameters.append(ParameterResult(parameter, None, not_evaluated=len(frequencies)))
            continue
        spec_trace = data.s[:, spec_index[0], spec_index[1]]
        limit = np.interp(f, data.frequencies, _values(spec_trace, spec.quantity))
        value = _values(measured[:, measured_index[0], measured_index[1]], spec.quantity)[inside]
        if spec.quantity == "fase_grados":
            difference = (value - limit + 180.0) % 360.0 - 180.0
        else:
            difference = value - limit
        if spec.criterion == "minimo":
            margin = difference
        elif spec.criterion == "maximo":
            margin = -difference
        else:
            margin = float(spec.tolerance) - np.abs(difference)
        worst = int(np.argmin(margin))
        failed = int(np.sum(margin < 0))
        result.parameters.append(
            ParameterResult(
                parameter,
                failed == 0,
                evaluated=len(f),
                failed=failed,
                not_evaluated=int(len(frequencies) - len(f)),
                worst_margin=float(margin[worst]),
                worst_frequency=float(f[worst]),
                frequencies=f,
                limit=limit,
            )
        )
    states = [p.passed for p in result.parameters]
    if any(state is False for state in states):
        result.passed = False
    elif states and all(state is True for state in states):
        result.passed = True
    else:
        result.passed = None
        result.message = result.message or "Algún parámetro no se ha podido evaluar"
    return result


def verdict(results: list[SpecResult]) -> str:
    if not results:
        return "SIN_ESPECIFICACIONES"
    if any(r.passed is False for r in results):
        return "FALLA"
    if all(r.passed is True for r in results):
        return "PASA"
    return "NO_EVALUADO"
