"""CSV files of the results of a test: the traces as measured (no smoothing) and their uncertainty.

They are written at the end of the run and again when the uncertainty is
recalculated with the ambient conditions of the test.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from calelektra.core.networks import LISN_NETWORKS, gamma_to_impedance, lisn_impedance
from calelektra.uncertainty.traces import TraceUncertainty

TWO_PORT = (("S11", (0, 0)), ("S21", (1, 0)), ("S12", (0, 1)), ("S22", (1, 1)))


def db(values: np.ndarray) -> np.ndarray:
    with np.errstate(divide="ignore"):
        return 20 * np.log10(np.maximum(np.abs(values), 1e-15))


def csv_number(value: float) -> str:
    return f"{value:.7g}".replace(".", ",")


def write_csv(path: Path, header: list[str], columns: list[np.ndarray]) -> None:
    rows = [";".join(header)]
    for k in range(len(columns[0])):
        frequency = f"{float(columns[0][k]):.12g}"
        rows.append(";".join([frequency] + [csv_number(float(column[k])) for column in columns[1:]]))
    path.write_text("\n".join(rows) + "\n", encoding="utf-8-sig")


def uncertainty_csv_path(result_path: Path) -> Path:
    """``<result>_incertidumbre.csv`` next to the result Touchstone file."""
    return result_path.with_name(f"{result_path.stem}_incertidumbre.csv")


def uncertainty_columns(uncertainty: dict[str, TraceUncertainty] | None, name: str, k: float) -> tuple[list[str], list[np.ndarray]]:
    if not uncertainty or name not in uncertainty:
        return [], []
    trace = uncertainty[name]
    return [f"U {name} (dB, k={k:g})", f"U {name} (°, k={k:g})"], [trace.magnitude_db, trace.phase_deg]


def write_uncertainty_csv(path: Path, frequencies: np.ndarray, uncertainty: dict[str, TraceUncertainty], k: float) -> Path:
    """Expanded uncertainty of every point and trace (the charts read it back)."""
    header, columns = ["Frecuencia (Hz)"], [frequencies]
    for name in uncertainty:
        names, values = uncertainty_columns(uncertainty, name, k)
        header += names
        columns += values
    write_csv(path, header, columns)
    return path


def write_results_csv_2p(path: Path, f: np.ndarray, s: np.ndarray, uncertainty: dict[str, TraceUncertainty] | None, k: float) -> Path:
    header = ["Frecuencia (Hz)"]
    columns = [f]
    for name, (i, j) in TWO_PORT:
        header += [f"{name} (dB)", f"{name} (°)"]
        columns += [db(s[:, i, j]), np.degrees(np.angle(s[:, i, j]))]
        names, values = uncertainty_columns(uncertainty, name, k)
        header += names
        columns += values
    write_csv(path, header, columns)
    return path


def write_results_csv_1p(
    path: Path, f: np.ndarray, gamma: np.ndarray, network: str | None, uncertainty: dict[str, TraceUncertainty] | None, k: float
) -> Path:
    z = gamma_to_impedance(gamma)
    names, values = uncertainty_columns(uncertainty, "S11", k)
    header = ["Frecuencia (Hz)", "S11 (dB)", "S11 (°)", *names, "|Z| (Ω)", "fase Z (°)"]
    columns = [f, db(gamma), np.degrees(np.angle(gamma)), *values, np.abs(z), np.degrees(np.angle(z))]
    if network in LISN_NETWORKS:
        nominal = lisn_impedance(network, f)
        header += ["|Z| nominal (Ω)", "fase nominal (°)", "desviación |Z| (%)", "desviación fase (°)"]
        columns += [
            np.abs(nominal),
            np.degrees(np.angle(nominal)),
            100 * (np.abs(z) - np.abs(nominal)) / np.abs(nominal),
            np.degrees(np.angle(z / nominal)),
        ]
    write_csv(path, header, columns)
    return path
