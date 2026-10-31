"""The S-parameters of the standards of a kit, with their uncertainty, and their files.

``ResolvedStandard`` joins what the kit model says of a standard (``KitStandard``) and what a unit of the
kit says of it (``UnitStandard``: serial number, state, its own data or specifications).
``standard_data`` gives its S-parameters with their uncertainty:

* model: computed on the frequencies of the kit (``kit_frequencies``) with the reference impedance of
  the kit, plus the uncertainty of the specifications;
* data from a ``.sdatb``: its values and its own uncertainty, on its frequencies;
* data from a Touchstone file: its values plus the uncertainty of the specifications.

``export_sdatb`` writes them as ``.sdatb``, named as VNA Tools names the standards.
"""

from __future__ import annotations

import cmath
import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from calelektra.core.frequency import FrequencyRange
from calelektra.kits.agilent import model_s
from calelektra.kits.model import KitError, KitStandard, SpecRow, StandardDefinition, UnitStandard, standard_name
from calelektra.kits.specs import with_specifications
from calelektra.sdata.files import load_sparam_data
from calelektra.sdata.model import SParamData
from calelektra.sdata.sdatb import write_sdatb
from calelektra.sdata.unclib import SDataError, UncVector

DEFAULT_POINTS = 1800
DEFAULT_STOP_HZ = 18e9


@dataclass
class KitSettings:
    """What the kit gives to all its standards: name, reference impedance, range and points."""

    name: str
    zr: complex = 50.0
    frequency_range: FrequencyRange | None = None
    points: int = DEFAULT_POINTS


@dataclass
class ResolvedStandard:
    standard: KitStandard
    unit: UnitStandard | None
    model_folder: Path  # the files of the model are relative to its YAML
    unit_folder: Path | None = None

    @property
    def id(self) -> str:
        return self.standard.id

    @property
    def serial_number(self) -> str | None:
        return self.unit.serial_number if self.unit else None

    @property
    def state(self) -> str:
        return self.unit.state if self.unit else "en_uso"

    @property
    def usable(self) -> bool:
        return self.state != "danado"

    @property
    def own_definition(self) -> bool:
        return bool(self.unit and self.unit.definition is not None)

    @property
    def definition(self) -> StandardDefinition:
        return self.unit.definition if self.own_definition else self.standard.definition  # type: ignore[union-attr]

    @property
    def specifications(self) -> list[SpecRow]:
        if self.unit and self.unit.specifications is not None:
            return self.unit.specifications
        return self.standard.specifications

    @property
    def folder(self) -> Path:
        """Folder of the YAML that refers to the file of the active definition."""
        return self.unit_folder if self.own_definition and self.unit_folder else self.model_folder

    def name(self, kit: str) -> str:
        return standard_name(self.standard, self.serial_number, kit)

    def data_path(self) -> Path | None:
        definition = self.definition
        if definition.kind != "datos" or not definition.file:
            return None
        path = Path(definition.file)
        return path if path.is_absolute() else (self.folder / path).resolve()


def resolve_standards(standards: list[KitStandard], units: dict[str, UnitStandard], model_folder: Path, unit_folder: Path | None) -> list[ResolvedStandard]:
    return [ResolvedStandard(s, units.get(s.id), model_folder, unit_folder) for s in standards]


def kit_frequencies(settings: KitSettings, standard_range: FrequencyRange | None = None) -> np.ndarray:
    """``points`` frequencies from the start to the end of the range of the kit (and of the standard).

    The model is not defined at DC: a range from DC starts one step above (DC – 18 GHz with 1800
    points: 10 MHz, 20 MHz… 18 GHz)."""
    span = settings.frequency_range or FrequencyRange(0.0, DEFAULT_STOP_HZ)
    if standard_range is not None:
        span = span.intersect(standard_range) or standard_range
    points = max(int(settings.points), 2)
    start, stop = span.start, span.stop
    if stop <= 0:
        raise KitError("El rango de frecuencia del kit no puede acabar en DC")
    if start <= 0:
        return np.linspace(stop / points, stop, points)
    return np.linspace(start, stop, points)


def select_ports(data: SParamData, ports: list[int]) -> SParamData:
    """The S-parameters of some ports of the file (by their number); empty: all."""
    if not ports:
        return data
    numbers = [p.number for p in data.ports]
    missing = [p for p in ports if p not in numbers]
    if missing:
        raise KitError(f"El fichero no tiene el puerto {', '.join(map(str, missing))} (tiene {', '.join(map(str, numbers))})")
    chosen = [numbers.index(p) for p in ports]
    elements = [2 * i + part for i in chosen for part in (0, 1)]  # Zr, then by frequency, receiver and source
    for k in range(data.n_points):
        for r in chosen:
            for c in chosen:
                elements += [data.element(k, r, c, 0), data.element(k, r, c, 1)]
    return SParamData(data.frequencies, [data.ports[i] for i in chosen], subset(data.vector, elements))


def subset(vector: UncVector, elements: list[int]) -> UncVector:
    """Some elements of a vector, with only the inputs they use."""
    rows = [vector.dependencies(e) for e in elements]
    used = sorted({int(i) for indices, _ in rows for i in indices})
    position = {old: new for new, old in enumerate(used)}
    offsets = np.cumsum([0] + [len(indices) for indices, _ in rows])
    indices = [position[int(i)] for indices_, _ in rows for i in indices_]
    jacobians = [float(j) for _, jacobians_ in rows for j in jacobians_]
    return UncVector(vector.values[elements], [vector.inputs[i] for i in used], offsets, indices, jacobians)


def standard_data(resolved: ResolvedStandard, settings: KitSettings, loader=load_sparam_data) -> SParamData:
    """S-parameters of the standard with their uncertainty (k = 1 in the budget)."""
    standard, definition = resolved.standard, resolved.definition
    name = f"{settings.name} {standard.label or standard.id}".strip()
    if definition.kind == "ninguna":
        raise KitError(f"«{standard.label or standard.id}» no tiene definición")
    if definition.kind == "modelo":
        f = kit_frequencies(settings, standard.frequency_range)
        data = SParamData.from_values(f, model_s(standard, f, settings.zr, definition), settings.zr)
        return with_specifications(data, standard.type, resolved.specifications, name)
    path = resolved.data_path()
    if path is None or not path.is_file():
        raise KitError(f"«{standard.label or standard.id}»: no se encuentra el fichero de datos {definition.file}")
    try:
        data = select_ports(loader(path), definition.ports)
    except SDataError as exc:
        raise KitError(f"«{standard.label or standard.id}»: {exc}") from exc
    if data.n_ports != standard.ports:
        raise KitError(
            f"«{standard.label or standard.id}» es de {standard.ports} puerto{'s' if standard.ports > 1 else ''} y el fichero "
            f"{path.name} de {data.n_ports}: elija los puertos que se usan"
        )
    if definition.is_touchstone:
        data = with_specifications(data, standard.type, resolved.specifications, name)
    return data


def export_sdatb(standards: list[ResolvedStandard], settings: KitSettings, folder: Path, loader=load_sparam_data) -> list[Path]:
    """A ``.sdatb`` per standard with definition, named as VNA Tools names them; returns the files written."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    written = []
    for resolved in standards:
        if resolved.definition.kind == "ninguna":
            continue
        data = standard_data(resolved, settings, loader)
        written.append(write_sdatb(folder / f"{resolved.name(settings.name)}.sdatb", data))
    return written


@dataclass
class TableRow:
    """A frequency of a parameter: value, its magnitude and phase, and their expanded uncertainty."""

    frequency: float
    parameter: str
    value: complex
    u_real: float
    u_imag: float
    u_magnitude: float
    u_phase_deg: float  # NaN where the magnitude is 0


def table_rows(data: SParamData, coverage_factor: float = 2.0) -> list[TableRow]:
    """The table of a standard: every frequency and parameter with U = k·u of each quantity."""
    n = data.n_ports
    order = [(r, c) for c in range(n) for r in range(n)]  # order of point_covariance
    rows = []
    s = data.s
    for k, frequency in enumerate(data.frequencies):
        covariance = data.point_covariance(k)
        for position, (r, c) in enumerate(order):
            cov = covariance[2 * position : 2 * position + 2, 2 * position : 2 * position + 2]
            value = complex(s[k, r, c])
            magnitude = abs(value)
            if magnitude > 0:
                g = np.array([value.real, value.imag]) / magnitude
                h = np.array([-value.imag, value.real]) / magnitude**2
                floor = 1e-14 * (cov[0, 0] + cov[1, 1])  # rounding: a deviation only in phase has no radial part
                u_mag = math.sqrt(max(float(g @ cov @ g), 0.0)) if float(g @ cov @ g) > floor else 0.0
                u_phase = math.degrees(math.sqrt(max(float(h @ cov @ h), 0.0))) if float(h @ cov @ h) * magnitude**2 > floor else 0.0
            else:
                u_mag, u_phase = math.sqrt(max((cov[0, 0] + cov[1, 1]) / 2, 0.0)), math.nan
            rows.append(
                TableRow(
                    float(frequency),
                    f"S{r + 1}{c + 1}",
                    value,
                    coverage_factor * math.sqrt(max(cov[0, 0], 0.0)),
                    coverage_factor * math.sqrt(max(cov[1, 1], 0.0)),
                    coverage_factor * u_mag,
                    coverage_factor * u_phase,
                )
            )
    return rows


def polar(value: complex) -> tuple[float, float]:
    return abs(value), math.degrees(cmath.phase(value))
