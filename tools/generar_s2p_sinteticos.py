"""Generate the SYNTHETIC S2P files of the example library.

They model a coaxial cable (loss ∝ √f and f, delay, small reflections) and
are only meant to exercise the software. They are not real measurements.

    python tools/generar_s2p_sinteticos.py
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from calelektra.core.touchstone import Touchstone, write_touchstone
from calelektra.core.units import format_frequency

OUT = Path(__file__).resolve().parents[1] / "biblioteca_ejemplo" / "catalogo" / "datos_s"

# name: (start Hz, stop Hz, length m, k_sqrt dB/√GHz, k_lin dB/GHz, return loss dB at low f)
CABLES = {
    "CAB-PT-N-01.s2p": (5.0, 18e9, 1.5, 0.50, 0.020, 32.0),
    "CAB-PT-N-02.s2p": (5.0, 18e9, 1.5, 0.52, 0.021, 31.0),
    "CAB-PT-292-01.s2p": (10e6, 40e9, 1.0, 0.60, 0.030, 30.0),
    "CAB-PT-292-02.s2p": (10e6, 40e9, 1.0, 0.61, 0.030, 29.5),
    "CAB-PT-BF-01.s2p": (5.0, 6e9, 1.0, 0.45, 0.020, 33.0),
    "CAB-PT-BF-02.s2p": (5.0, 6e9, 1.0, 0.46, 0.021, 32.5),
    "PATH-TX-01.s2p": (9e3, 18e9, 12.0, 1.80, 0.120, 26.0),
    "PATH-RX-01.s2p": (9e3, 18e9, 15.0, 2.10, 0.140, 25.0),
}


def synthetic_cable(start: float, stop: float, length: float, k_sqrt: float, k_lin: float, rl_db: float, seed: int):
    rng = np.random.default_rng(seed)
    f = np.geomspace(start, stop, 401)
    f_ghz = f / 1e9
    loss_db = k_sqrt * np.sqrt(f_ghz) + k_lin * f_ghz
    delay = length / (0.83 * 299_792_458.0)
    s21 = 10 ** (-loss_db / 20) * np.exp(-2j * np.pi * f * delay)
    rl = rl_db - 0.4 * f_ghz + rng.normal(0, 0.3, f.size)
    gamma = 10 ** (-rl / 20)
    s11 = gamma * np.exp(-1j * (4 * np.pi * f * delay * 0.02 + 0.7))
    s22 = gamma * np.exp(-1j * (4 * np.pi * f * delay * 0.03 - 0.4))
    s = np.empty((f.size, 2, 2), dtype=complex)
    s[:, 0, 0], s[:, 1, 0], s[:, 0, 1], s[:, 1, 1] = s11, s21, s21, s22
    return f, s


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for seed, (name, (start, stop, length, k_sqrt, k_lin, rl)) in enumerate(CABLES.items()):
        f, s = synthetic_cable(start, stop, length, k_sqrt, k_lin, rl, seed)
        comments = [
            "CAL-ELEKTRA — DATOS SINTÉTICOS DE EJEMPLO: no son una medida real.",
            f"Cable simulado {name[:-4]}: {length} m, {format_frequency(start)} – {format_frequency(stop)}",
        ]
        write_touchstone(OUT / name, Touchstone(n_ports=2, frequencies=f, s=s, comments=comments))
        print("escrito", OUT / name)


if __name__ == "__main__":
    main()
