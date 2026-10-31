"""Reading the calibration kit files of Keysight analyzers (``.ckt``).

The format is not documented: it is an archive of MFC (Microsoft Foundation Classes) objects with
Unicode texts, decoded by inspection of a file of version 5 (kit AA180-VNACKN, checked with METAS VNA
Tools). What is read:

* the kit: version (int32), label and description;
* six int32 of unknown meaning and the labels of the classes of standards;
* two int32 and the standards, MFC objects (``CLoadCalStandard``, ``COpenCalStandard``,
  ``CShortCalStandard``, ``CCalStandard8510Model``…): type (1 load, 2 open, 3 short, 4 thru),
  description, label, the connector of each port (name, range, gender: 2 male, 3 female), range,
  number, tables of unknown meaning, the offset line (delay in s, loss in GΩ/s, Z0 in Ω) and the
  termination (C0–C3 in F…, L0–L3 in H…, or the impedance of the load);
* the lists of the classes (0-based indices of the standards), read for information.

Strings: ``FF FE FF``, a length in characters (a byte, or ``FF`` and a 16-bit length) and UTF-16.
New classes: tag ``FFFF``, schema and name; known classes: ``0x8000`` + their index.
"""

from __future__ import annotations

import re
import struct
from dataclasses import dataclass, field
from pathlib import Path

from calelektra.core.connectors import ConnectorError, family_key
from calelektra.core.frequency import FrequencyRange
from calelektra.kits.model import C_UNITS, L_UNITS, KitError, KitStandard, StandardDefinition, default_id

TYPES = {1: "carga", 2: "abierto", 3: "cortocircuito", 4: "through"}
GENDERS = {1: "sin_genero", 2: "macho", 3: "hembra"}
NO_LIMIT_HZ = 999e9  # the maximum frequency of a standard or connector without limit
CLASS_LISTS = 17  # lists of the classes in the files of version 5 (with 19 labels)


@dataclass
class CktConnector:
    name: str
    gender: str
    fmin: float
    fmax: float
    z0: float


@dataclass
class CktStandard:
    kind: str
    number: int
    label: str
    description: str
    connectors: list[CktConnector]
    fmin: float
    fmax: float
    delay_s: float
    loss_gohm_s: float
    z0: float
    coefficients: list[float] = field(default_factory=list)  # SI: F, F/Hz… or H, H/Hz…
    load_impedance: tuple[float, float] | None = None
    tables: list[list[tuple[float, float]]] = field(default_factory=list)  # of unknown meaning


@dataclass
class CktKit:
    version: int
    label: str
    description: str
    standards: list[CktStandard]
    classes: list[tuple[str, list[str]]] = field(default_factory=list)  # class label → labels of its standards

    @property
    def family(self) -> str | None:
        names = {c.name for s in self.standards for c in s.connectors}
        families = {connector_family(name) for name in names}
        families.discard(None)
        return families.pop() if len(families) == 1 else None


def connector_family(name: str) -> str | None:
    """Family of CAL-ELEKTRA of a connector name of the .ckt (``Type N (50)`` → ``N``)."""
    text = name.strip()
    candidates = [text, re.sub(r"^type\s+", "", text, flags=re.IGNORECASE), re.sub(r"\s*\(.*\)$", "", text)]
    candidates.append(re.sub(r"\s*\(.*\)$", "", candidates[1]))
    match = re.search(r"\((\d+)\)", text)
    for candidate in candidates:
        for option in (candidate, f"{candidate}{match.group(1)}" if match and match.group(1) != "50" else None):
            if not option:
                continue
            try:
                return family_key(option)
            except ConnectorError:
                continue
    return None


class _Archive:
    def __init__(self, data: bytes):
        self.data, self.pos = data, 0
        self.classes: dict[int, str] = {}
        self.objects: dict[int, CktStandard] = {}
        self.index = 1  # classes and objects share the index of the archive

    def take(self, n: int) -> bytes:
        if n < 0 or self.pos + n > len(self.data):
            raise KitError(f"Fichero .ckt truncado (byte {self.pos})")
        chunk = self.data[self.pos : self.pos + n]
        self.pos += n
        return chunk

    def i32(self) -> int:
        return struct.unpack("<i", self.take(4))[0]

    def u16(self) -> int:
        return struct.unpack("<H", self.take(2))[0]

    def f64(self) -> float:
        return struct.unpack("<d", self.take(8))[0]

    def text(self) -> str:
        if self.take(3) != b"\xff\xfe\xff":
            raise KitError(f"Fichero .ckt no reconocido: se esperaba un texto en el byte {self.pos - 3}")
        n = self.take(1)[0]
        if n == 0xFF:
            n = self.u16()
        try:
            return self.take(2 * n).decode("utf-16-le")
        except UnicodeDecodeError as exc:
            raise KitError(f"Texto no válido en el byte {self.pos}") from exc

    def peek16(self) -> int:
        if self.pos + 2 > len(self.data):
            raise KitError("Fichero .ckt truncado")
        return struct.unpack_from("<H", self.data, self.pos)[0]

    def class_name(self) -> str:
        tag = self.u16()
        if tag == 0xFFFF:
            self.u16()  # schema
            name = self.take(self.u16()).decode("ascii", errors="replace")
            self.classes[self.index] = name
            self.index += 1
            return name
        if tag & 0x8000 and (tag & 0x7FFF) in self.classes:
            return self.classes[tag & 0x7FFF]
        raise KitError(f"Fichero .ckt no reconocido: etiqueta de objeto {tag:#06x} en el byte {self.pos - 2}")


def _connector(a: _Archive) -> CktConnector:
    a.i32()  # schema (5)
    name, _text = a.text(), a.text()
    fmin, fmax, _unknown = a.f64(), a.f64(), a.f64()
    gender = a.i32()
    z0 = a.f64()
    a.i32(), a.i32()
    for _ in range(8):  # …, 0.5 (height / width)
        a.f64()
    return CktConnector(name, GENDERS.get(gender, "sin_genero"), fmin, fmax, z0)


def _standard(a: _Archive, class_name: str) -> CktStandard:
    a.i32()  # schema (3)
    code = a.i32()
    if code not in TYPES:
        raise KitError(f"El .ckt tiene un patrón de un tipo que no se reconoce ({class_name}, código {code})")
    kind = TYPES[code]
    description, label = a.text(), a.text()
    connectors = [_connector(a) for _ in range(a.i32())]
    fmin, fmax = a.f64(), a.f64()
    number = a.i32()
    r0, r1, c0, c1 = a.i32(), a.i32(), a.i32(), a.i32()
    tables = [[(a.f64(), a.f64()) for _ in range(a.i32())] for _ in range((r1 - r0 + 1) * (c1 - c0 + 1))]
    a.i32()
    for _ in range(a.i32()):  # a block per port: 2 int32 and 6 doubles (50, 0, 0, 50, 0, 0)
        a.i32(), a.i32()
        for _ in range(6):
            a.f64()
    a.i32()  # 0 open / short, 1 load, 5 thru
    a.i32(), a.i32()  # schema of the offset (2, 1)
    delay, loss, z0 = a.f64(), a.f64(), a.f64()
    standard = CktStandard(kind, number, label, description, connectors, fmin, fmax, delay, loss, z0, tables=tables)
    if kind in ("abierto", "cortocircuito"):
        a.i32()
        standard.coefficients = [a.f64() for _ in range(4)]
    elif kind == "carga":
        a.i32(), a.i32(), a.i32()
        standard.load_impedance = (a.f64(), a.f64())
        a.i32()
    return standard


def read_ckt(source: str | Path | bytes) -> CktKit:
    data = source if isinstance(source, bytes) else Path(source).read_bytes()
    a = _Archive(data)
    try:
        version = a.i32()
        label, description = a.text(), a.text()
        for _ in range(6):
            a.i32()
        class_labels = [a.text() for _ in range(19)]
        a.i32(), a.i32()
        standards: list[CktStandard] = []
        while a.pos + 2 <= len(data):
            tag = a.peek16()
            if tag != 0xFFFF and not tag & 0x8000:
                break  # the lists of the classes
            name = a.class_name()
            standard = _standard(a, name)
            a.objects[a.index] = standard
            a.index += 1
            standards.append(standard)
    except (KitError, struct.error) as exc:
        raise KitError(f"No se puede leer el .ckt: {exc}") from exc
    if not standards:
        raise KitError("El .ckt no tiene patrones")
    kit = CktKit(version, label, description, standards)
    try:  # the classes, for information: not needed to define the standards
        for class_label in class_labels[:CLASS_LISTS]:
            indices = [a.i32() for _ in range(a.u16())]
            kit.classes.append((class_label, [standards[i].label for i in indices if 0 <= i < len(standards)]))
    except (KitError, struct.error, IndexError):
        kit.classes = []
    return kit


def _range(standard: CktStandard) -> FrequencyRange | None:
    low = max([standard.fmin] + [c.fmin for c in standard.connectors])
    high = min([standard.fmax] + [c.fmax for c in standard.connectors])
    if low <= 0 and high >= NO_LIMIT_HZ:
        return None
    return FrequencyRange(max(low, 0.0), high)


def _clean(value: float) -> float:
    """12 significant digits: the units of VNA Tools without the noise of the change of units (57.993 ps)."""
    return float(f"{value:.12g}")


def kit_standards(kit: CktKit) -> list[KitStandard]:
    """The standards of the kit with their definition by the model, in the units of VNA Tools."""
    result: list[KitStandard] = []
    taken: set[str] = set()
    for standard in kit.standards:
        genders = [c.gender for c in standard.connectors][: 2 if standard.kind == "through" else 1]
        if len(genders) != (2 if standard.kind == "through" else 1):
            raise KitError(f"El patrón «{standard.label}» del .ckt no tiene los conectores que necesita")
        definition = StandardDefinition(
            kind="modelo",
            offset_z0=_clean(standard.z0),
            offset_delay_ps=_clean(standard.delay_s * 1e12),
            offset_loss=_clean(standard.loss_gohm_s),
        )
        if standard.kind == "abierto":
            definition.capacitance = [_clean(c / unit) for c, unit in zip(standard.coefficients, C_UNITS)]
        elif standard.kind == "cortocircuito":
            definition.inductance = [_clean(coefficient / unit) for coefficient, unit in zip(standard.coefficients, L_UNITS)]
        elif standard.kind == "carga" and standard.load_impedance and tuple(standard.load_impedance) != (standard.z0, 0.0):
            definition.load_impedance = list(standard.load_impedance)
        identifier = default_id(standard.kind, genders, taken)
        taken.add(identifier)
        result.append(
            KitStandard(
                id=identifier,
                type=standard.kind,
                label=standard.label,
                description=standard.description or None,
                genders=genders,
                frequency_range=_range(standard),
                definition=definition,
            )
        )
    return result
