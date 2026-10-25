"""Uncertainty of the impedance Z = Z0 (1 + Γ)/(1 − Γ) from that of the reflection Γ (S11).

The derivation and its checks are in ``docs/10_algoritmos_de_incertidumbre.md``.
For each point, with Γ = ρ·e^{jφ} and the expanded uncertainties of its
magnitude (dB or linear) and phase (°), all with the same coverage factor:

1. Magnitude to linear (the derivative the engine uses):
   U_ρ = ρ · ln10/20 · U_dB.
2. Tangential uncertainty, never smaller than the radial one (the error of Γ
   can point in any direction, as in the VNA error model):
   U_t = max(ρ · U_φ[rad], U_ρ).
3. First order (GUM), magnitude and phase errors uncorrelated, with
   B = 2·e^{jφ}/(1 − Γ²) = b_r + j·b_i:
   U(|Z|)/|Z| = √[(b_r·U_ρ)² + (b_i·U_t)²],  U(∠Z) = √[(b_i·U_ρ)² + (b_r·U_t)²].
4. Where the first order is not valid — the uncertainty r = U_t is more than
   10 % of the distance from Γ to the open (|1 − Γ|) or to the short
   (|1 + Γ|) — the exact bounds of Z for Γ in the disk |Γ − Γ0| ≤ r: the map
   is a Möbius transformation and the image of the disk is a disk,
   Zc = Z0·(−1 + 2·conj(1 − Γ0)/d), R = 2·Z0·r/d, d = |1 − Γ0|² − r².
   The bounds are not symmetric. With d ≤ 0 the disk holds the open (Z is
   not bounded) and the point has no uncertainty.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np

Z0 = 50.0  # reference impedance (Ω)
LN10_20 = math.log(10) / 20  # d(ln ρ)/d(dB)
DB_PER_RELATIVE = 20 / math.log(10)  # dB of a small relative change: 8.686
LINEAR_LIMIT = 0.1  # first order while r ≤ 10 % of |1 − Γ| and of |1 + Γ|


def _wrap(degrees: np.ndarray) -> np.ndarray:
    return (degrees + 180.0) % 360.0 - 180.0


@dataclass
class ImpedanceUncertainty:
    """Expanded uncertainty of Z at each point: above (+) and below (−) the value; equal unless ``exact``."""

    impedance: np.ndarray  # Z (Ω), complex
    magnitude_plus: np.ndarray  # Ω
    magnitude_minus: np.ndarray
    magnitude_db_plus: np.ndarray  # dB (|Z| in dBΩ)
    magnitude_db_minus: np.ndarray
    phase_plus: np.ndarray  # degrees
    phase_minus: np.ndarray
    exact: np.ndarray  # bool: exact bounds of the disk (the first order is not valid there)
    z0: float = Z0

    @property
    def magnitude(self) -> np.ndarray:
        return np.abs(self.impedance)

    @property
    def phase_deg(self) -> np.ndarray:
        return np.degrees(np.angle(self.impedance))

    @property
    def missing(self) -> int:
        """Points without uncertainty of |Z| or ∠Z (no uncertainty of Γ, or Z not bounded)."""
        finite = np.isfinite(self.magnitude_plus) & np.isfinite(self.magnitude_minus) & np.isfinite(self.phase_plus) & np.isfinite(self.phase_minus)
        return int(np.count_nonzero(~finite))

    def summary(self) -> dict[str, Any]:
        def extremes(plus: np.ndarray, minus: np.ndarray, digits: int) -> dict[str, float] | None:
            values = np.fmax(plus, minus)
            finite = values[np.isfinite(values)]
            if not len(finite):
                return None
            return {"min": round(float(finite.min()), digits), "max": round(float(finite.max()), digits)}

        entry: dict[str, Any] = {
            "z0_ohm": self.z0,
            "U_ohm": extremes(self.magnitude_plus, self.magnitude_minus, 4),
            "U_dbohm": extremes(self.magnitude_db_plus, self.magnitude_db_minus, 4),
            "U_grados": extremes(self.phase_plus, self.phase_minus, 3),
            "puntos": len(self.impedance),
        }
        if self.exact.any():
            entry["puntos_cota_exacta"] = int(np.count_nonzero(self.exact))
        if self.missing:
            entry["puntos_sin_incertidumbre"] = self.missing
        return entry


def impedance_text(summary: dict[str, Any]) -> str:
    """The uncertainty of Z kept in a record (``ImpedanceUncertainty.summary``), as one line."""
    parts = []
    ohm, dbohm, degrees = summary.get("U_ohm"), summary.get("U_dbohm"), summary.get("U_grados")
    if ohm:
        parts.append(f"|Z| {ohm['min']:g} – {ohm['max']:g} Ω" + (f" ({dbohm['min']:g} – {dbohm['max']:g} dB)" if dbohm else ""))
    if degrees:
        parts.append(f"∠Z {degrees['min']:g} – {degrees['max']:g}°")
    text = "U " + ", ".join(parts) if parts else "—"
    text += f" · Z0 = {summary.get('z0_ohm', Z0):g} Ω"
    if summary.get("puntos_cota_exacta"):
        text += f" · {summary['puntos_cota_exacta']} punto(s) con cota exacta (no simétrica)"
    if summary.get("puntos_sin_incertidumbre"):
        text += f" · {summary['puntos_sin_incertidumbre']} de {summary.get('puntos')} puntos sin incertidumbre"
    return text


def impedance_uncertainty(
    gamma: np.ndarray, u_magnitude: np.ndarray, u_phase_deg: np.ndarray, magnitude_unit: str = "dB", z0: float = Z0
) -> ImpedanceUncertainty:
    """Expanded uncertainty of Z from that of Γ: magnitude in dB (``magnitude_unit="dB"``) or linear, phase in degrees."""
    gamma = np.asarray(gamma, dtype=complex)
    u_magnitude = np.asarray(u_magnitude, dtype=float)
    u_phase = np.radians(np.asarray(u_phase_deg, dtype=float))
    rho, phi = np.abs(gamma), np.angle(gamma)
    with np.errstate(divide="ignore", invalid="ignore", over="ignore"):
        u_rho = rho * LN10_20 * u_magnitude if magnitude_unit.lower() == "db" else u_magnitude
        u_t = np.maximum(rho * u_phase, u_rho)  # NaN stays NaN: no uncertainty of Γ, none of Z
        z = z0 * (1 + gamma) / (1 - gamma)
        magnitude = np.abs(z)

        # first order
        b = 2 * np.exp(1j * phi) / (1 - gamma**2)
        relative = np.hypot(b.real * u_rho, b.imag * u_t)
        theta = np.degrees(np.hypot(b.imag * u_rho, b.real * u_t))
        magnitude_plus = magnitude * relative
        magnitude_db = DB_PER_RELATIVE * relative
        magnitude_minus, magnitude_db_plus, magnitude_db_minus = magnitude_plus.copy(), magnitude_db.copy(), magnitude_db.copy()
        phase_plus, phase_minus = theta.copy(), theta.copy()

        # exact bounds where the first order is not valid
        r = u_t
        exact = np.isfinite(r) & (r > LINEAR_LIMIT * np.minimum(np.abs(1 - gamma), np.abs(1 + gamma)))
        if exact.any():
            g, rr, zz = gamma[exact], r[exact], z[exact]
            zeta = 1 - g
            d = np.abs(zeta) ** 2 - rr**2
            bounded = d > 0
            center = z0 * (-1 + 2 * np.conj(zeta) / d)
            radius = 2 * z0 * rr / d
            high = np.where(bounded, np.abs(center) + radius, np.nan)
            low = np.where(bounded, np.maximum(np.abs(center) - radius, 0.0), np.nan)
            magnitude_plus[exact] = high - np.abs(zz)
            magnitude_minus[exact] = np.abs(zz) - low
            magnitude_db_plus[exact] = 20 * np.log10(high / np.abs(zz))
            magnitude_db_minus[exact] = 20 * np.log10(np.abs(zz) / low)  # +inf when the disk reaches Z = 0
            determined = bounded & (radius < np.abs(center))  # otherwise the disk holds Z = 0: any phase
            half = np.degrees(np.arcsin(np.where(determined, radius / np.abs(center), 0.0)))
            middle = np.degrees(np.angle(center))
            nominal = np.degrees(np.angle(zz))
            phase_plus[exact] = np.where(determined, np.abs(_wrap(middle + half - nominal)), np.nan)
            phase_minus[exact] = np.where(determined, np.abs(_wrap(nominal - (middle - half))), np.nan)
    return ImpedanceUncertainty(
        z, magnitude_plus, magnitude_minus, magnitude_db_plus, magnitude_db_minus, phase_plus, phase_minus, exact, z0
    )
