"""Equipment catalogue: categories → models → equipment (physical units).

YAML keys are Spanish (they are the lab-facing data format); Python attribute
names are English and map to the YAML keys through aliases.

Each category declares its typed fields (``campos``, see ``catalog/fields.py``).
Models hold the values common to a model; equipment holds its own values and,
where the field level allows it, overrides the model.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any, Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, PrivateAttr, field_validator

from calelektra.catalog.fields import FieldSpec, ResolvedField, resolve

FORMAT_PREFIX = "calelektra/"
FORMAT_VERSION = "0.3"


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


class PortDef(BaseModel):
    """A port of the equipment and the field that holds its connector."""

    model_config = ConfigDict(populate_by_name=True)

    name: str = Field(alias="nombre")
    connector_field: str | None = Field(default=None, alias="conector")


class PortTemplate(BaseModel):
    """Numbered ports, e.g. ``P1 … P{num_puertos}`` of a VNA."""

    model_config = ConfigDict(populate_by_name=True)

    template: str = Field(alias="plantilla")  # e.g. "P{n}"
    count_field: str = Field(alias="cantidad")  # field holding the number of ports
    connector_field: str | None = Field(default=None, alias="conector")


class Category(CatalogItem):
    format: str = Field(default=f"calelektra/categoria@{FORMAT_VERSION}", alias="formato")
    id: str
    name: str = Field(alias="nombre")
    kind: Literal["instrumento", "pasivo", "posicionador", "recinto"] = Field(alias="clase")
    description: str | None = Field(default=None, alias="descripcion")
    rf: bool = True  # takes part in the frequency coverage of a setup
    requires_calibration: bool = Field(default=False, alias="requiere_calibracion")
    requires_s_parameters: bool = Field(default=False, alias="requiere_datos_s")
    s_parameter_ports: int | None = Field(default=None, alias="puertos_datos_s")
    ports: list[PortDef] | PortTemplate = Field(default_factory=list, alias="puertos")
    fields: dict[str, FieldSpec] = Field(default_factory=dict, validation_alias=AliasChoices("campos", "fields"), serialization_alias="campos")
    operations: Any = Field(default=None, alias="operaciones")
    specifications: dict[str, Any] = Field(default_factory=dict, alias="especificaciones")

    @field_validator("ports", mode="before")
    @classmethod
    def _ports(cls, value: Any) -> Any:
        if isinstance(value, list):
            return [{"nombre": item} if isinstance(item, str) else item for item in value]
        return value

    @property
    def has_connection(self) -> bool:
        """Instruments and positioners are remote-controlled (or simulated)."""
        return self.kind in ("instrumento", "posicionador")

    def port_defs(self, resolved: dict[str, ResolvedField]) -> list[PortDef]:
        if isinstance(self.ports, PortTemplate):
            field = resolved.get(self.ports.count_field)
            count = int(field.value) if field is not None and field.value else 0
            return [
                PortDef(name=self.ports.template.format(n=n), connector_field=self.ports.connector_field)
                for n in range(1, count + 1)
            ]
        return list(self.ports)


def _values_field() -> Any:
    return Field(
        default_factory=dict,
        validation_alias=AliasChoices("campos", "parametros", "values"),
        serialization_alias="campos",
    )


class EquipmentModel(CatalogItem):
    format: str = Field(default=f"calelektra/modelo@{FORMAT_VERSION}", alias="formato")
    id: str
    category: str = Field(alias="categoria")
    manufacturer: str = Field(alias="fabricante")
    designation: str = Field(alias="modelo")
    variant: str | None = Field(default=None, alias="variante")
    status: str | None = Field(default=None, alias="estado")
    description: str | None = Field(default=None, alias="descripcion")
    values: dict[str, Any] = _values_field()
    communication: dict[str, Any] | None = Field(default=None, alias="comunicacion")
    specifications: list[Any] = Field(default_factory=list, alias="especificaciones")
    documents: list[Any] = Field(default_factory=list, alias="documentos")

    @property
    def display_name(self) -> str:
        name = f"{self.manufacturer} {self.designation}"
        return f"{name} ({self.variant})" if self.variant else name

    @property
    def driver(self) -> str | None:
        return (self.communication or {}).get("driver")


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
    timeout_ms: int | None = Field(default=None, alias="timeout_ms")


EquipmentStatus = Literal["en_servicio", "fuera_de_servicio", "en_calibracion"]
STATUS_NAMES = {"en_servicio": "En servicio", "fuera_de_servicio": "Fuera de servicio", "en_calibracion": "En calibración"}


class Equipment(CatalogItem):
    format: str = Field(default=f"calelektra/equipo@{FORMAT_VERSION}", alias="formato")
    id: str
    model_id: str = Field(alias="modelo")
    serial_number: str | None = Field(default=None, alias="numero_serie")
    status: EquipmentStatus = Field(default="en_servicio", alias="estado")
    description: str | None = Field(default=None, alias="descripcion")
    values: dict[str, Any] = _values_field()  # own values / overrides of the model
    connection: ConnectionSettings | None = Field(default=None, alias="conexion")
    calibration: Calibration | None = Field(default=None, alias="calibracion")
    s_parameters: SParameterData | None = Field(default=None, alias="datos_s")
    location: str | None = Field(default=None, alias="ubicacion")
    notes: str | None = Field(default=None, alias="notas")

    @property
    def simulated(self) -> bool:
        return bool(self.connection and self.connection.simulated)


def resolved_fields(category: Category, model: EquipmentModel, equipment: Equipment | None = None) -> dict[str, ResolvedField]:
    return resolve(category.fields, model.values, equipment.values if equipment else None)


def parse_format(value: Any) -> tuple[str, str] | None:
    """``"calelektra/equipo@0.3"`` → ``("equipo", "0.3")``."""
    if not isinstance(value, str) or not value.startswith(FORMAT_PREFIX):
        return None
    kind, _, version = value[len(FORMAT_PREFIX) :].partition("@")
    return kind, version


__all__ = [
    "STATUS_NAMES",
    "Calibration",
    "Category",
    "ConnectionSettings",
    "Equipment",
    "EquipmentModel",
    "FieldSpec",
    "PortDef",
    "PortTemplate",
    "SParameterData",
    "parse_format",
    "resolved_fields",
]
