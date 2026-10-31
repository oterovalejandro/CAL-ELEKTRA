"""Uncertainty of a standard from its electrical specifications (k = 2), as in METAS VNA Tools (§4.6.1).

Each row of the specifications gives the values from its frequency on; between rows they are
interpolated linearly and outside the table they keep the value of the nearest row. Each quantity is
one input of the budget, standard normal and shared by all the frequencies (a systematic deviation of
the standard from its definition), with the sensitivity U(f)/2 times:

* open and short — magnitude deviation δM (dB) and phase deviation δφ (°):
  ∂Γ/∂δM = Γ·ln10/20 and ∂Γ/∂δφ = j·Γ·π/180;
* load — return loss RL (dB): U(Re Γ) = U(Im Γ) = 10^(RL/20) (−40 dB → 0,01), one input for the real
  part and another for the imaginary part;
* through — the return loss of S11 and S22 (an input for each part of each) and the magnitude and
  phase deviations of the transmission, one input each for S21 and S12 (the through is reciprocal).

A ``.sdatb`` brings its own uncertainty: its specifications are not added.
"""

from __future__ import annotations

import math

import numpy as np

from calelektra.kits.model import SpecRow
from calelektra.sdata.model import SParamData
from calelektra.sdata.unclib import UncVector, new_input

LN10_20 = math.log(10) / 20
DEG = math.pi / 180


def spec_values(rows: list[SpecRow], column: str, frequencies: np.ndarray) -> np.ndarray | None:
    """Specified value (k = 2) at each frequency, or None if no row gives it."""
    points = sorted((row.frequency, row.value(column)) for row in rows if row.value(column) is not None)
    if not points:
        return None
    xs, ys = zip(*points)
    return np.interp(np.asarray(frequencies, float), xs, ys)


def _elements(data: SParamData, receiver: int, source: int, part: int) -> np.ndarray:
    n = data.n_ports
    return 2 * n + 2 * ((np.arange(data.n_points) * n + receiver) * n + source) + part


def specification_inputs(data: SParamData, kind: str, rows: list[SpecRow], name: str) -> list[tuple[str, np.ndarray]]:
    """(description, sensitivity of every element of ``data.vector``) of each input of the specifications."""
    f, s = data.frequencies, data.s
    size = len(data.vector)
    inputs: list[tuple[str, np.ndarray]] = []

    def add(description: str, parts: list[tuple[np.ndarray, np.ndarray]]) -> None:
        sensitivity = np.zeros(size)
        for elements, values in parts:
            sensitivity[elements] += values
        if np.any(sensitivity):
            inputs.append((f"{name}: {description} (especificación, k = 2)", sensitivity))

    def polar(receiver: int, source: int, magnitude: np.ndarray | None, phase: np.ndarray | None) -> tuple[list, list]:
        g = s[:, receiver, source]
        re, im = _elements(data, receiver, source, 0), _elements(data, receiver, source, 1)
        mag_parts = [(re, g.real * LN10_20 * magnitude / 2), (im, g.imag * LN10_20 * magnitude / 2)] if magnitude is not None else []
        phase_parts = [(re, -g.imag * DEG * phase / 2), (im, g.real * DEG * phase / 2)] if phase is not None else []
        return mag_parts, phase_parts

    def return_loss(receiver: int, source: int, label: str) -> None:
        rl = spec_values(rows, "perdidas_retorno_db", f)
        if rl is None:
            return
        u = 10 ** (rl / 20)
        for part, text in ((0, "parte real"), (1, "parte imaginaria")):
            add(f"{label}{text} (pérdidas de retorno)", [(_elements(data, receiver, source, part), u / 2)])

    if kind in ("abierto", "cortocircuito"):
        mag, phase = polar(0, 0, spec_values(rows, "modulo_db", f), spec_values(rows, "fase_grados", f))
        add("desviación de módulo", mag)
        add("desviación de fase", phase)
    elif kind == "carga":
        return_loss(0, 0, "")
    elif kind == "through":
        return_loss(0, 0, "S11, ")
        return_loss(1, 1, "S22, ")
        magnitude, phase = spec_values(rows, "transmision_modulo_db", f), spec_values(rows, "transmision_fase_grados", f)
        mag21, phase21 = polar(1, 0, magnitude, phase)
        mag12, phase12 = polar(0, 1, magnitude, phase)
        add("transmisión, desviación de módulo", mag21 + mag12)
        add("transmisión, desviación de fase", phase21 + phase12)
    return inputs


def with_specifications(data: SParamData, kind: str, rows: list[SpecRow], name: str) -> SParamData:
    """``data`` with the inputs of its specifications added (after its own inputs, if it has any)."""
    added = specification_inputs(data, kind, rows, name)
    if not added:
        return data
    vector = data.vector
    first = len(vector.inputs)
    inputs = list(vector.inputs) + [new_input(description) for description, _ in added]
    sensitivities = np.array([sensitivity for _, sensitivity in added])  # (new inputs, elements)
    offsets, indices, jacobians = [0], [], []
    for element in range(len(vector)):
        own_indices, own_jacobians = vector.dependencies(element)
        indices.extend(own_indices.tolist())
        jacobians.extend(own_jacobians.tolist())
        for k in np.flatnonzero(sensitivities[:, element]):
            indices.append(first + int(k))
            jacobians.append(float(sensitivities[k, element]))
        offsets.append(len(indices))
    return SParamData(data.frequencies, data.ports, UncVector(vector.values, inputs, offsets, indices, jacobians), data.conversions)
