"""Generate the SYNTHETIC S2P files of the example library.

* ``catalogo/datos_s``: S-parameters of the measurement cables and paths
  (coaxial cable model: loss ∝ √f and f, delay, small reflections);
* ``catalogo/especificaciones``: specification masks of the example cable
  models (``especificaciones_s``), used for pass / fail;
* ``hardware_setups/listas``: frequency list (CSV) of the list-mode setup.

They are only meant to exercise the software: they are not real measurements
nor real manufacturer specifications.

    python tools/generar_s2p_sinteticos.py
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from calelektra.core.touchstone import Touchstone, write_touchstone
from calelektra.core.units import format_frequency

LIBRARY = Path(__file__).resolve().parents[1] / "biblioteca_ejemplo"
OUT = LIBRARY / "catalogo" / "datos_s"
SPECS = LIBRARY / "catalogo" / "especificaciones"
LISTS = LIBRARY / "hardware_setups" / "listas"

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


def mask_292(constant: float, k_sqrt: float, k_lin: float) -> tuple[np.ndarray, np.ndarray]:
    """Mask of a 2,92 mm cable: maximum insertion loss (S21, S12) and minimum return loss (S11, S22)."""
    steps = [18e9, 26.5e9]
    f = np.unique(np.concatenate([np.geomspace(10e6, 40e9, 300), steps, [x + 1.0 for x in steps], [40e9]]))
    f_ghz = f / 1e9
    insertion_loss = constant + k_sqrt * np.sqrt(f_ghz) + k_lin * f_ghz
    return_loss = np.where(f <= 18e9, 20.0, np.where(f <= 26.5e9, 17.0, 15.0))
    s = np.empty((f.size, 2, 2), dtype=complex)
    s[:, 1, 0] = s[:, 0, 1] = 10 ** (-insertion_loss / 20)
    s[:, 0, 0] = s[:, 1, 1] = 10 ** (-return_loss / 20)
    return f, s


def reference_n_2m() -> tuple[np.ndarray, np.ndarray]:
    """Typical |S21| of a 2 m N cable (0,45 dB/m at 1 GHz): reference curve for a ± tolerance check."""
    f = np.geomspace(10e6, 18e9, 300)
    f_ghz = f / 1e9
    loss = 0.45 * 2.0 * (0.9 * np.sqrt(f_ghz) + 0.1 * f_ghz) + 0.05
    s = np.zeros((f.size, 2, 2), dtype=complex)
    s[:, 1, 0] = s[:, 0, 1] = 10 ** (-loss / 20)
    s[:, 0, 0] = s[:, 1, 1] = 10 ** (-25 / 20)
    return f, s


def frequency_list_n() -> list[float]:
    """Calibration points of the N cables: 10 MHz steps up to 1 GHz, 100 MHz steps up to 18 GHz."""
    low = np.arange(30e6, 1e9, 10e6)
    high = np.arange(1e9, 18e9 + 1, 100e6)
    return [float(x) for x in np.concatenate([low, high])]


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
    SPECS.mkdir(parents=True, exist_ok=True)
    note = "CAL-ELEKTRA — ESPECIFICACIÓN SINTÉTICA DE EJEMPLO: no es la de ningún fabricante."
    for name, length, coefficients in (("cable_292_0m5_mascara.s2p", "0,5 m", (0.1, 0.5, 0.06)), ("cable_292_1m_mascara.s2p", "1 m", (0.05, 0.68, 0.08))):
        f, s = mask_292(*coefficients)
        comments = [note, f"Cable 2,92 mm de {length}: S21/S12 = pérdidas de inserción máximas; S11/S22 = pérdidas de retorno mínimas"]
        write_touchstone(SPECS / name, Touchstone(2, f, s, comments=comments), "DB", "GHZ")
    f, s = reference_n_2m()
    comments = [note, "Cable N de 2 m: |S21| típico (curva de referencia para la comprobación ± tolerancia)"]
    write_touchstone(SPECS / "cable_N_2m_referencia.s2p", Touchstone(2, f, s, comments=comments), "DB", "GHZ")
    LISTS.mkdir(parents=True, exist_ok=True)
    rows = ["# Puntos de calibración de cables N (ejemplo). Primera columna: frecuencia.", "Frecuencia (MHz)"]
    rows += [f"{x / 1e6:g}" for x in frequency_list_n()]
    (LISTS / "puntos_cable_N.csv").write_text("\n".join(rows) + "\n", encoding="utf-8")
    print("escritos", SPECS, LISTS)


if __name__ == "__main__":
    main()
