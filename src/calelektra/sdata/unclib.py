"""Uncertain numbers of METAS UncLib (linear propagation, LinProp) and their binary formats.

A LinProp number is a value and its dependencies on inputs: each input has an identifier, a description
and a distribution, and the number depends on it through a sensitivity («Jacobi»). The covariance of
some numbers is V = J · diag(u²) · Jᵀ, with u the standard uncertainty of each input: numbers that share
inputs are correlated, also across files, because the inputs keep their identifier.

Binary formats: «METAS UncLib - Data Formats V3.0.2» §2 and «METAS VNA Tools - Data Formats V3.0.2»
§2.1–2.2 (the same structures). Little-endian, with the primitives of .NET's ``BinaryWriter``: «7-bit
encoded int» (LEB128) and strings as their length in bytes (7-bit encoded int) followed by UTF-8.
"""

from __future__ import annotations

import math
import struct
import uuid
from dataclasses import dataclass, field
from functools import cached_property

import numpy as np


class SDataError(ValueError):
    """A data file with uncertainties that cannot be read or written."""


_INT16 = struct.Struct("<h")
_INT32 = struct.Struct("<i")
_DOUBLE = struct.Struct("<d")
_TWO_DOUBLES = struct.Struct("<2d")
_SMALL = [bytes([value]) for value in range(128)]  # 7-bit encoded ints below 128: one byte


def _int7_bytes(value: int) -> bytes:
    if 0 <= value < 128:
        return _SMALL[value]
    if value < 0 or value > 0x7FFFFFFF:
        raise SDataError(f"Entero fuera de rango para el formato: {value}")
    out = bytearray()
    while value >= 0x80:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value)
    return bytes(out)


class BinaryReader:
    """Reads the primitives of .NET's ``BinaryReader`` from bytes."""

    def __init__(self, data: bytes, position: int = 0):
        self.data = data
        self.position = position

    @property
    def remaining(self) -> int:
        return len(self.data) - self.position

    def take(self, count: int) -> bytes:
        end = self.position + count
        if count < 0 or end > len(self.data):
            raise SDataError(f"Fichero truncado: faltan datos en el byte {self.position}")
        chunk = self.data[self.position : end]
        self.position = end
        return chunk

    def _unpack(self, packer: struct.Struct):
        try:
            value = packer.unpack_from(self.data, self.position)[0]
        except struct.error as exc:
            raise SDataError(f"Fichero truncado: faltan datos en el byte {self.position}") from exc
        self.position += packer.size
        return value

    def int16(self) -> int:
        return self._unpack(_INT16)

    def int32(self) -> int:
        return self._unpack(_INT32)

    def double(self) -> float:
        return self._unpack(_DOUBLE)

    def peek(self) -> int:
        if self.position >= len(self.data):
            raise SDataError(f"Fichero truncado: faltan datos en el byte {self.position}")
        return self.data[self.position]

    def int7(self) -> int:
        result = shift = 0
        data = self.data
        while True:
            if self.position >= len(data):
                raise SDataError(f"Fichero truncado: faltan datos en el byte {self.position}")
            byte = data[self.position]
            self.position += 1
            result |= (byte & 0x7F) << shift
            if byte < 0x80:
                return result
            shift += 7
            if shift > 28:
                raise SDataError(f"Entero de 7 bits no válido en el byte {self.position}")

    def string(self) -> str:
        try:
            return self.take(self.int7()).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SDataError(f"Texto no válido en el byte {self.position}") from exc

    def doubles(self, count: int) -> np.ndarray:
        return np.frombuffer(self.take(8 * count), "<f8").astype(float)


class BinaryWriter:
    """Writes the primitives of .NET's ``BinaryWriter``."""

    def __init__(self):
        self.buffer = bytearray()

    def bytes(self) -> bytes:
        return bytes(self.buffer)

    def raw(self, data: bytes) -> None:
        self.buffer += data

    def int16(self, value: int) -> None:
        self.buffer += _INT16.pack(value)

    def int32(self, value: int) -> None:
        self.buffer += _INT32.pack(value)

    def double(self, value: float) -> None:
        self.buffer += _DOUBLE.pack(value)

    def int7(self, value: int) -> None:
        self.buffer += _int7_bytes(value)

    def string(self, text: str) -> None:
        data = text.encode("utf-8")
        self.int7(len(data))
        self.buffer += data

    def doubles(self, values) -> None:
        self.buffer += np.asarray(values, "<f8").tobytes()


# -- distributions -----------------------------------------------------------------------------------------

# type: name and parameters (doubles, except the k of ChiSquared: int32)
DISTRIBUTIONS = {
    0: ("StandardNormal", ()),
    1: ("Normal", ("mu", "sigma")),
    2: ("StandardUniform", ()),
    3: ("Uniform", ("a", "b")),
    4: ("CurvilinearTrapezoid", ("a", "b", "d")),
    5: ("Trapezoidal", ("a", "b", "beta")),
    6: ("Triangular", ("a", "b")),
    7: ("ArcSine", ("a", "b")),
    8: ("Gamma", ("a", "b")),
    9: ("ChiSquared", ("k",)),
    10: ("StudentT", ("mu", "sigma", "dof")),
    11: ("StudentTFromSamples", ()),
    99: ("RandomChoicesFromSamples", ()),
}


@dataclass(frozen=True, slots=True)
class Distribution:
    """Distribution of an input: its type (``DISTRIBUTIONS``), parameters, samples and seed."""

    kind: int = 0
    params: tuple[float, ...] = ()
    samples: tuple[float, ...] = ()
    seed: bytes = b""

    @property
    def name(self) -> str:
        return DISTRIBUTIONS[self.kind][0]

    def stdunc(self) -> float:
        """Standard uncertainty, as the XML schema of METAS UncLib (§1.2) defines it for each type."""
        p, kind = self.params, self.kind
        if kind == 0:
            return 1.0
        if kind == 1:
            return p[1]
        if kind == 2:
            return 1 / math.sqrt(12)
        if kind in (3, 6, 7):
            return (p[1] - p[0]) / math.sqrt({3: 12, 6: 24, 7: 8}[kind])
        if kind == 4:
            return math.sqrt((p[1] - p[0]) ** 2 / 12 + p[2] ** 2 / 9)
        if kind == 5:
            return (p[1] - p[0]) * math.sqrt((1 + p[2] ** 2) / 24)
        if kind == 8:
            return math.sqrt(p[0]) * p[1]
        if kind == 9:
            return math.sqrt(2 * p[0])
        if kind == 10:  # n = dof + 1
            return p[1] * math.sqrt(p[2] / (p[2] - 2))
        if kind == 11:
            n = len(self.samples)
            return float(np.std(self.samples, ddof=1)) / math.sqrt(n) * math.sqrt((n - 1) / (n - 3))
        return float(np.std(self.samples))  # random choices from samples


STANDARD_NORMAL = Distribution()


def read_distribution(r: BinaryReader) -> Distribution:
    kind = r.int7()
    if kind == 0:
        return STANDARD_NORMAL
    if kind == 1:  # the most common after the standard normal: both parameters at once
        try:
            params = _TWO_DOUBLES.unpack_from(r.data, r.position)
        except struct.error as exc:
            raise SDataError(f"Fichero truncado: faltan datos en el byte {r.position}") from exc
        r.position += 16
        return Distribution(1, params)
    if kind not in DISTRIBUTIONS:
        raise SDataError(f"Distribución desconocida (tipo {kind}) en el byte {r.position}")
    if kind == 9:
        return Distribution(kind, (float(r.int32()),))
    if kind in (11, 99):
        if (version := r.int7()) != 2:
            raise SDataError(f"Versión {version} de {DISTRIBUTIONS[kind][0]} no admitida")
        seed = r.take(r.int7()) if kind == 99 else b""
        return Distribution(kind, samples=tuple(r.doubles(r.int7()).tolist()), seed=seed)
    return Distribution(kind, tuple(r.double() for _ in DISTRIBUTIONS[kind][1]))


def write_distribution(w: BinaryWriter, distribution: Distribution) -> None:
    w.int7(distribution.kind)
    if distribution.kind == 9:
        w.int32(int(distribution.params[0]))
    elif distribution.kind in (11, 99):
        w.int7(2)
        if distribution.kind == 99:
            w.int7(len(distribution.seed))
            w.raw(distribution.seed)
        w.int7(len(distribution.samples))
        w.doubles(distribution.samples)
    else:
        for value in distribution.params:
            w.double(value)


# -- inputs ------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Input:
    """An input of the uncertainty budget.

    ``distribution`` None: an input of the version 1 formats, standard normal, with its «IDof»."""

    id: bytes
    description: str = ""
    distribution: Distribution | None = STANDARD_NORMAL
    idof: float = 0.0

    def stdunc(self) -> float:
        return 1.0 if self.distribution is None else self.distribution.stdunc()


def new_input(description: str = "", distribution: Distribution = STANDARD_NORMAL) -> Input:
    """An input with a new identifier (16 random bytes, as METAS UncLib)."""
    return Input(uuid.uuid4().bytes, description, distribution)


def read_input_distribution(r: BinaryReader) -> Input:
    if (version := r.int7()) != 2:
        raise SDataError(f"Versión {version} de InputDistribution no admitida")
    identifier = r.take(r.int7())
    return Input(identifier, r.string(), read_distribution(r))


def write_input_distribution(w: BinaryWriter, item: Input) -> None:
    w.int7(2)
    w.int7(len(item.id))
    w.raw(item.id)
    w.string(item.description)
    write_distribution(w, STANDARD_NORMAL if item.distribution is None else item.distribution)


# -- single numbers ----------------------------------------------------------------------------------------


@dataclass
class UncNumber:
    """A number with its dependencies: (input, Jacobi)."""

    value: float
    dependencies: list[tuple[Input, float]] = field(default_factory=list)

    def stdunc(self) -> float:
        return math.sqrt(sum((jacobi * item.stdunc()) ** 2 for item, jacobi in self.dependencies))


def read_unc_number(r: BinaryReader) -> UncNumber:
    """UncNumber version 1 (version as int32) or 2 (7-bit encoded int)."""
    if r.peek() == 1:
        r.int32()
        value = r.double()
        if (version2 := r.int32()) != 4:
            raise SDataError(f"UncNumber versión 1 no válido (Version2 = {version2})")
        dependencies = []
        for _ in range(r.int32()):
            identifier = r.take(r.int32())
            description = r.string()
            idof = r.double()
            dependencies.append((Input(identifier, description, None, idof), r.double()))
        return UncNumber(value, dependencies)
    if (version := r.int7()) != 2:
        raise SDataError(f"Versión {version} de UncNumber no admitida")
    value = r.double()
    return UncNumber(value, [(read_input_distribution(r), r.double()) for _ in range(r.int7())])


def write_unc_number(w: BinaryWriter, number: UncNumber) -> None:
    """UncNumber version 2."""
    w.int7(2)
    w.double(number.value)
    w.int7(len(number.dependencies))
    for item, jacobi in number.dependencies:
        write_input_distribution(w, item)
        w.double(jacobi)


def read_complex_number(r: BinaryReader) -> tuple[UncNumber, UncNumber]:
    if (version := r.int32()) != 1:
        raise SDataError(f"Versión {version} de ComplexUncNumber no admitida")
    return read_unc_number(r), read_unc_number(r)


def write_complex_number(w: BinaryWriter, real: UncNumber, imag: UncNumber) -> None:
    w.int32(1)
    write_unc_number(w, real)
    write_unc_number(w, imag)


# -- vectors of numbers sharing a table of inputs ---------------------------------------------------------


@dataclass(eq=False)
class UncVector:
    """Real numbers with their dependencies on a shared table of inputs (METAS «flat vector»).

    The dependencies of element ``e`` are ``indices[offsets[e]:offsets[e + 1]]`` (inputs, ascending) and
    ``jacobians[offsets[e]:offsets[e + 1]]``."""

    values: np.ndarray
    inputs: list[Input]
    offsets: np.ndarray
    indices: np.ndarray
    jacobians: np.ndarray

    def __post_init__(self):
        self.values = np.asarray(self.values, float)
        self.offsets = np.asarray(self.offsets, np.int64)
        self.indices = np.asarray(self.indices, np.int64)
        self.jacobians = np.asarray(self.jacobians, float)
        if len(self.offsets) != len(self.values) + 1 or self.offsets[0] != 0 or self.offsets[-1] != len(self.indices):
            raise SDataError("Vector de incertidumbres incoherente: dependencias y valores no casan")
        if len(self.indices) != len(self.jacobians):
            raise SDataError("Vector de incertidumbres incoherente: índices y sensibilidades no casan")
        if len(self.indices) and (self.indices.min() < 0 or self.indices.max() >= len(self.inputs)):
            raise SDataError("Vector de incertidumbres incoherente: dependencia de una entrada inexistente")

    def __len__(self) -> int:
        return len(self.values)

    @classmethod
    def exact(cls, values) -> UncVector:
        """Values without uncertainty."""
        values = np.asarray(values, float)
        return cls(values, [], np.zeros(len(values) + 1, np.int64), np.zeros(0, np.int64), np.zeros(0))

    @classmethod
    def from_numbers(cls, numbers: list[UncNumber]) -> UncVector:
        """The vector of some numbers: their inputs once (by identifier), in order of appearance."""
        index_of: dict[bytes, int] = {}
        inputs: list[Input] = []
        offsets, indices, jacobians = [0], [], []
        for number in numbers:
            merged: dict[int, float] = {}
            for item, jacobi in number.dependencies:
                if item.id not in index_of:
                    index_of[item.id] = len(inputs)
                    inputs.append(item)
                position = index_of[item.id]
                merged[position] = merged.get(position, 0.0) + jacobi
            for position in sorted(merged):
                indices.append(position)
                jacobians.append(merged[position])
            offsets.append(len(indices))
        return cls(np.array([n.value for n in numbers], float), inputs, offsets, indices, jacobians)

    @classmethod
    def from_covariances(cls, values: np.ndarray, covariances: list[np.ndarray], describe) -> UncVector:
        """Values in consecutive blocks, each with its covariance matrix (``values`` of shape (blocks, m)).

        Each block depends on its own inputs, standard normal: the eigenvectors of its covariance scaled by
        the square root of their eigenvalues (one decomposition per group of correlated values, so that
        uncorrelated values do not share inputs). ``describe(block, contribution)`` names each input.
        Negative eigenvalues, from rounding in the file, count as zero."""
        values = np.asarray(values, float)
        inputs: list[Input] = []
        rows: list[list[tuple[int, float]]] = [[] for _ in range(values.size)]
        m = values.shape[1]
        for block, matrix in enumerate(covariances):
            matrix = np.asarray(matrix, float)
            matrix = (matrix + matrix.T) / 2
            contribution = 0
            for group in _correlated_groups(matrix):
                eigenvalues, eigenvectors = np.linalg.eigh(matrix[np.ix_(group, group)])
                scale = eigenvalues.max(initial=0.0)
                for k in np.flatnonzero(eigenvalues > scale * 1e-15):
                    position = len(inputs)
                    inputs.append(new_input(describe(block, contribution)))
                    contribution += 1
                    column = eigenvectors[:, k] * math.sqrt(eigenvalues[k])
                    for local, jacobi in zip(group, column):
                        if jacobi != 0:
                            rows[block * m + local].append((position, float(jacobi)))
        offsets = np.cumsum([0] + [len(row) for row in rows])
        indices = [position for row in rows for position, _ in row]
        jacobians = [jacobi for row in rows for _, jacobi in row]
        return cls(values.ravel(), inputs, offsets, indices, jacobians)

    def dependencies(self, element: int) -> tuple[np.ndarray, np.ndarray]:
        start, end = self.offsets[element], self.offsets[element + 1]
        return self.indices[start:end], self.jacobians[start:end]

    def number(self, element: int) -> UncNumber:
        indices, jacobians = self.dependencies(element)
        return UncNumber(float(self.values[element]), [(self.inputs[i], float(j)) for i, j in zip(indices, jacobians)])

    @cached_property
    def input_uncertainties(self) -> np.ndarray:
        return np.array([item.stdunc() for item in self.inputs], float)

    def standard_uncertainties(self) -> np.ndarray:
        """Standard uncertainty of each element (an input repeated in an element adds its sensitivities)."""
        owners = np.repeat(np.arange(len(self.values)), np.diff(self.offsets))
        if not len(owners):
            return np.zeros(len(self.values))
        first = np.ones(len(owners), bool)
        first[1:] = (owners[1:] != owners[:-1]) | (self.indices[1:] != self.indices[:-1])
        starts = np.flatnonzero(first)
        terms = (np.add.reduceat(self.jacobians, starts) * self.input_uncertainties[self.indices[starts]]) ** 2
        return np.sqrt(np.bincount(owners[starts], weights=terms, minlength=len(self.values)))

    def covariance(self, elements) -> np.ndarray:
        """Covariance matrix of some elements."""
        elements = np.asarray(elements, np.int64)
        parts = [self.dependencies(e) for e in elements]
        used = np.unique(np.concatenate([indices for indices, _ in parts])) if parts else np.zeros(0, np.int64)
        sensitivities = np.zeros((len(elements), len(used)))
        for row, (indices, jacobians) in enumerate(parts):
            np.add.at(sensitivities[row], np.searchsorted(used, indices), jacobians)
        scaled = sensitivities * self.input_uncertainties[used]
        return scaled @ scaled.T


def cross_covariance(a: UncVector, elements_a, b: UncVector, elements_b) -> np.ndarray:
    """Covariance between elements of two vectors, through the inputs they share (same identifier).

    That of two files of VNA Tools: e.g. a standard and a measurement calibrated with it."""
    index_in_b = {item.id: position for position, item in enumerate(b.inputs)}
    shared_a = np.full(len(a.inputs), -1, np.int64)
    for position, item in enumerate(a.inputs):
        shared_a[position] = index_in_b.get(item.id, -1)
    result = np.zeros((len(elements_a), len(elements_b)))
    for row, element_a in enumerate(elements_a):
        indices, jacobians = a.dependencies(element_a)
        mask = shared_a[indices] >= 0
        if not mask.any():
            continue
        dense = np.zeros(len(b.inputs))
        np.add.at(dense, shared_a[indices[mask]], jacobians[mask] * a.input_uncertainties[indices[mask]])
        for column, element_b in enumerate(elements_b):
            indices_b, jacobians_b = b.dependencies(element_b)
            result[row, column] = float(np.dot(dense[indices_b], jacobians_b * b.input_uncertainties[indices_b]))
    return result


def _correlated_groups(matrix: np.ndarray) -> list[list[int]]:
    """Groups of indices connected by non-zero covariances (the values with variance)."""
    size = len(matrix)
    parent = list(range(size))

    def root(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i, j in zip(*np.nonzero(matrix)):
        parent[root(int(i))] = root(int(j))
    groups: dict[int, list[int]] = {}
    for i in range(size):
        if matrix[i, i] > 0:
            groups.setdefault(root(i), []).append(i)
    return list(groups.values())


def read_flat_vector(r: BinaryReader) -> UncVector:
    """Flat vector of uncertainty numbers, version 1 (int32) or 2 (7-bit encoded int)."""
    version = 1 if r.peek() == 1 else None
    if version == 1:
        r.int32()
    elif (version := r.int7()) != 2:
        raise SDataError(f"Versión {version} de FlatVectorUncNumbers no admitida")
    length = r.int7()
    values = r.doubles(length)
    count = r.int7()
    if version == 2:
        inputs = [read_input_distribution(r) for _ in range(count)]
    else:
        inputs, size = [], 0
        for _ in range(count):
            flags = r.take(1)[0]
            if not flags & 1:  # «same id size»: that of the previous input
                size = r.int7()
            identifier = r.take(size)
            description = "" if flags & 2 else r.string()
            idof = 0.0 if flags & 4 else r.double()
            inputs.append(Input(identifier, description, None, idof))
    offsets, indices, jacobians = _read_dependencies(r, length)
    return UncVector(values, inputs, offsets, indices, jacobians)


def _read_dependencies(r: BinaryReader, length: int):
    """The dependencies of each element: their number, then (pointer relative to the previous, Jacobi)."""
    data, position = r.data, r.position
    unpack = _DOUBLE.unpack_from
    offsets = np.zeros(length + 1, np.int64)
    indices: list[int] = []
    jacobians: list[float] = []
    try:
        for element in range(length):
            count = data[position]
            position += 1
            if count >= 0x80:
                r.position = position - 1
                count = r.int7()
                position = r.position
            index = 0
            for _ in range(count):
                delta = data[position]
                position += 1
                if delta >= 0x80:
                    r.position = position - 1
                    delta = r.int7()
                    position = r.position
                index += delta
                indices.append(index)
                jacobians.append(unpack(data, position)[0])
                position += 8
            offsets[element + 1] = len(indices)
    except (IndexError, struct.error) as exc:
        raise SDataError("Fichero truncado: faltan dependencias de incertidumbre") from exc
    r.position = position
    return offsets, np.array(indices, np.int64), np.array(jacobians, float)


def write_flat_vector(w: BinaryWriter, vector: UncVector) -> None:
    """Flat vector version 2 (inputs with their distribution)."""
    w.int7(2)
    w.int7(len(vector.values))
    w.doubles(vector.values)
    w.int7(len(vector.inputs))
    for item in vector.inputs:
        write_input_distribution(w, item)
    buffer, pack = w.buffer, _DOUBLE.pack
    offsets, indices, jacobians = vector.offsets.tolist(), vector.indices.tolist(), vector.jacobians.tolist()
    for element in range(len(vector.values)):
        start, end = offsets[element], offsets[element + 1]
        buffer += _int7_bytes(end - start)
        previous = 0
        for k in range(start, end):
            index = indices[k]
            if index < previous:
                raise SDataError("Las dependencias de cada elemento deben ir en orden creciente de entrada")
            buffer += _int7_bytes(index - previous)
            buffer += pack(jacobians[k])
            previous = index
