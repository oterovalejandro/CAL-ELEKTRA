"""Nominal impedance networks used by the procedures (and by the simulation)."""

from __future__ import annotations

import numpy as np

# V-AMN (LISN) networks of CISPR 16-1-2: 50 Ω in parallel with (L + R)
LISN_NETWORKS: dict[str, tuple[float, float]] = {
    "50Ω‖(50µH+5Ω)": (50e-6, 5.0),
    "50Ω‖50µH": (50e-6, 0.0),
    "50Ω‖5µH+1Ω": (5e-6, 1.0),
}


def lisn_impedance(network: str, frequencies: np.ndarray, spread: float = 1.0) -> np.ndarray:
    """Complex impedance at the EUT terminal of the nominal network (``spread`` scales L and the 50 Ω)."""
    if network not in LISN_NETWORKS:
        raise ValueError(f"Red de LISN desconocida: {network!r} ({', '.join(LISN_NETWORKS)})")
    inductance, resistance = LISN_NETWORKS[network]
    branch = resistance + 2j * np.pi * np.asarray(frequencies) * inductance * spread
    return 1 / (1 / (50.0 * spread) + 1 / branch)


def gamma_to_impedance(gamma: np.ndarray, z0: float = 50.0) -> np.ndarray:
    return z0 * (1 + gamma) / (1 - gamma)


def impedance_to_gamma(z: np.ndarray, z0: float = 50.0) -> np.ndarray:
    return (z - z0) / (z + z0)
