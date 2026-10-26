"""Network calculations: nominal impedance networks (procedures and simulation), Γ ↔ Z and the delay of a transmission."""

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


def transmission_delay(frequencies: np.ndarray, s21: np.ndarray, nominal: float | None = None) -> float:
    """Delay (s) of a transmission: the slope of its phase, −dφ/dω, fitted over the band.

    A list of frequencies can have steps where the phase turns more than half a
    turn (a long cable between points 100 MHz apart), and a plain unwrap
    aliases. So the phase is first fitted on the densest run of points, then
    unwrapped over the whole band against that first delay. The densest step
    Δf resolves delays up to about 1/(2·Δf); beyond that the delay is ambiguous
    by multiples of 1/Δf, and a ``nominal`` delay longer than 0.4/Δf picks the
    closest one.
    """
    f = np.asarray(frequencies, dtype=float)
    s = np.asarray(s21, dtype=complex)
    valid = np.isfinite(f) & np.isfinite(s) & (np.abs(s) > 0)
    f, s = f[valid], s[valid]
    order = np.argsort(f)
    f, s = f[order], s[order]
    if len(f) < 3:
        return float("nan")
    steps = np.diff(f)
    smallest = steps[steps > 0].min() if (steps > 0).any() else 0.0
    dense = np.concatenate([[False], steps <= 1.5 * smallest]) if smallest > 0 else np.zeros(len(f), dtype=bool)
    # longest run of points with the smallest step
    best, start = (0, 0), None
    for k in range(len(f) + 1):
        if k < len(f) and dense[k]:
            start = k - 1 if start is None else start
        elif start is not None:
            if k - start > best[1] - best[0]:
                best = (start, k)
            start = None
    run = slice(*best) if best[1] - best[0] >= 3 else slice(0, len(f))
    first = -np.polyfit(2 * np.pi * f[run], np.unwrap(np.angle(s[run])), 1)[0]
    if nominal is not None and smallest > 0 and nominal > 0.4 / smallest:
        period = 1 / smallest  # delays first + k/Δf give the same phase at the points of the run
        first += round((nominal - first) / period) * period
    residual = np.unwrap(np.angle(s * np.exp(2j * np.pi * f * first)))
    return float(first - np.polyfit(2 * np.pi * f, residual, 1)[0])


def gamma_to_impedance(gamma: np.ndarray, z0: float = 50.0) -> np.ndarray:
    return z0 * (1 + gamma) / (1 - gamma)


def impedance_to_gamma(z: np.ndarray, z0: float = 50.0) -> np.ndarray:
    return (z - z0) / (z + z0)
