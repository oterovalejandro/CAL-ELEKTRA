"""Covariance and correlation of S-parameter data with uncertainty (the «Covariance» view of the Data
Explorer of METAS VNA Tools).

Two matrices:

* between frequencies (``frequency_matrix``): one quantity of one S-parameter, its real or imaginary
  part, its magnitude or its phase, at the chosen frequencies;
* at one frequency (``point_matrix``): the real and imaginary parts (or the magnitude and phase) of all
  the S-parameters of a frequency, in the order of a ``.sdatcv`` (by source port: S11, S21, S12, S22).

The covariance of two quantities is that of their linear dependencies on the inputs of the budget,
V = J·diag(u²)·Jᵀ (``UncVector.combination_covariance``), and their correlation ρᵢⱼ = Vᵢⱼ/(uᵢ·uⱼ), which
is not defined for a quantity without uncertainty. The magnitude and the phase are, to first order as in
UncLib, d|S| = (Re·dRe + Im·dIm)/|S| and d∠S = (Re·dIm − Im·dRe)/|S|² (×180/π: degrees); they are not
defined where S = 0 (an ideal load). A variance that is only rounding (the magnitude of a standard that
deviates only in phase) counts as zero: below 10⁻¹⁴ of that of its terms.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from calelektra.core.units import format_frequency
from calelektra.sdata.model import SParamData
from calelektra.sdata.unclib import SDataError

COMPONENTS = {"re": "Parte real", "im": "Parte imaginaria", "modulo": "Módulo", "fase": "Fase (°)"}
POINT_COMPONENTS = {"re_im": "Partes real e imaginaria", "polar": "Módulo y fase"}
SHORT_NAMES = {"re": "re", "im": "im", "modulo": "|S|", "fase": "∠"}
MAX_POINTS = 1200  # frequencies of a matrix between frequencies: more are thinned (equally spaced)
ROUNDING = 1e-14  # relative variance below which a quantity has no uncertainty


@dataclass
class CovarianceMatrix:
    """The covariance of some quantities: a row (and a column) per quantity."""

    labels: list[str]
    covariance: np.ndarray
    defined: np.ndarray  # per row: False where the quantity is not defined (magnitude and phase of S = 0)
    units: list[str]  # of each quantity ("" or "°"): the covariance is in their product
    frequencies: np.ndarray  # Hz, of each row
    points: np.ndarray  # index of the frequency of each row in the data
    total: int  # frequencies in the selection, before thinning
    between_frequencies: bool = True  # a quantity at several frequencies (else: the quantities of a frequency)

    @property
    def size(self) -> int:
        return len(self.labels)

    @property
    def standard_uncertainties(self) -> np.ndarray:
        return np.sqrt(np.clip(np.diag(self.covariance), 0.0, None))

    @property
    def valid(self) -> np.ndarray:
        """The quantities with a correlation: defined and with uncertainty."""
        return self.defined & (self.standard_uncertainties > 0)

    @property
    def correlation(self) -> np.ndarray:
        """ρᵢⱼ = Vᵢⱼ/(uᵢ·uⱼ); NaN for the quantities without uncertainty or not defined."""
        u, valid = self.standard_uncertainties, self.valid
        with np.errstate(divide="ignore", invalid="ignore"):
            rho = np.clip(self.covariance / np.outer(u, u), -1.0, 1.0)
        rho[~valid, :] = np.nan
        rho[:, ~valid] = np.nan
        rho[np.diag_indices(self.size)] = np.where(valid, 1.0, np.nan)
        return rho

    def values(self, kind: str) -> np.ndarray:
        """``correlacion`` or ``covarianza`` (NaN where a quantity is not defined)."""
        if kind == "correlacion":
            return self.correlation
        result = self.covariance.copy()
        result[~self.defined, :] = np.nan
        result[:, ~self.defined] = np.nan
        return result

    @property
    def unit(self) -> str | None:
        """The unit of the quantities if they all have the same, else None."""
        return self.units[0] if len(set(self.units)) == 1 else None

    def off_diagonal(self) -> np.ndarray:
        """The correlations between different quantities (each pair once), without the undefined ones."""
        rho = self.correlation
        upper = rho[np.triu_indices(self.size, 1)]
        return upper[np.isfinite(upper)]

    def text(self, kind: str = "correlacion") -> str:
        """The matrix separated by tabs, with a point as decimal separator (for a spreadsheet): between
        frequencies, headed by the frequencies in Hz."""
        if self.between_frequencies:
            corner, labels = "f (Hz)", [f"{f:.12g}" for f in self.frequencies]
        else:
            corner, labels = format_frequency(self.frequencies[0], decimal="."), self.labels
        lines = ["\t".join([corner, *labels])]
        for label, row in zip(labels, self.values(kind)):
            lines.append("\t".join([label, *("" if math.isnan(v) else repr(float(v)) for v in row)]))
        return "\n".join(lines)


def parameters(data: SParamData) -> list[tuple[str, int, int]]:
    """(name, receiver, source) of each S-parameter, by source port as in a ``.sdatcv``: S11, S21, S12, S22."""
    n = data.n_ports
    name = (lambda r, c: f"S{r + 1}{c + 1}") if n < 10 else (lambda r, c: f"S{r + 1},{c + 1}")
    return [(name(r, c), r, c) for c in range(n) for r in range(n)]


def parameter_indices(data: SParamData, parameter: str) -> tuple[int, int]:
    for name, receiver, source in parameters(data):
        if name == parameter:
            return receiver, source
    raise SDataError(f"Los datos no tienen el parámetro {parameter}")


def select_points(frequencies, start: float | None = None, stop: float | None = None, max_points: int | None = MAX_POINTS) -> tuple[np.ndarray, int]:
    """The frequencies between ``start`` and ``stop`` (included; None: no limit), thinned to ``max_points``
    equally spaced ones (with the first and the last); also how many there were."""
    f = np.asarray(frequencies, float)
    mask = np.ones(len(f), bool)
    if start is not None:
        mask &= f >= start - 1e-9 * abs(start)
    if stop is not None:
        mask &= f <= stop + 1e-9 * abs(stop)
    points = np.flatnonzero(mask)
    total = len(points)
    if max_points and total > max_points:
        points = points[np.unique(np.round(np.linspace(0, total - 1, max_points)).astype(np.int64))]
    return points, total


def nearest_point(frequencies, frequency: float) -> int:
    return int(np.argmin(np.abs(np.asarray(frequencies, float) - frequency)))


def _terms(data: SParamData, receiver: int, source: int, component: str, points: np.ndarray):
    """Elements (real, imaginary part) and weights of a quantity of S[receiver, source] at some frequencies."""
    elements = np.column_stack([data.element(points, receiver, source, 0), data.element(points, receiver, source, 1)])
    value = data.s[points, receiver, source]
    defined = np.ones(len(points), bool)
    if component == "re":
        weights = np.tile([1.0, 0.0], (len(points), 1))
    elif component == "im":
        weights = np.tile([0.0, 1.0], (len(points), 1))
    elif component in ("modulo", "fase"):
        magnitude = np.abs(value)
        defined = magnitude > 0
        safe = np.where(defined, magnitude, 1.0)
        if component == "modulo":
            weights = np.column_stack([value.real / safe, value.imag / safe])
        else:
            weights = np.column_stack([-value.imag / safe**2, value.real / safe**2]) * (180 / math.pi)
        weights[~defined] = 0.0
    else:
        raise SDataError(f"Magnitud desconocida: {component}")
    return elements, weights, defined


def _covariance(data: SParamData, elements: np.ndarray, weights: np.ndarray) -> np.ndarray:
    covariance = data.vector.combination_covariance(elements, weights)
    u = data.vector.standard_uncertainties()
    scale = ((weights * u[elements]) ** 2).sum(axis=1)  # the variance if the terms were not correlated
    rounding = np.diag(covariance) <= ROUNDING * scale
    covariance[rounding, :] = 0.0
    covariance[:, rounding] = 0.0
    return covariance


def frequency_matrix(
    data: SParamData,
    parameter: str,
    component: str = "re",
    start: float | None = None,
    stop: float | None = None,
    max_points: int | None = MAX_POINTS,
) -> CovarianceMatrix:
    """Covariance between frequencies of a quantity (``COMPONENTS``) of an S-parameter (``"S21"``)."""
    receiver, source = parameter_indices(data, parameter)
    points, total = select_points(data.frequencies, start, stop, max_points)
    if not len(points):
        raise SDataError("No hay frecuencias en el rango elegido")
    elements, weights, defined = _terms(data, receiver, source, component, points)
    f = data.frequencies[points]
    return CovarianceMatrix(
        [format_frequency(x) for x in f],
        _covariance(data, elements, weights),
        defined,
        ["°" if component == "fase" else ""] * len(points),
        f,
        points,
        total,
    )


def point_matrix(data: SParamData, point: int, component: str = "re_im") -> CovarianceMatrix:
    """Covariance of the S-parameters of a frequency: real and imaginary parts (``re_im``, the order of a
    ``.sdatcv``) or magnitude and phase (``polar``) of each one."""
    if not 0 <= point < data.n_points:
        raise SDataError(f"No hay frecuencia {point} en los datos")
    quantities = ("re", "im") if component == "re_im" else ("modulo", "fase")
    labels, element_rows, weight_rows, defined = [], [], [], []
    at = np.array([point])
    for name, receiver, source in parameters(data):
        for quantity in quantities:
            elements, weights, ok = _terms(data, receiver, source, quantity, at)
            labels.append(f"{name} {SHORT_NAMES[quantity]}")
            element_rows.append(elements[0])
            weight_rows.append(weights[0])
            defined.append(bool(ok[0]))
    count = len(labels)
    return CovarianceMatrix(
        labels,
        _covariance(data, np.array(element_rows), np.array(weight_rows)),
        np.array(defined),
        ["°" if label.endswith("∠") else "" for label in labels],
        np.full(count, data.frequencies[point]),
        np.full(count, point),
        1,
        between_frequencies=False,
    )
