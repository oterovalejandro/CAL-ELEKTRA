"""Equipment catalogue: categories → models → equipment (physical units).

YAML keys are Spanish (they are the lab-facing data format); Python attribute
names are English and map to the YAML keys through aliases.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr

from calelektra.core.frequency import FrequencyRange
from calelektra.core.units import UnitError

FORMAT_PREFIX = "calelektra/"


class CatalogItem(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="allow", arbitrary_types_allowed=True)

    _source_path: Path | None = PrivateAttr(default=None)

    @property
    def source_path(self) -> Path | None:
        return self._source_path

    def set_source_path(self, path: Path | None) -> None:
        self._source_path = path

    def to_yaml_dict(self) -> dict[str, Any]:
        return self.model_dump(by_alias=True, exclude_none=True, mode="json")


class PortTemplate(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    template: str = Field(alias="plantilla")  # e.g. "P{n}"
    count_parameter: str = Field(alias="cantidad")  # model parameter holding the count


class Category(CatalogItem):
    format: str = Field(default="calelektra/categoria@0.2", alias="formato")
    id: str
    name: str = Field(alias="nombre")
    kind: Literal["instrumento", "pasivo", "posicionador", "recinto"] = Field(alias="clase")
    description: str | None = Field(default=None, alias="descripcion")
    rf: bool = True  # takes part in the frequency coverage of a setup
    requires_calibration: bool = Field(default=False, alias="requiere_calibracion")
    requires_s_parameters: bool = Field(default=False, alias="requiere_datos_s")
    s_parameter_ports: int | None = Field(default=None, alias="puertos_datos_s")
    ports: list[str] | PortTemplate = Field(default_factory=list, alias="puertos")
    parameters: dict[str, Any] = Field(default_factory=dict, alias="parametros")
    operations: dict[str, Any] = Field(default_factory=dict, alias="operaciones")
    specifications: dict[str, Any] = Field(default_factory=dict, alias="especificaciones")


class EquipmentModel(CatalogItem):
    format: str = Field(default="calelektra/modelo@0.2", alias="formato")
    id: str
    category: str = Field(alias="categoria")
    manufacturer: str = Field(alias="fabricante")
    designation: str = Field(alias="modelo")
    variant: str | None = Field(default=None, alias="variante")
    status: str | None = Field(default=None, alias="estado")
    description: str | None = Field(default=None, alias="descripcion")
    parameters: dict[str, Any] = Field(default_factory=dict, alias="parametros")
    communication: dict[str, Any] | None = Field(default=None, alias="comunicacion")
    specifications: list[Any] = Field(default_factory=list, alias="especificaciones")
    documents: list[Any] = Field(default_factory=list, alias="documentos")

    @property
    def display_name(self) -> str:
        name = f"{self.manufacturer} {self.designation}"
        return f"{name} ({self.variant})" if self.variant else name

    def nominal_range(self) -> FrequencyRange | None:
        value = self.parameters.get("rango_frecuencia")
        return FrequencyRange.parse(value) if value is not None else None

    def port_names(self, category: Category) -> list[str]:
        if isinstance(category.ports, PortTemplate):
            count = int(self.parameters.get(category.ports.count_parameter, 0))
            return [category.ports.template.format(n=n) for n in range(1, count + 1)]
        return list(category.ports)


class Calibration(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="allow")

    certificate: str | None = Field(default=None, alias="certificado")
    laboratory: str | None = Field(default=None, alias="laboratorio")
    issued: date | None = Field(default=None, alias="fecha")
    due: date | None = Field(default=None, alias="proxima")


class SParameterData(BaseModel):
    """Calibrated S-parameters (Touchstone file) of a passive equipment, e.g. a cable."""

    model_config = ConfigDict(populate_by_name=True, extra="allow")

    file: str = Field(alias="fichero")  # relative to the equipment YAML
    reference: str = Field(default="", alias="referencia")  # certificate / measurement it comes from
    measured_on: date | None = Field(default=None, alias="fecha")


class ConnectionSettings(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="allow")

    simulated: bool = Field(default=False, alias="simulado")
    resource: str | None = Field(default=None, alias="recurso")


EquipmentStatus = Literal["en_servicio", "fuera_de_servicio", "en_calibracion"]


class Equipment(CatalogItem):
    format: str = Field(default="calelektra/equipo@0.2", alias="formato")
    id: str
    model_id: str = Field(alias="modelo")
    serial_number: str | None = Field(default=None, alias="numero_serie")
    status: EquipmentStatus = Field(default="en_servicio", alias="estado")
    description: str | None = Field(default=None, alias="descripcion")
    parameters: dict[str, Any] = Field(default_factory=dict, alias="parametros")  # overrides the model
    connection: ConnectionSettings | None = Field(default=None, alias="conexion")
    calibration: Calibration | None = Field(default=None, alias="calibracion")
    s_parameters: SParameterData | None = Field(default=None, alias="datos_s")
    location: str | None = Field(default=None, alias="ubicacion")
    notes: str | None = Field(default=None, alias="notas")

    def range_override(self) -> FrequencyRange | None:
        value = self.parameters.get("rango_frecuencia")
        return FrequencyRange.parse(value) if value is not None else None


def parse_format(value: Any) -> tuple[str, str] | None:
    """``"calelektra/equipo@0.2"`` → ``("equipo", "0.2")``."""
    if not isinstance(value, str) or not value.startswith(FORMAT_PREFIX):
        return None
    kind, _, version = value[len(FORMAT_PREFIX) :].partition("@")
    return kind, version


__all__ = [
    "Calibration",
    "Category",
    "ConnectionSettings",
    "Equipment",
    "EquipmentModel",
    "PortTemplate",
    "SParameterData",
    "UnitError",
    "parse_format",
]
