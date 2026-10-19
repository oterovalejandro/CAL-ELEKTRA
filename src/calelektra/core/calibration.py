"""VNA systematic-error model and calibration math (vectorised over frequency).

Notation of the classic 12-term model (forward / reverse), without isolation:

    port 1: e00 directivity, e11 source match, e10e01 reflection tracking
    port 2: e33 directivity, e22 source match, e23e32 reflection tracking
    forward:  e10e32 transmission tracking, e22f load match
    reverse:  e23e01 transmission tracking, e11r load match

The real R&S ZNB43 solves this internally (``SENS:CORR:COLL:…``). The dummy
VNA uses this module so that the calibration flow (standards acquired in the
right order, applied, then used to correct measurements) behaves like the
real instrument and can be tested.

Ideal standards are used: open Γ = +1, short Γ = −1, match Γ = 0, flush
through S21 = S12 = 1, S11 = S22 = 0.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

STANDARD_GAMMA = {"OPEN": 1.0 + 0j, "SHORT": -1.0 + 0j, "MATCH": 0.0 + 0j}


class CalibrationError(ValueError):
    pass


@dataclass
class OnePortTerms:
    directivity: np.ndarray  # e00 (or e33)
    source_match: np.ndarray  # e11 (or e22)
    tracking: np.ndarray  # e10e01 (or e23e32)

    def measure(self, gamma: np.ndarray) -> np.ndarray:
        """Raw reflection seen by the reflectometer for an actual reflection ``gamma``."""
        return self.directivity + self.tracking * gamma / (1 - self.source_match * gamma)

    def correct(self, measured: np.ndarray) -> np.ndarray:
        difference = measured - self.directivity
        return difference / (self.tracking + self.source_match * difference)


@dataclass
class TwoPortTerms:
    port1: OnePortTerms
    port2: OnePortTerms
    forward_tracking: np.ndarray  # e10e32
    reverse_tracking: np.ndarray  # e23e01
    forward_load_match: np.ndarray  # e22f
    reverse_load_match: np.ndarray  # e11r

    def correct(self, measured: np.ndarray) -> np.ndarray:
        """Corrected 2×2 S-matrices from raw ones, shape ``(points, 2, 2)``."""
        p1, p2 = self.port1, self.port2
        n11 = (measured[:, 0, 0] - p1.directivity) / p1.tracking
        n21 = measured[:, 1, 0] / self.forward_tracking
        n12 = measured[:, 0, 1] / self.reverse_tracking
        n22 = (measured[:, 1, 1] - p2.directivity) / p2.tracking
        e11, e22 = p1.source_match, p2.source_match
        e22f, e11r = self.forward_load_match, self.reverse_load_match
        d = (1 + n11 * e11) * (1 + n22 * e22) - n21 * n12 * e22f * e11r
        corrected = np.empty_like(measured)
        corrected[:, 0, 0] = (n11 * (1 + n22 * e22) - e22f * n21 * n12) / d
        corrected[:, 1, 0] = n21 * (1 + n22 * (e22 - e22f)) / d
        corrected[:, 0, 1] = n12 * (1 + n11 * (e11 - e11r)) / d
        corrected[:, 1, 1] = (n22 * (1 + n11 * e11) - e11r * n21 * n12) / d
        return corrected


def solve_one_port(measured: dict[str, np.ndarray]) -> OnePortTerms:
    """OSM: error terms of one port from the raw reflections of open, short and match.

    Linear form per frequency: Γm = e00 + Γ·Γm·e11 − Γ·Δe, with Δe = e00·e11 − e10e01.
    """
    missing = [name for name in STANDARD_GAMMA if name not in measured]
    if missing:
        raise CalibrationError(f"faltan estándares para OSM: {', '.join(missing)}")
    points = len(measured["OPEN"])
    a = np.zeros((points, 3, 3), dtype=complex)
    b = np.zeros((points, 3), dtype=complex)
    for row, (name, gamma) in enumerate(STANDARD_GAMMA.items()):
        gm = measured[name]
        a[:, row, 0] = 1
        a[:, row, 1] = gamma * gm
        a[:, row, 2] = -gamma
        b[:, row] = gm
    e00, e11, delta = np.linalg.solve(a, b[..., None])[..., 0].T
    return OnePortTerms(directivity=e00, source_match=e11, tracking=e00 * e11 - delta)


def solve_tosm(port1: OnePortTerms, port2: OnePortTerms, through: np.ndarray) -> TwoPortTerms:
    """TOSM with a known flush through (planes connected directly)."""
    e22f = port1.correct(through[:, 0, 0])  # load match seen from port 1
    e11r = port2.correct(through[:, 1, 1])  # load match seen from port 2
    return TwoPortTerms(
        port1=port1,
        port2=port2,
        forward_tracking=through[:, 1, 0] * (1 - port1.source_match * e22f),
        reverse_tracking=through[:, 0, 1] * (1 - port2.source_match * e11r),
        forward_load_match=e22f,
        reverse_load_match=e11r,
    )


def solve_uosm(port1: OnePortTerms, port2: OnePortTerms, through: np.ndarray, frequencies: np.ndarray) -> TwoPortTerms:
    """UOSM with an unknown reciprocal through (8-term model, ideal switch).

    Reciprocity of the through fixes e10e32² = S21m·e10e01·e23e32 / S12m. The
    sign of the root is chosen like the «AUTO delay» option of the analyzer:
    the phase of the corrected through must follow the delay estimated from
    its (sign-free) transmission product, with zero phase extrapolated to DC.
    """
    product = port1.tracking * port2.tracking  # e10e01 · e23e32 = e10e32 · e23e01
    k = np.sqrt(through[:, 1, 0] * product / through[:, 0, 1])
    terms = TwoPortTerms(port1, port2, k, product / k, port2.source_match, port1.source_match)
    corrected = terms.correct(through)
    transmission_product = corrected[:, 1, 0] * corrected[:, 0, 1]  # independent of the sign of k
    phase2 = np.unwrap(np.angle(transmission_product))
    if len(frequencies) > 1:
        slope, intercept = np.polyfit(frequencies, phase2, 1)
    else:
        intercept = phase2[0]
    phase2 = phase2 - 2 * np.pi * np.round(intercept / (2 * np.pi))
    expected = np.exp(1j * phase2 / 2)
    flip = np.abs(np.angle(corrected[:, 1, 0] / expected)) > np.pi / 2
    k = np.where(flip, -k, k)
    return TwoPortTerms(port1, port2, k, product / k, port2.source_match, port1.source_match)


def cascade(x: np.ndarray, y: np.ndarray) -> np.ndarray:
    """Cascade of two 2-ports (port 2 of ``x`` to port 1 of ``y``), shape ``(points, 2, 2)``."""
    denominator = 1 - x[:, 1, 1] * y[:, 0, 0]
    result = np.empty_like(x)
    result[:, 0, 0] = x[:, 0, 0] + x[:, 0, 1] * x[:, 1, 0] * y[:, 0, 0] / denominator
    result[:, 1, 0] = x[:, 1, 0] * y[:, 1, 0] / denominator
    result[:, 0, 1] = x[:, 0, 1] * y[:, 0, 1] / denominator
    result[:, 1, 1] = y[:, 1, 1] + y[:, 0, 1] * y[:, 1, 0] * x[:, 1, 1] / denominator
    return result


def error_box(terms: OnePortTerms, transmission: np.ndarray, facing_dut_at_port1: bool) -> np.ndarray:
    """2-port error adapter whose reflection terms are ``terms`` and whose
    transmission coefficient (both directions) is ``transmission``."""
    box = np.empty((len(transmission), 2, 2), dtype=complex)
    if facing_dut_at_port1:  # port 2 of the analyzer: [[e22, e23], [e32, e33]]
        box[:, 0, 0], box[:, 1, 1] = terms.source_match, terms.directivity
    else:  # port 1 of the analyzer: [[e00, e01], [e10, e11]]
        box[:, 0, 0], box[:, 1, 1] = terms.directivity, terms.source_match
    box[:, 0, 1] = box[:, 1, 0] = transmission
    return box
