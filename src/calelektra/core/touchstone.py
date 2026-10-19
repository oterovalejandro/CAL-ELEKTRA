"""Reading and writing Touchstone files (``.s1p``, ``.s2p``, … ``.sNp``).

Supports Touchstone 1.x and the basic Touchstone 2.0 keywords for S-parameter
data in RI, MA and DB formats. Only what the hardware setups need: frequency
range, number of ports and the complex S-matrix per frequency point.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from calelektra.core.frequency import FrequencyRange

_FREQ_UNITS = {"HZ": 1.0, "KHZ": 1e3, "MHZ": 1e6, "GHZ": 1e9}


class TouchstoneError(ValueError):
    pass


@dataclass
class Touchstone:
    n_ports: int
    frequencies: np.ndarray  # Hz, ascending
    s: np.ndarray  # complex, shape (points, n_ports, n_ports)
    reference_impedance: float = 50.0
    comments: list[str] = field(default_factory=list)
    path: Path | None = None

    @property
    def frequency_range(self) -> FrequencyRange:
        return FrequencyRange(float(self.frequencies[0]), float(self.frequencies[-1]))

    def s_param(self, name: str) -> np.ndarray:
        """Complex trace of a parameter named ``"S21"`` (1-based indices)."""
        match = re.fullmatch(r"[Ss](\d)(\d)", name)
        if not match:
            raise TouchstoneError(f"Parámetro S no válido: {name!r}")
        i, j = int(match.group(1)) - 1, int(match.group(2)) - 1
        if not (0 <= i < self.n_ports and 0 <= j < self.n_ports):
            raise TouchstoneError(f"{name} no existe en un fichero de {self.n_ports} puertos")
        return self.s[:, i, j]

    def s_db(self, name: str) -> np.ndarray:
        with np.errstate(divide="ignore"):
            return 20 * np.log10(np.abs(self.s_param(name)))


def _ports_from_name(path: Path) -> int | None:
    match = re.search(r"\.s(\d+)p$", path.name, re.IGNORECASE)
    return int(match.group(1)) if match else None


def _pair_to_complex(a: np.ndarray, b: np.ndarray, data_format: str) -> np.ndarray:
    if data_format == "RI":
        return a + 1j * b
    magnitude = a if data_format == "MA" else 10 ** (a / 20)
    return magnitude * np.exp(1j * np.deg2rad(b))


def read_touchstone(path: str | Path) -> Touchstone:
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise TouchstoneError(f"No se puede leer {path}: {exc}") from exc

    n_ports = _ports_from_name(path)
    unit, data_format, z0 = "GHZ", "MA", 50.0
    two_port_order = "21_12"
    comments: list[str] = []
    tokens: list[float] = []
    option_seen = False

    for raw_line in text.splitlines():
        line, _, comment = raw_line.partition("!")
        if comment.strip() and not line.strip():
            comments.append(comment.strip())
        line = line.strip()
        if not line:
            continue
        if line.startswith("#"):
            if option_seen:
                continue  # only the first option line counts
            option_seen = True
            parts = line[1:].upper().split()
            k = 0
            while k < len(parts):
                part = parts[k]
                if part in _FREQ_UNITS:
                    unit = part
                elif part in ("RI", "MA", "DB"):
                    data_format = part
                elif part == "R" and k + 1 < len(parts):
                    z0 = float(parts[k + 1])
                    k += 1
                elif part != "S":
                    raise TouchstoneError(f"{path.name}: solo se admiten parámetros S (encontrado {part!r})")
                k += 1
            continue
        if line.startswith("["):
            keyword, _, value = line.partition("]")
            keyword = keyword[1:].strip().lower()
            value = value.strip()
            if keyword == "number of ports":
                n_ports = int(value)
            elif keyword == "two-port data order":
                two_port_order = value
            elif keyword == "end":
                break
            continue
        try:
            tokens.extend(float(tok) for tok in line.split())
        except ValueError as exc:
            raise TouchstoneError(f"{path.name}: línea de datos no válida: {raw_line!r}") from exc

    if not n_ports:
        raise TouchstoneError(f"{path.name}: no se puede determinar el número de puertos (extensión .sNp)")
    per_point = 1 + 2 * n_ports * n_ports
    frequencies: list[float] = []
    rows: list[list[float]] = []
    position = 0
    while position + per_point <= len(tokens):
        freq = tokens[position]
        if frequencies and freq <= frequencies[-1]:
            break  # 2-port noise parameters follow the S-data in Touchstone 1.x
        frequencies.append(freq)
        rows.append(tokens[position + 1 : position + per_point])
        position += per_point
    if not frequencies:
        raise TouchstoneError(f"{path.name}: no contiene datos")

    values = np.asarray(rows, dtype=float)
    complex_values = _pair_to_complex(values[:, 0::2], values[:, 1::2], data_format)
    if n_ports == 2 and two_port_order == "21_12":
        s = complex_values[:, [0, 2, 1, 3]].reshape(-1, 2, 2)  # file order: 11 21 12 22
    else:
        s = complex_values.reshape(-1, n_ports, n_ports)  # row-major
    return Touchstone(
        n_ports=n_ports,
        frequencies=np.asarray(frequencies) * _FREQ_UNITS[unit],
        s=s,
        reference_impedance=z0,
        comments=comments,
        path=path,
    )


def _format_pair(value: complex, data_format: str) -> str:
    if data_format == "RI":
        return f"{value.real:.9g} {value.imag:.9g}"
    angle = np.degrees(np.angle(value))
    if data_format == "MA":
        return f"{abs(value):.9g} {angle:.6f}"
    with np.errstate(divide="ignore"):
        magnitude_db = 20 * np.log10(abs(value)) if value != 0 else -400.0
    return f"{magnitude_db:.6f} {angle:.6f}"


def write_touchstone(path: str | Path, data: Touchstone, data_format: str = "RI", unit: str = "HZ") -> None:
    """Write Touchstone 1.1 (2-port order 11 21 12 22) in RI, MA or DB with the given frequency unit."""
    path = Path(path)
    data_format, unit = data_format.upper(), unit.upper()
    if data_format not in ("RI", "MA", "DB"):
        raise TouchstoneError(f"Formato Touchstone no válido: {data_format}")
    if unit not in _FREQ_UNITS:
        raise TouchstoneError(f"Unidad de frecuencia no válida: {unit}")
    factor = _FREQ_UNITS[unit]
    lines = [f"! {comment}" for comment in data.comments]
    lines.append(f"# {unit} S {data_format} R {data.reference_impedance:g}")
    for k, freq in enumerate(data.frequencies):
        matrix = data.s[k]
        if data.n_ports == 2:
            ordered = [matrix[0, 0], matrix[1, 0], matrix[0, 1], matrix[1, 1]]
        else:
            ordered = list(matrix.reshape(-1))
        numbers = " ".join(_format_pair(complex(value), data_format) for value in ordered)
        lines.append(f"{freq / factor:.12g} {numbers}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
