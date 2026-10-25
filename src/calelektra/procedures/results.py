"""CSV files of the results of a test: the traces as measured (no smoothing) and their uncertainty.

They are written at the end of the run and again when the uncertainty is
recalculated with the ambient conditions of the test. The 1-port impedance
measurement adds the uncertainty of Z, above and below the value (they differ
where the bounds are exact, ``uncertainty/impedance.py``). The transmission test
(S21 → .csv) has no Touchstone file: its CSV is the result and is read back
to recalculate the uncertainty.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from calelektra.core.networks import gamma_to_impedance
from calelektra.uncertainty.impedance import ImpedanceUncertainty
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


def impedance_columns(impedance: ImpedanceUncertainty | None, k: float) -> tuple[list[str], list[np.ndarray]]:
    if impedance is None:
        return [], []
    names = [
        f"U+ |Z| (Ω, k={k:g})",
        f"U− |Z| (Ω, k={k:g})",
        f"U+ |Z| (dBΩ, k={k:g})",
        f"U− |Z| (dBΩ, k={k:g})",
        f"U+ ∠Z (°, k={k:g})",
        f"U− ∠Z (°, k={k:g})",
        "Z: cota exacta (1 = sí)",
    ]
    values = [
        impedance.magnitude_plus,
        impedance.magnitude_minus,
        impedance.magnitude_db_plus,
        impedance.magnitude_db_minus,
        impedance.phase_plus,
        impedance.phase_minus,
        impedance.exact.astype(float),
    ]
    return names, values


def write_uncertainty_csv(
    path: Path, frequencies: np.ndarray, uncertainty: dict[str, TraceUncertainty], k: float, impedance: ImpedanceUncertainty | None = None
) -> Path:
    """Expanded uncertainty of every point and trace (the charts read it back), and that of Z in the impedance measurement."""
    header, columns = ["Frecuencia (Hz)"], [frequencies]
    for name in uncertainty:
        names, values = uncertainty_columns(uncertainty, name, k)
        header += names
        columns += values
    names, values = impedance_columns(impedance, k)
    write_csv(path, header + names, columns + values)
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
    path: Path, f: np.ndarray, gamma: np.ndarray, uncertainty: dict[str, TraceUncertainty] | None, k: float, impedance: ImpedanceUncertainty | None = None
) -> Path:
    z = gamma_to_impedance(gamma)
    names, values = uncertainty_columns(uncertainty, "S11", k)
    z_names, z_values = impedance_columns(impedance, k)
    header = ["Frecuencia (Hz)", "S11 (dB)", "S11 (°)", *names, "|Z| (Ω)", "fase Z (°)", *z_names]
    columns = [f, db(gamma), np.degrees(np.angle(gamma)), *values, np.abs(z), np.degrees(np.angle(z)), *z_values]
    write_csv(path, header, columns)
    return path


def write_results_csv_s21(path: Path, f: np.ndarray, s21: np.ndarray, uncertainty: dict[str, TraceUncertainty] | None, k: float) -> Path:
    """Result of the transmission test: S21 in dB and degrees, with its expanded uncertainty."""
    names, values = uncertainty_columns(uncertainty, "S21", k)
    write_csv(path, ["Frecuencia (Hz)", "S21 (dB)", "S21 (°)", *names], [f, db(s21), np.degrees(np.angle(s21)), *values])
    return path


def read_csv_columns(path: Path) -> tuple[list[str], np.ndarray]:
    """Header and columns (as rows of an array) of a CSV written by :func:`write_csv`."""
    lines = [line for line in path.read_text(encoding="utf-8-sig").splitlines() if line.strip()]
    if len(lines) < 2:
        raise ValueError(f"{path.name}: sin datos")
    header = lines[0].split(";")
    rows = [[float(cell.replace(",", ".")) for cell in line.split(";")] for line in lines[1:]]
    return header, np.array(rows, dtype=float).T


def read_results_csv_s21(path: Path) -> tuple[np.ndarray, np.ndarray]:
    """Frequencies and S21 of a transmission result, as an S matrix (points, 2, 2) with only S21."""
    header, columns = read_csv_columns(path)
    try:
        magnitude, phase = columns[header.index("S21 (dB)")], columns[header.index("S21 (°)")]
    except ValueError as exc:
        raise ValueError(f"{path.name}: no tiene las columnas «S21 (dB)» y «S21 (°)»") from exc
    s = np.full((len(columns[0]), 2, 2), np.nan, dtype=complex)
    s[:, 1, 0] = 10 ** (magnitude / 20) * np.exp(1j * np.radians(phase))
    return columns[0], s


def write_coupler_csv(path: Path, f: np.ndarray, quantities: list, port_return_losses: dict[str, np.ndarray] | None = None) -> Path:
    """Insertion loss, coupling, isolation, directivity and return loss of a directional coupler (dB), and the RL of each port."""
    header, columns = ["Frecuencia (Hz)"], [f]
    for quantity in quantities:
        if quantity.values is not None:
            header.append(f"{quantity.name} {quantity.symbol} (dB)")
            columns.append(quantity.values)
    for port, values in (port_return_losses or {}).items():
        header.append(f"RL {port} (dB)")
        columns.append(values)
    write_csv(path, header, columns)
    return path
