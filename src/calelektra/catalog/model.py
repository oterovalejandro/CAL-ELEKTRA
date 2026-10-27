"""Equipment catalogue: categories → models → equipment (physical units).

YAML keys are Spanish (they are the lab-facing data format); Python attribute
names are English and map to the YAML keys through aliases.

Each category declares its typed fields (``campos``, see ``catalog/fields.py``).
Models hold the values common to a model; equipment holds its own values and,
where the field level allows it, overrides the model.

The S-parameter specifications of a model (``especificaciones_s``: S2P/S1P
files and how to compare a measurement with them) give the pass / fail of
every unit of that model when it is calibrated (``procedures/specs.py``).
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any, Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, PrivateAttr, field_validator, model_validator

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


# ports of a directional coupler before 0.8.3 (read and renamed: In, Out, Coupl. Fwd, Coupl. Rev)
LEGACY_COUPLER_PORTS = {"ENTRADA": "In", "SALIDA": "Out", "ACOPLADO": "Coupl. Fwd", "AISLADO": "Coupl. Rev"}


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
            value = [{"nombre": item} if isinstance(item, str) else item for item in value]
            names = {item.get("nombre") for item in value if isinstance(item, dict)}
            if names == set(LEGACY_COUPLER_PORTS):  # a directional coupler with the names of its ports before 0.8.3
                value = [{**item, "nombre": LEGACY_COUPLER_PORTS[item["nombre"]]} for item in value]
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


SPEC_CRITERIA = {
    "minimo": "Mínimo: medida ≥ especificación",
    "maximo": "Máximo: medida ≤ especificación",
    "tolerancia": "Tolerancia: |medida − especificación| ≤ tolerancia",
}
SPEC_QUANTITIES = {"magnitud_db": "Magnitud (dB)", "fase_grados": "Fase (°)"}


class SpecFile(BaseModel):
    """S2P (or S1P) file with specification values; pass / fail compares the measurement with it."""

    model_config = ConfigDict(populate_by_name=True, extra="allow")

    name: str = Field(alias="nombre")
    file: str = Field(alias="fichero")  # relative to the model YAML (``../especificaciones/…``)
    parameters: list[str] = Field(default_factory=lambda: ["S21"], alias="parametros")
    criterion: Literal["minimo", "maximo", "tolerancia"] = Field(default="minimo", alias="criterio")
    quantity: Literal["magnitud_db", "fase_grados"] = Field(default="magnitud_db", alias="magnitud")
    tolerance: float | None = Field(default=None, alias="tolerancia", gt=0)

    @field_validator("parameters", mode="before")
    @classmethod
    def _as_list(cls, value: Any) -> Any:
        return [value] if isinstance(value, str) else value

    @field_validator("parameters")
    @classmethod
    def _names(cls, value: list[str]) -> list[str]:
        names = [str(item).upper() for item in value]
        for item in names:
            if len(item) != 3 or item[0] != "S" or not item[1:].isdigit():
                raise ValueError(f"parámetro S no válido: {item}")
        return names

    @model_validator(mode="after")
    def _tolerance(self) -> "SpecFile":
        if self.criterion == "tolerancia" and self.tolerance is None:
            raise ValueError(f"«{self.name}»: el criterio de tolerancia necesita el valor de la tolerancia")
        return self

    def describe(self) -> str:
        unit = "dB" if self.quantity == "magnitud_db" else "°"
        values = ", ".join(f"|{p}|" if self.quantity == "magnitud_db" else f"∠{p}" for p in self.parameters)
        rule = {
            "minimo": "≥ especificación",
            "maximo": "≤ especificación",
            "tolerancia": f"= especificación ± {self.tolerance:g} {unit}" if self.tolerance else "= especificación ±",
        }[self.criterion]
        return f"{values} ({unit}) {rule}"


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
    specifications: list[Any] = Field(default_factory=list, alias="especificaciones")  # datasheet / uncertainty files (stored)
    s_specifications: list[SpecFile] = Field(default_factory=list, alias="especificaciones_s")  # pass / fail of each unit
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
    reference: str | None = Field(default=None, alias="referencia")  # inventory number of the laboratory (LENNNN.NNN)
    name: str | None = Field(default=None, alias="denominacion")  # its name in the inventory and the certificates
    serial_number: str | None = Field(default=None, alias="numero_serie")
    status: EquipmentStatus = Field(default="en_servicio", alias="estado")
    description: str | None = Field(default=None, alias="descripcion")
    values: dict[str, Any] = _values_field()  # own values / overrides of the model
    connection: ConnectionSettings | None = Field(default=None, alias="conexion")
    calibration: Calibration | None = Field(default=None, alias="calibracion")
    s_parameters: SParameterData | None = Field(default=None, alias="datos_s")
    uncertainty_file: str | None = Field(default=None, alias="incertidumbre")  # uncertainty data file of the unit (VNA)
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
