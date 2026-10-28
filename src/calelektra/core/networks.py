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


def group_delay(frequencies: np.ndarray, s21: np.ndarray, aperture: int = 10) -> np.ndarray:
    """Group delay (s) at each point, −dφ/dω, as the delay format of a VNA.

    The slope of the phase is fitted (least squares) over ``aperture``
    frequency steps centred on the point, fewer at the ends of the sweep. The
    phase is first unwrapped against the delay of the whole trace
    (``transmission_delay``), so that the steps of a list where a long cable
    turns more than half a turn do not alias. NaN where there is no data.
    """
    f = np.asarray(frequencies, dtype=float)
    s = np.asarray(s21, dtype=complex)
    out = np.full(len(f), np.nan)
    valid = np.isfinite(f) & np.isfinite(s) & (np.abs(s) > 0)
    if valid.sum() < 2:
        return out
    fv, sv = f[valid], s[valid]
    overall = transmission_delay(fv, sv) if len(fv) >= 3 else 0.0
    if not np.isfinite(overall):
        overall = 0.0
    omega = 2 * np.pi * fv
    residual = np.unwrap(np.angle(sv * np.exp(1j * omega * overall)))  # what is left changes little between points
    x = omega - omega.mean()
    half = max(1, int(aperture) // 2)
    index = np.arange(len(fv))
    low, high = np.clip(index - half, 0, len(fv) - 1), np.clip(index + half, 0, len(fv) - 1) + 1
    sums = [np.concatenate([[0.0], np.cumsum(values)]) for values in (x, residual, x * x, x * residual)]
    sx, sy, sxx, sxy = (total[high] - total[low] for total in sums)
    count = (high - low).astype(float)
    with np.errstate(divide="ignore", invalid="ignore"):
        slope = (count * sxy - sx * sy) / (count * sxx - sx * sx)
    out[valid] = overall - slope
    return out


def step_delay(frequencies: np.ndarray, s21: np.ndarray) -> np.ndarray:
    """Delay (s) between each point and the previous one, −Δφ/Δω of the unwrapped phase (the first point: that of the second).

    The point-wise delay of the certificates of the laboratory, before their smoothing (``moving_average``).
    """
    f = np.asarray(frequencies, dtype=float)
    out = np.full(len(f), np.nan)
    if len(f) < 2:
        return out
    phase = np.unwrap(np.angle(np.asarray(s21, dtype=complex)))
    with np.errstate(divide="ignore", invalid="ignore"):
        steps = -np.diff(phase) / np.diff(2 * np.pi * f)
    out[1:] = steps
    out[0] = steps[0]
    return out


def moving_average(values: np.ndarray, size: int) -> np.ndarray:
    """Mean over ``size`` points, window [k − size//2, k + (size − 1)//2], mirrored at the ends.

    The same as ``scipy.ndimage.uniform_filter1d(values, size)`` (mode ``reflect``): the smoothing of the
    certificates of the laboratory, with ``size`` = 1 % of the points.
    """
    x = np.asarray(values, dtype=float)
    if size < 2 or len(x) < 2:
        return x.copy()
    left, right = size // 2, (size - 1) // 2
    padded = np.pad(x, (left, right), mode="symmetric")
    sums = np.concatenate([[0.0], np.cumsum(padded)])
    return (sums[size:] - sums[:-size]) / size


def gamma_to_impedance(gamma: np.ndarray, z0: float = 50.0) -> np.ndarray:
    return z0 * (1 + gamma) / (1 - gamma)


def impedance_to_gamma(z: np.ndarray, z0: float = 50.0) -> np.ndarray:
    return (z - z0) / (z + z0)
