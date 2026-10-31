"""``.sdatb`` files of METAS VNA Tools: S-parameter data with their full uncertainty budget.

«METAS VNA Tools - Data Formats V3.0.2» §2, versions 1 to 5:

* header ``%SDATA`` (string), version (int32), number of frequencies and of ports (int32), frequencies
  (double);
* ports: their number (int32) up to version 2; from version 3, also their mode and index (int16 each);
* frequency conversion of each port: from version 4 (numerator, denominator and offset, doubles); in
  version 5, one for the test receiver, one for the reference receiver and one for the source;
* version 1, compressed with GZIP: the reference impedance of each port and the data as complex
  numbers, each one with its own inputs; from version 2: a flat vector of numbers with a table of
  inputs (``unclib.read_flat_vector``).

Any version may come in a GZIP stream. The writer writes the first version that keeps the data
(``SParamData.required_version``), uncompressed, or version 1 if asked.
"""

from __future__ import annotations

import gzip
from pathlib import Path

from calelektra.sdata.model import ConversionTerm, FrequencyConversion, PortDescription, SParamData
from calelektra.sdata.unclib import (
    BinaryReader,
    BinaryWriter,
    SDataError,
    UncNumber,
    UncVector,
    read_complex_number,
    read_flat_vector,
    write_complex_number,
    write_flat_vector,
)

HEADER = "%SDATA"
VERSIONS = (1, 2, 3, 4, 5)


def _conversion_term(r: BinaryReader) -> ConversionTerm:
    return ConversionTerm(r.double(), r.double(), r.double())


def _write_term(w: BinaryWriter, term: ConversionTerm) -> None:
    w.double(term.numerator)
    w.double(term.denominator)
    w.double(term.offset)


def sdatb_bytes(source: str | Path | bytes) -> bytes:
    """The content of a ``.sdatb``, decompressed if it comes in a GZIP stream."""
    data = source if isinstance(source, bytes) else Path(source).read_bytes()
    if data[:2] == b"\x1f\x8b":
        try:
            data = gzip.decompress(data)
        except (OSError, EOFError) as exc:
            raise SDataError(f"Fichero .sdatb comprimido dañado: {exc}") from exc
    return data


def sdatb_version(source: str | Path | bytes) -> int:
    r = BinaryReader(sdatb_bytes(source))
    if r.string() != HEADER:
        raise SDataError("No es un fichero .sdatb (falta la cabecera %SDATA)")
    return r.int32()


def read_sdatb(source: str | Path | bytes) -> SParamData:
    """The S-parameter data of a ``.sdatb`` file (path or content)."""
    r = BinaryReader(sdatb_bytes(source))
    try:
        header = r.string()
    except SDataError:
        header = ""
    if header != HEADER:
        raise SDataError("No es un fichero .sdatb (falta la cabecera %SDATA)")
    version = r.int32()
    if version not in VERSIONS:
        raise SDataError(f"Versión {version} de .sdatb no admitida (de 1 a 5)")
    points, n = r.int32(), r.int32()
    if points < 0 or n <= 0 or 8 * points > r.remaining:
        raise SDataError(f"Cabecera de .sdatb no válida: {points} frecuencias, {n} puertos")
    frequencies = r.doubles(points)
    if version < 3:
        ports = tuple(PortDescription(r.int32()) for _ in range(n))
    else:
        ports = tuple(PortDescription(r.int32(), r.int16(), r.int16()) for _ in range(n))
    conversions = None
    if version == 4:
        conversions = tuple(FrequencyConversion(*(3 * [_conversion_term(r)])) for _ in range(n))
    elif version == 5:
        conversions = tuple(FrequencyConversion(_conversion_term(r), _conversion_term(r), _conversion_term(r)) for _ in range(n))
    if version == 1:
        numbers: list[UncNumber] = []
        for _ in range(n + points * n * n):
            numbers.extend(read_complex_number(r))
        vector = UncVector.from_numbers(numbers)
    else:
        vector = read_flat_vector(r)
    if r.remaining:
        raise SDataError(f"El fichero .sdatb tiene {r.remaining} bytes de más tras los datos")
    return SParamData(frequencies, ports, vector, conversions)


def sdatb_content(data: SParamData, version: int | None = None) -> bytes:
    """The content of the ``.sdatb`` of ``data`` (by default, the first version that keeps it)."""
    version = data.required_version if version is None else version
    if version not in VERSIONS:
        raise SDataError(f"Versión {version} de .sdatb no admitida (de 1 a 5)")
    if version < data.required_version and not (version == 1 and data.required_version == 2):
        raise SDataError(f"La versión {version} de .sdatb no guarda estos datos (hace falta la {data.required_version})")
    w = BinaryWriter()
    w.string(HEADER)
    w.int32(version)
    w.int32(data.n_points)
    w.int32(data.n_ports)
    w.doubles(data.frequencies)
    for port in data.ports:
        w.int32(port.number)
        if version >= 3:
            w.int16(port.mode)
            w.int16(port.index)
    if version >= 4:
        conversions = data.conversions or tuple(FrequencyConversion() for _ in data.ports)
        for conversion in conversions:
            if version == 4:
                _write_term(w, conversion.test_receiver)
            else:
                for term in (conversion.test_receiver, conversion.reference_receiver, conversion.source):
                    _write_term(w, term)
    if version == 1:
        numbers = [data.vector.number(e) for e in range(len(data.vector))]
        for real, imag in zip(numbers[0::2], numbers[1::2]):
            write_complex_number(w, real, imag)
        return gzip.compress(w.bytes(), mtime=0)
    write_flat_vector(w, data.vector)
    return w.bytes()


def write_sdatb(path: str | Path, data: SParamData, version: int | None = None) -> Path:
    path = Path(path)
    path.write_bytes(sdatb_content(data, version))
    return path

