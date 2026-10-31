"""``.sdatcv`` files of METAS VNA Tools: S-parameters with the covariance matrix of each frequency.

«METAS VNA Tools - Data Formats V3.0.2» §4: ASCII text, case-insensitive, entries separated by tabs,
decimal point, comments after ``%``. Six header lines (``SDATCV``, ``Ports``, the ports, the names and
values of the reference impedances, the description of the columns), then a line per frequency: the
frequency in Hz, the S-parameters (real and imaginary parts) and the covariances ``CV[a,b]`` that the
columns name, in the order of the S-parameters by source port (S11 re, S11 im, S21 re, S21 im…, S12 re…).
The missing covariances are completed by symmetry, and the rest are zero.

It keeps less than a ``.sdatb``: no correlation between frequencies nor with other files. When it is read,
each frequency depends on its own inputs (``SParamData.from_covariances``).
"""

from __future__ import annotations

import re
from pathlib import Path

import numpy as np

from calelektra.sdata.model import PortDescription, SParamData
from calelektra.sdata.unclib import SDataError

_S_COLUMN = re.compile(r"S\s*\[\s*(\d+)\s*,\s*(\d+)\s*\]\s*(re|im)", re.IGNORECASE)
_CV_COLUMN = re.compile(r"CV\s*\[\s*(\d+)\s*,\s*(\d+)\s*\]", re.IGNORECASE)
_COLUMN = re.compile(r"Freq|S\s*\[\s*\d+\s*,\s*\d+\s*\]\s*(?:re|im)|CV\s*\[\s*\d+\s*,\s*\d+\s*\]", re.IGNORECASE)


def _numbers(line: str, where: str) -> list[float]:
    try:
        return [float(token) for token in line.split()]
    except ValueError as exc:
        raise SDataError(f"{where}: número no válido ({exc})") from exc


def read_sdatcv(source: str | Path, name: str | None = None) -> SParamData:
    """The data of a ``.sdatcv`` (path, or its text if it has line breaks)."""
    if isinstance(source, Path) or "\n" not in str(source):
        path = Path(source)
        text = path.read_text(encoding="ascii", errors="replace")
        name = path.name if name is None else name
    else:
        text = str(source)
    lines = [line.split("%", 1)[0].strip() for line in text.splitlines()]
    lines = [line for line in lines if line]
    if len(lines) < 6 or lines[0].upper() != "SDATCV" or lines[1].upper() != "PORTS":
        raise SDataError("No es un fichero .sdatcv (faltan las líneas SDATCV y Ports)")
    ports = tuple(PortDescription.parse(token) for token in lines[2].split())
    n = len(ports)
    zr_values = _numbers(lines[4], "Impedancias de referencia")
    if len(zr_values) != 2 * n:
        raise SDataError(f"Hacen falta {2 * n} valores de impedancia de referencia (parte real e imaginaria de cada puerto)")
    zr = np.array(zr_values[0::2]) + 1j * np.array(zr_values[1::2])
    columns = _COLUMN.findall(lines[5])
    if not columns or columns[0].lower() != "freq":
        raise SDataError("La descripción de columnas debe empezar por Freq")
    size = 2 * n * n
    s_columns: dict[int, tuple[int, int, int]] = {}
    cv_columns: dict[int, tuple[int, int]] = {}
    for position, column in enumerate(columns[1:], start=1):
        if match := _S_COLUMN.fullmatch(column):
            receiver, source_port, part = int(match.group(1)) - 1, int(match.group(2)) - 1, match.group(3).lower() == "im"
            if not (0 <= receiver < n and 0 <= source_port < n):
                raise SDataError(f"Columna {column} fuera de los {n} puertos")
            s_columns[position] = (receiver, source_port, int(part))
        elif match := _CV_COLUMN.fullmatch(column):
            a, b = int(match.group(1)) - 1, int(match.group(2)) - 1
            if not (0 <= a < size and 0 <= b < size):
                raise SDataError(f"Columna {column} fuera de la matriz de covarianza ({size} × {size})")
            cv_columns[position] = (a, b)
    if len(s_columns) != size:
        raise SDataError(f"Faltan columnas de parámetros S: hay {len(s_columns)} de {size}")
    frequencies, matrices, covariances = [], [], []
    for row, line in enumerate(lines[6:], start=1):
        values = _numbers(line, f"Frecuencia {row}")
        if len(values) != len(columns):
            raise SDataError(f"Frecuencia {row}: {len(values)} valores en vez de {len(columns)}")
        s = np.zeros((n, n), complex)
        for position, (receiver, source_port, part) in s_columns.items():
            s[receiver, source_port] += values[position] * (1j if part else 1)
        given = np.zeros((size, size), bool)
        covariance = np.zeros((size, size))
        for position, (a, b) in cv_columns.items():
            covariance[a, b] = values[position]
            given[a, b] = True
        mirror = given.T & ~given
        covariance[mirror] = covariance.T[mirror]
        frequencies.append(values[0])
        matrices.append(s)
        covariances.append(covariance)
    if not frequencies:
        raise SDataError("El fichero .sdatcv no tiene datos")
    if np.any(np.diff(frequencies) <= 0):
        raise SDataError("Las frecuencias de un .sdatcv deben ir en orden creciente")
    return SParamData.from_covariances(frequencies, np.array(matrices), covariances, zr, ports, name or "")


def sdatcv_text(data: SParamData) -> str:
    """The ``.sdatcv`` of ``data``, with the complete covariance matrix of each frequency."""
    n, size = data.n_ports, 2 * data.n_ports**2
    order = [(r, c) for c in range(n) for r in range(n)]

    def number(value: float) -> str:
        return repr(float(value))

    lines = ["SDATCV", "Ports", "\t".join(port.label for port in data.ports)]
    lines.append("\t".join(f"Zr[{k + 1}] {part}" for k in range(n) for part in ("re", "im")))
    lines.append("\t".join(number(v) for z in data.zr for v in (z.real, z.imag)))
    header = ["Freq"] + [f"S[{r + 1},{c + 1}] {part}" for r, c in order for part in ("re", "im")]
    header += [f"CV[{a + 1},{b + 1}]" for b in range(size) for a in range(size)]
    lines.append("\t".join(header))
    s = data.s
    for k, frequency in enumerate(data.frequencies):
        row = [number(frequency)] + [number(v) for r, c in order for v in (s[k, r, c].real, s[k, r, c].imag)]
        row += [number(v) for v in data.point_covariance(k).T.ravel()]  # CV[1,1], CV[2,1]…: by columns
        lines.append("\t".join(row))
    return "\r\n".join(lines) + "\r\n"


def write_sdatcv(path: str | Path, data: SParamData) -> Path:
    path = Path(path)
    path.write_bytes(sdatcv_text(data).encode("ascii"))
    return path
