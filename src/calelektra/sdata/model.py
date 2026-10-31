"""S-parameter data with uncertainties: the «S-Parameter Data» of METAS VNA Tools.

Frequencies, the ports, the reference impedance of each port (complex, the same at all frequencies), an
optional frequency conversion per port and the S-matrix of each frequency, all with LinProp uncertainty
(``unclib.UncVector``). The numbers are kept as in a ``.sdatb``: first the real and imaginary parts of
the reference impedance of each port, then those of the data, by frequency, receiver port and source
port (S11, S12, S21, S22 for two ports). ``Data Formats V3.0.2`` §1.1.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import numpy as np

from calelektra.core.touchstone import Touchstone
from calelektra.sdata.unclib import SDataError, UncVector

PORT_MODES = ("", "d", "c")  # single-ended (also «s»), differential mode, common mode
_ROMAN = (("M", 1000), ("CM", 900), ("D", 500), ("CD", 400), ("C", 100), ("XC", 90), ("L", 50), ("XL", 40), ("X", 10), ("IX", 9), ("V", 5), ("IV", 4), ("I", 1))


def roman(number: int) -> str:
    text = ""
    for letters, value in _ROMAN:
        while number >= value:
            text += letters
            number -= value
    return text


def from_roman(text: str) -> int:
    number, rest = 0, text.upper()
    for letters, value in _ROMAN:
        while rest.startswith(letters):
            number += value
            rest = rest[len(letters) :]
    if rest or not text:
        raise SDataError(f"Índice de puerto no válido: {text!r}")
    return number


@dataclass(frozen=True)
class PortDescription:
    """A VNA port: its number, its mode (0 single-ended, 1 differential, 2 common) and its index (0: none)."""

    number: int
    mode: int = 0
    index: int = 0

    @property
    def label(self) -> str:
        """As VNA Tools writes it: ``1``, ``2d``, ``1:II``…"""
        return f"{self.number}{PORT_MODES[self.mode]}" + (f":{roman(self.index)}" if self.index else "")

    @property
    def plain(self) -> bool:
        return self.mode == 0 and self.index == 0

    @classmethod
    def parse(cls, text: str) -> PortDescription:
        match = re.fullmatch(r"\s*(\d+)\s*([sdc]?)\s*(?::?\s*([IVXLCDM]+))?\s*", text, re.IGNORECASE)
        if not match:
            raise SDataError(f"Descripción de puerto no válida: {text!r}")
        mode = {"": 0, "s": 0, "d": 1, "c": 2}[match.group(2).lower()]
        return cls(int(match.group(1)), mode, from_roman(match.group(3)) if match.group(3) else 0)


@dataclass(frozen=True)
class ConversionTerm:
    """f = numerator / denominator · f_list + offset."""

    numerator: float = 1.0
    denominator: float = 1.0
    offset: float = 0.0

    def apply(self, frequencies: np.ndarray) -> np.ndarray:
        return self.numerator / self.denominator * np.asarray(frequencies) + self.offset


@dataclass(frozen=True)
class FrequencyConversion:
    """Frequency conversion of a port: of its test receiver, its reference receiver and its source."""

    test_receiver: ConversionTerm = ConversionTerm()
    reference_receiver: ConversionTerm = ConversionTerm()
    source: ConversionTerm = ConversionTerm()

    @property
    def uniform(self) -> bool:
        """The same for the receivers and the source (what a version 4 file can keep)."""
        return self.test_receiver == self.reference_receiver == self.source


@dataclass(eq=False)
class SParamData:
    frequencies: np.ndarray  # Hz
    ports: tuple[PortDescription, ...]
    vector: UncVector  # Zr of each port (re, im), then the data by frequency, receiver and source (re, im)
    conversions: tuple[FrequencyConversion, ...] | None = None

    def __post_init__(self):
        self.frequencies = np.asarray(self.frequencies, float)
        self.ports = tuple(self.ports)
        n, points = len(self.ports), len(self.frequencies)
        if len(self.vector) != 2 * n + 2 * points * n * n:
            raise SDataError(f"Los datos no casan con {points} frecuencias y {n} puertos")
        if self.conversions is not None and len(self.conversions) != n:
            raise SDataError("Hace falta una conversión de frecuencia por puerto")

    @property
    def n_ports(self) -> int:
        return len(self.ports)

    @property
    def n_points(self) -> int:
        return len(self.frequencies)

    @property
    def zr(self) -> np.ndarray:
        """Reference impedance of each port (Ω)."""
        values = self.vector.values[: 2 * self.n_ports]
        return values[0::2] + 1j * values[1::2]

    @property
    def s(self) -> np.ndarray:
        """S-matrices, shape (frequencies, receiver port, source port): ``s[:, 1, 0]`` is S21."""
        values = self.vector.values[2 * self.n_ports :]
        return (values[0::2] + 1j * values[1::2]).reshape(self.n_points, self.n_ports, self.n_ports)

    def element(self, point: int, receiver: int, source: int, part: int = 0) -> int:
        """Position in ``vector`` of the real (``part`` 0) or imaginary (1) part of S[receiver, source] (0-based)."""
        n = self.n_ports
        return 2 * n + 2 * ((point * n + receiver) * n + source) + part

    def standard_uncertainty(self) -> tuple[np.ndarray, np.ndarray]:
        """Standard uncertainty of the real and of the imaginary parts, shape (frequencies, receiver, source)."""
        u = self.vector.standard_uncertainties()[2 * self.n_ports :]
        shape = (self.n_points, self.n_ports, self.n_ports)
        return u[0::2].reshape(shape), u[1::2].reshape(shape)

    def point_elements(self, point: int) -> np.ndarray:
        """The elements of a frequency in the order of a ``.sdatcv``: S11 re, S11 im, S21 re… (by source port)."""
        n = self.n_ports
        return np.array([self.element(point, r, c, part) for c in range(n) for r in range(n) for part in (0, 1)])

    def point_covariance(self, point: int) -> np.ndarray:
        """Covariance of the S-parameters of a frequency, in the order of ``point_elements``."""
        return self.vector.covariance(self.point_elements(point))

    @property
    def required_version(self) -> int:
        """The first ``.sdatb`` version that keeps these data: 2, 3 (port modes), 4 or 5 (frequency conversion)."""
        if self.conversions is not None:
            return 4 if all(c.uniform for c in self.conversions) else 5
        return 2 if all(port.plain for port in self.ports) else 3

    def to_touchstone(self, comments: list[str] | None = None) -> Touchstone:
        """The values, without uncertainty: Touchstone needs the same real reference impedance at all ports."""
        zr = self.zr
        if np.any(zr.imag != 0) or np.any(zr.real != zr.real[0]):
            raise SDataError(f"Touchstone no admite estas impedancias de referencia: {', '.join(f'{z:g}' for z in zr)}")
        if self.conversions is not None or not all(port.plain for port in self.ports):
            raise SDataError("Touchstone no admite puertos diferenciales ni conversión de frecuencia")
        return Touchstone(self.n_ports, self.frequencies.copy(), self.s.copy(), float(zr.real[0]), list(comments or []))

    @classmethod
    def from_values(cls, frequencies, s, zr=50.0, ports=None) -> SParamData:
        """Data without uncertainty: ``s`` of shape (frequencies, receiver, source)."""
        s = np.asarray(s, complex)
        n = s.shape[1]
        zr = np.broadcast_to(np.asarray(zr, complex), (n,))
        values = np.concatenate([np.column_stack([zr.real, zr.imag]).ravel(), np.column_stack([s.real.ravel(), s.imag.ravel()]).ravel()])
        return cls(frequencies, ports or tuple(PortDescription(k + 1) for k in range(n)), UncVector.exact(values))

    @classmethod
    def from_touchstone(cls, data: Touchstone) -> SParamData:
        return cls.from_values(data.frequencies, data.s, data.reference_impedance)

    @classmethod
    def from_covariances(cls, frequencies, s, covariances, zr=50.0, ports=None, name: str = "") -> SParamData:
        """Data with the covariance of the S-parameters of each frequency (order of ``point_elements``).

        Each frequency depends on its own inputs (no correlation between frequencies, as in a ``.sdatcv``),
        named as VNA Tools names them: ``Data Set <name> Frequency Index: k, Contribution: c``."""
        s = np.asarray(s, complex)
        points, n = s.shape[0], s.shape[1]
        order = [(r, c) for c in range(n) for r in range(n)]  # order of the covariance matrix
        blocks = np.array([[part for r, c in order for part in (s[k, r, c].real, s[k, r, c].imag)] for k in range(points)])
        blocks_vector = UncVector.from_covariances(blocks, covariances, lambda k, c: f"Data Set\t{name}\tFrequency Index: {k}, Contribution: {c}")
        data = cls.from_values(frequencies, s, zr, ports)
        # from the order of the covariance matrix to that of the data (by receiver, then source)
        permutation = np.empty(len(data.vector), np.int64)
        permutation[: 2 * n] = -1
        for k in range(points):
            for position, element in enumerate(data.point_elements(k)):
                permutation[element] = k * 2 * n * n + position
        rows = [blocks_vector.dependencies(p) if p >= 0 else (np.zeros(0, np.int64), np.zeros(0)) for p in permutation]
        offsets = np.cumsum([0] + [len(indices) for indices, _ in rows])
        indices = np.concatenate([indices for indices, _ in rows]) if rows else np.zeros(0, np.int64)
        jacobians = np.concatenate([jacobians for _, jacobians in rows]) if rows else np.zeros(0)
        return cls(data.frequencies, data.ports, UncVector(data.vector.values, blocks_vector.inputs, offsets, indices, jacobians))
