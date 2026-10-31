"""The standards of a calibration kit: what each one is, how it is defined and its electrical specifications.

As in METAS VNA Tools (manual §4.6), a standard is defined:

* by the model of Keysight («Agilent model», ``clase: modelo``): an offset line (Z0, delay, loss) ended
  by the capacitance C0–C3 of an open, the inductance L0–L3 of a short or a load; a through is the line;
* by data (``clase: datos``): its S-parameters in a ``.sdatb`` (with their uncertainty) or a Touchstone
  ``.sNp`` file (values only);
* or not at all (``clase: ninguna``): the adapters of a kit, unknown throughs of the UOSM.

The electrical specifications (``especificaciones``, k = 2, a row per frequency) give the uncertainty of
a model or of a Touchstone file (``kits/specs.py``).

The kit model lists its standards with their nominal definition (``EquipmentModel.patrones``); each unit
of the kit gives the serial number and the state of its standards and, if its certificate characterizes
them, its own data or specifications (``Equipment.patrones``, ``UnitStandard``).
"""

from __future__ import annotations

from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from calelektra.core.connectors import GENDERS
from calelektra.core.frequency import FrequencyRange
from calelektra.core.units import UnitError, format_frequency, parse_frequency

StandardType = Literal["abierto", "cortocircuito", "carga", "through", "adaptador"]
TYPE_NAMES = {
    "abierto": "Abierto",
    "cortocircuito": "Cortocircuito",
    "carga": "Carga",
    "through": "Through (delay/thru)",
    "adaptador": "Adaptador (through desconocido)",
}
VNA_TOOLS_TYPES = {"abierto": "Open", "cortocircuito": "Short", "carga": "Load", "through": "Thru", "adaptador": "Adapter"}
TWO_PORT_TYPES = frozenset({"through", "adaptador"})
DEFINITION_NAMES = {"modelo": "Modelo (Agilent)", "datos": "Datos", "ninguna": "Sin definición"}
STATE_NAMES = {"en_uso": "En uso", "danado": "Dañado", "sustituido": "Sustituido"}
GENDER_LETTERS = {"macho": "m", "hembra": "f", "sin_genero": "x"}
# columns of the electrical specifications of each type (VNA Tools §4.6.1), k = 2
SPEC_COLUMNS = {
    "abierto": ("modulo_db", "fase_grados"),
    "cortocircuito": ("modulo_db", "fase_grados"),
    "carga": ("perdidas_retorno_db",),
    "through": ("perdidas_retorno_db", "transmision_modulo_db", "transmision_fase_grados"),
    "adaptador": (),
}
SPEC_NAMES = {
    "modulo_db": "Desviación de módulo (dB)",
    "fase_grados": "Desviación de fase (°)",
    "perdidas_retorno_db": "Pérdidas de retorno (dB)",
    "transmision_modulo_db": "Desviación de módulo de la transmisión (dB)",
    "transmision_fase_grados": "Desviación de fase de la transmisión (°)",
}
# units of the polynomial coefficients, as VNA Tools enters them
C_UNITS = (1e-15, 1e-27, 1e-36, 1e-45)  # F, F/Hz, F/Hz², F/Hz³
L_UNITS = (1e-12, 1e-24, 1e-33, 1e-42)  # H, H/Hz, H/Hz², H/Hz³


class KitError(ValueError):
    """A standard that cannot be defined or computed."""


def _frequency(value: Any) -> float:
    try:
        return parse_frequency(value)
    except UnitError as exc:
        raise ValueError(str(exc)) from exc


class SpecRow(BaseModel):
    """A row of the electrical specifications: values with k = 2 from a frequency on (linear in between)."""

    model_config = ConfigDict(populate_by_name=True)

    frequency: float = Field(alias="frecuencia")
    magnitude_db: float | None = Field(default=None, alias="modulo_db", ge=0)
    phase_deg: float | None = Field(default=None, alias="fase_grados", ge=0)
    return_loss_db: float | None = Field(default=None, alias="perdidas_retorno_db")
    transmission_db: float | None = Field(default=None, alias="transmision_modulo_db", ge=0)
    transmission_deg: float | None = Field(default=None, alias="transmision_fase_grados", ge=0)

    @field_validator("frequency", mode="before")
    @classmethod
    def _parse_frequency(cls, value: Any) -> float:
        return _frequency(value)

    @field_validator("return_loss_db")
    @classmethod
    def _return_loss(cls, value: float | None) -> float | None:
        """Written as VNA Tools does (−40 dB) or as a positive return loss (40 dB): kept negative."""
        return None if value is None else -abs(value)

    def value(self, column: str) -> float | None:
        return getattr(self, {
            "modulo_db": "magnitude_db",
            "fase_grados": "phase_deg",
            "perdidas_retorno_db": "return_loss_db",
            "transmision_modulo_db": "transmission_db",
            "transmision_fase_grados": "transmission_deg",
        }[column])

    def to_yaml(self) -> dict[str, Any]:
        data = self.model_dump(by_alias=True, exclude_none=True)
        data["frecuencia"] = format_frequency(self.frequency, decimal=".")
        return data


class StandardDefinition(BaseModel):
    """How a standard is defined: the model of Keysight, data of a file, or nothing."""

    model_config = ConfigDict(populate_by_name=True, extra="allow")

    kind: Literal["modelo", "datos", "ninguna"] = Field(default="ninguna", alias="clase")
    # model (Agilent): offset line and termination
    offset_z0: float = Field(default=50.0, alias="z0_offset_ohm", gt=0)
    offset_delay_ps: float = Field(default=0.0, alias="retardo_offset_ps")
    offset_loss: float = Field(default=0.0, alias="perdidas_offset_gohm_s", ge=0)  # GΩ/s
    capacitance: list[float] = Field(default_factory=lambda: [0.0, 0.0, 0.0, 0.0], alias="c")  # C0–C3, units C_UNITS
    inductance: list[float] = Field(default_factory=lambda: [0.0, 0.0, 0.0, 0.0], alias="l")  # L0–L3, units L_UNITS
    load_impedance: list[float] | None = Field(default=None, alias="impedancia_carga_ohm")  # R, X; by default the offset Z0
    media: Literal["coaxial", "guia"] = Field(default="coaxial", alias="medio")
    cutoff_hz: float | None = Field(default=None, alias="frecuencia_corte")
    height_width_ratio: float = Field(default=0.5, alias="relacion_alto_ancho", gt=0)
    # data: a .sdatb or .sNp file, relative to the YAML that refers to it
    file: str | None = Field(default=None, alias="fichero")
    ports: list[int] = Field(default_factory=list, alias="puertos")  # ports of the file that are used (empty: all)
    reference: str | None = Field(default=None, alias="referencia")  # certificate or origin of the data
    data_date: date | None = Field(default=None, alias="fecha")
    sha256: str | None = None

    @field_validator("capacitance", "inductance")
    @classmethod
    def _four(cls, value: list[float]) -> list[float]:
        value = [float(v) for v in value][:4]
        return value + [0.0] * (4 - len(value))

    @field_validator("cutoff_hz", mode="before")
    @classmethod
    def _cutoff(cls, value: Any) -> float | None:
        return None if value in (None, "") else _frequency(value)

    @model_validator(mode="after")
    def _check(self) -> StandardDefinition:
        if self.kind == "datos" and not self.file:
            raise ValueError("un patrón definido por datos necesita su fichero (.sdatb o .sNp)")
        if self.load_impedance is not None and len(self.load_impedance) != 2:
            raise ValueError("la impedancia de la carga son dos valores: R y X en Ω")
        return self

    @property
    def is_touchstone(self) -> bool:
        return self.kind == "datos" and not str(self.file).lower().endswith(".sdatb")

    def to_yaml(self) -> dict[str, Any]:
        """Only what the kind of definition uses."""
        if self.kind == "ninguna":
            return {"clase": "ninguna"}
        if self.kind == "datos":
            data = self.model_dump(by_alias=True, exclude_none=True, mode="json", include={"kind", "file", "ports", "reference", "data_date", "sha256"})
            if not self.ports:
                data.pop("puertos", None)
            return data
        data = self.model_dump(
            by_alias=True,
            exclude_none=True,
            include={"kind", "offset_z0", "offset_delay_ps", "offset_loss", "capacitance", "inductance", "load_impedance", "media", "cutoff_hz", "height_width_ratio"},
        )
        if not any(self.capacitance):
            data.pop("c")
        if not any(self.inductance):
            data.pop("l")
        if self.media == "coaxial":
            for key in ("medio", "frecuencia_corte", "relacion_alto_ancho"):
                data.pop(key, None)
        elif self.cutoff_hz is not None:
            data["frecuencia_corte"] = format_frequency(self.cutoff_hz, decimal=".")
        return data


def _genders(value: Any) -> Any:
    if isinstance(value, str):
        value = [part.strip() for part in value.replace("–", "-").split("-")] if "-" in value else [value]
    return value


class KitStandard(BaseModel):
    """A standard of a kit model, with its nominal definition and specifications."""

    model_config = ConfigDict(populate_by_name=True, extra="allow")

    id: str
    type: StandardType = Field(alias="tipo")
    label: str = Field(default="", alias="etiqueta")  # as the VNA or the .ckt names it (e.g. «OPEN -M-»)
    description: str | None = Field(default=None, alias="descripcion")
    genders: list[str] = Field(default_factory=list, validation_alias="generos", serialization_alias="generos")
    frequency_range: FrequencyRange | None = Field(default=None, alias="rango")
    definition: StandardDefinition = Field(default_factory=StandardDefinition, alias="definicion")
    specifications: list[SpecRow] = Field(default_factory=list, alias="especificaciones")
    notes: str | None = Field(default=None, alias="notas")

    @model_validator(mode="before")
    @classmethod
    def _gender_alias(cls, data: Any) -> Any:
        if isinstance(data, dict) and "genero" in data and "generos" not in data:
            data = {**data, "generos": data["genero"]}
            data.pop("genero")
        return data

    @field_validator("genders", mode="before")
    @classmethod
    def _parse_genders(cls, value: Any) -> Any:
        return _genders(value)

    @field_validator("frequency_range", mode="before")
    @classmethod
    def _range(cls, value: Any) -> Any:
        if value is None or isinstance(value, FrequencyRange):
            return value
        try:
            return FrequencyRange.parse(value)
        except (UnitError, ValueError) as exc:
            raise ValueError(str(exc)) from exc

    @model_validator(mode="after")
    def _check(self) -> KitStandard:
        ports = 2 if self.type in TWO_PORT_TYPES else 1
        if len(self.genders) != ports:
            raise ValueError(f"«{self.id}»: un {TYPE_NAMES[self.type].lower()} tiene {ports} género{'s' if ports > 1 else ''}")
        unknown = [g for g in self.genders if g not in GENDERS]
        if unknown:
            raise ValueError(f"«{self.id}»: género no válido: {', '.join(unknown)} (macho, hembra o sin_genero)")
        if self.type == "adaptador" and self.definition.kind == "modelo":
            raise ValueError(f"«{self.id}»: un adaptador (through desconocido) no tiene modelo; defínalo por datos o sin definición")
        return self

    @property
    def ports(self) -> int:
        return 2 if self.type in TWO_PORT_TYPES else 1

    @property
    def gender_text(self) -> str:
        return " – ".join(GENDERS[g] for g in self.genders)

    def to_yaml(self) -> dict[str, Any]:
        data: dict[str, Any] = {"id": self.id, "tipo": self.type}
        if self.label:
            data["etiqueta"] = self.label
        if self.description:
            data["descripcion"] = self.description
        data["generos"] = list(self.genders)
        if self.frequency_range is not None:
            data["rango"] = self.frequency_range.to_yaml()
        data["definicion"] = self.definition.to_yaml()
        if self.specifications:
            data["especificaciones"] = [row.to_yaml() for row in self.specifications]
        if self.notes:
            data["notas"] = self.notes
        return data


class UnitStandard(BaseModel):
    """What a unit of the kit says of one of its standards: serial number, state and its own data."""

    model_config = ConfigDict(populate_by_name=True, extra="allow")

    serial_number: str | None = Field(default=None, alias="numero_serie")
    state: Literal["en_uso", "danado", "sustituido"] = Field(default="en_uso", alias="estado")
    state_date: date | None = Field(default=None, alias="fecha_estado")
    definition: StandardDefinition | None = Field(default=None, alias="definicion")  # e.g. data of its certificate
    specifications: list[SpecRow] | None = Field(default=None, alias="especificaciones")
    notes: str | None = Field(default=None, alias="notas")

    @property
    def empty(self) -> bool:
        return not (self.serial_number or self.state != "en_uso" or self.definition or self.specifications is not None or self.notes)

    def to_yaml(self) -> dict[str, Any]:
        data: dict[str, Any] = {}
        if self.serial_number:
            data["numero_serie"] = self.serial_number
        if self.state != "en_uso":
            data["estado"] = self.state
        if self.state_date:
            data["fecha_estado"] = self.state_date.isoformat()
        if self.definition is not None:
            data["definicion"] = self.definition.to_yaml()
        if self.specifications is not None:
            data["especificaciones"] = [row.to_yaml() for row in self.specifications]
        if self.notes:
            data["notas"] = self.notes
        return data


def standard_name(standard: KitStandard, serial: str | None, kit: str) -> str:
    """Name of a standard as VNA Tools names it (appendix C): ``Open(f) 975``, ``Thru(f-m) AA180-VNACKN``."""
    genders = "-".join(GENDER_LETTERS[g] for g in standard.genders)
    return f"{VNA_TOOLS_TYPES[standard.type]}({genders}) {serial or kit}".strip()


def unique_id(base: str, taken: set[str]) -> str:
    candidate, counter = base, 2
    while candidate in taken:
        candidate, counter = f"{base}_{counter}", counter + 1
    return candidate


def default_id(kind: str, genders: list[str], taken: set[str]) -> str:
    """``abierto_m``, ``carga_h``, ``through_hm``…"""
    letters = "".join({"macho": "m", "hembra": "h", "sin_genero": "x"}[g] for g in genders)
    return unique_id(f"{kind}_{letters}", taken)
