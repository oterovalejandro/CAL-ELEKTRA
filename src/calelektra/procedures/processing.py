"""Trace processing applied by CAL-ELEKTRA after reading the VNA (the same for real and simulated VNAs)."""

from __future__ import annotations

import numpy as np


def smoothing_window(points: int, aperture_pct: float) -> int:
    """Odd number of points of the smoothing window (``aperture_pct`` % of the trace points)."""
    window = int(round(points * aperture_pct / 100.0))
    if window < 2:
        return 1
    return window if window % 2 else window + 1


def smooth(trace: np.ndarray, aperture_pct: float) -> np.ndarray:
    """Moving average of the magnitude (dB) and of the unwrapped phase, like the trace smoothing of a VNA.

    Averaging the complex values directly would cancel the rotating phase of a
    long cable, so magnitude and phase are smoothed separately.
    """
    window = smoothing_window(len(trace), aperture_pct)
    if window == 1:
        return trace.copy()
    kernel = np.ones(window)
    counts = np.convolve(np.ones(len(trace)), kernel, mode="same")

    def moving(values: np.ndarray) -> np.ndarray:
        return np.convolve(values, kernel, mode="same") / counts

    with np.errstate(divide="ignore"):
        magnitude_db = 20 * np.log10(np.maximum(np.abs(trace), 1e-15))
    phase = np.unwrap(np.angle(trace))
    return 10 ** (moving(magnitude_db) / 20) * np.exp(1j * moving(phase))
