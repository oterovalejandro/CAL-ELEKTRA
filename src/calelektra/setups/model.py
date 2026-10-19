"""Hardware setup templates (generic) and configured setups (equipment assigned).

A template declares *roles* (typed slots filled with equipment), the
connections between their ports in each state (measurement, calibration…),
the reference planes, the VNA calibration and the field-based requirements
(``requisitos``) that the assigned equipment must meet.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, field_validator

from calelektra.core.fields import FrequencyRangeField, StrList

Cardinality = Literal["1", "0..1", "0..n", "1..n"]


class Role(BaseModel):
    """Typed slot of a setup, filled with equipment when the setup is configured."""

    model_config = ConfigDict(populate_by_name=True, extra="allow")

    # Empty only in a child template that overrides part of an inherited role.
    categories: StrList = Field(default_factory=list, alias="categoria")
    label: str | None = Field(default=None, alias="etiqueta")
    description: str | None = Field(default=None, alias="descripcion")
    cardinality: Cardinality = Field(default="1", alias="cardinalidad")
    is_dut: bool = Field(default=False, alias="es_dut")
    per_segment: bool = Field(default=False, alias="por_segmento")
    min_ports: int | None = Field(default=None, alias="puertos_min")
    allowed_types: StrList | None = Field(default=None, alias="tipos")
    requires_s_parameters: bool | None = Field(default=None, alias="requiere_datos_s")
    frequency_relevant: bool | None = Field(default=None, alias="participa_en_rango")
    mounted_on: str | None = Field(default=None, alias="montado_en")
    settings: dict[str, Any] = Field(default_factory=dict, alias="ajustes")
    notes: StrList = Field(default_factory=list, alias="notas")

    @property
    def required(self) -> bool:
        return self.cardinality in ("1", "1..n")

    @property
    def multiple(self) -> bool:
        return self.cardinality in ("0..n", "1..n")

    def display(self, key: str) -> str:
        return self.label or key


class Connection(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    a: str
    b: str
    kind: Literal["coaxial", "radiado"] = "coaxial"

    @staticmethod
    def endpoint(text: str) -> tuple[str, str | None]:
        role, _, port = text.partition(".")
        return role, (port or None)

    @property
    def roles(self) -> tuple[str, str]:
        return self.endpoint(self.a)[0], self.endpoint(self.b)[0]


class ConnectionState(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="allow")

    description: str | None = Field(default=None, alias="descripcion")
    connections: list[Connection] = Field(default_factory=list, alias="conexiones")

    @field_validator("connections", mode="before")
    @classmethod
    def _parse_connections(cls, value: Any) -> Any:
        parsed = []
        for item in value or []:
            if isinstance(item, (list, tuple)) and len(item) == 2:
                parsed.append({"a": item[0], "b": item[1]})
            elif isinstance(item, dict) and "entre" in item:
                a, b = item["entre"]
                parsed.append({"a": a, "b": b, "kind": item.get("tipo", "coaxial")})
            else:
                parsed.append(item)
        return parsed


OPERATORS = {
    "==": "igual a",
    "!=": "distinto de",
    ">=": "mayor o igual que",
    "<=": "menor o igual que",
    ">": "mayor que",
    "<": "menor que",
    "en": "uno de",
    "no_en": "ninguno de",
    "contiene": "que contenga",
    "contiene_alguno": "que contenga alguno de",
    "existe": "definido",
    "cubre": "que cubra",
    "dentro_de": "dentro de",
}
RequirementOperator = Literal[
    "==", "!=", ">=", "<=", ">", "<", "en", "no_en", "contiene", "contiene_alguno", "existe", "cubre", "dentro_de"
]


class Requirement(BaseModel):
    """Condition on a field of the equipment assigned to a role (see ``setups/requirements.py``)."""

    model_config = ConfigDict(populate_by_name=True, extra="allow")

    id: str | None = None
    field: str = Field(alias="campo")  # "rol.campo[.atributo]"
    operator: RequirementOperator = Field(alias="operador")
    value: Any = Field(default=None, alias="valor")  # literal or "@rol.campo[.atributo]" / "@rango_requerido"
    severity: Literal["error", "aviso", "info"] = Field(default="error", alias="gravedad")
    message: str | None = Field(default=None, alias="mensaje")

    @property
    def role(self) -> str:
        return self.field.split(".", 1)[0]

    def roles(self) -> list[str]:
        roles = [self.role]
        if isinstance(self.value, str) and self.value.startswith("@") and "." in self.value:
            other = self.value[1:].split(".", 1)[0]
            if other not in roles:
                roles.append(other)
        return roles


class SetupTemplate(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="allow", arbitrary_types_allowed=True)

    format: str = Field(default="calelektra/plantilla_setup@0.3", alias="formato")
    id: str
    name: str = Field(alias="nombre")
    version: str = "0.1"
    status: str | None = Field(default=None, alias="estado")
    description: str | None = Field(default=None, alias="descripcion")
    extends: str | None = Field(default=None, alias="extiende")
    default_range: FrequencyRangeField | None = Field(default=None, alias="rango_medida")
    roles: dict[str, Role] = Field(default_factory=dict)
    connection_states: dict[str, ConnectionState] = Field(default_factory=dict, alias="estados_conexion")
    reference_planes: dict[str, Any] = Field(default_factory=dict, alias="planos_referencia")
    vna_calibration: dict[str, Any] = Field(default_factory=dict, alias="calibracion_vna")
    axes: dict[str, Any] = Field(default_factory=dict, alias="ejes")
    requirements: list[Requirement] = Field(default_factory=list, alias="requisitos")

    _source_path: Path | None = PrivateAttr(default=None)

    @field_validator("version", mode="before")
    @classmethod
    def _version_as_text(cls, value: Any) -> str:
        return str(value)

    @property
    def source_path(self) -> Path | None:
        return self._source_path

    def set_source_path(self, path: Path | None) -> None:
        self._source_path = path

    def merged_over(self, parent: "SetupTemplate") -> "SetupTemplate":
        """Child template (``self``) applied over its parent: roles and states are merged per key."""
        roles = {key: role.model_copy() for key, role in parent.roles.items()}
        for key, role in self.roles.items():
            if key in roles:
                merged = roles[key].model_dump(by_alias=False)
                merged.update(role.model_dump(by_alias=False, exclude_unset=True))
                roles[key] = Role.model_validate(merged)
            else:
                roles[key] = role
        states = dict(parent.connection_states)
        states.update(self.connection_states)
        own_ids = {r.id for r in self.requirements if r.id}
        requirements = [r for r in parent.requirements if not r.id or r.id not in own_ids] + list(self.requirements)
        merged_template = self.model_copy(
            update={
                "roles": roles,
                "connection_states": states,
                "requirements": requirements,
                "reference_planes": {**parent.reference_planes, **self.reference_planes},
                "vna_calibration": {**parent.vna_calibration, **self.vna_calibration},
                "axes": {**parent.axes, **self.axes},
                "default_range": self.default_range or parent.default_range,
            }
        )
        merged_template.set_source_path(self.source_path)
        return merged_template


Assignment = str | list[str] | None


def assignment_ids(value: Assignment) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value else []
    return [item for item in value if item]


class Segment(BaseModel):
    """Frequency sub-range with its own assignments for the per-segment roles."""

    model_config = ConfigDict(populate_by_name=True, arbitrary_types_allowed=True)

    name: str = Field(alias="nombre")
    range: FrequencyRangeField = Field(alias="rango")
    assignments: dict[str, Assignment] = Field(default_factory=dict, alias="asignaciones")


class ConfiguredSetup(BaseModel):
    """A template with equipment assigned to its roles and a required frequency range."""

    model_config = ConfigDict(populate_by_name=True, extra="allow", arbitrary_types_allowed=True)

    format: str = Field(default="calelektra/setup@0.3", alias="formato")
    id: str
    name: str = Field(alias="nombre")
    template: str = Field(alias="plantilla")
    required_range: FrequencyRangeField = Field(alias="rango_requerido")
    assignments: dict[str, Assignment] = Field(default_factory=dict, alias="asignaciones")
    segments: list[Segment] = Field(default_factory=list, alias="segmentos")
    layout: dict[str, list[float]] = Field(default_factory=dict, alias="disposicion")
    notes: str | None = Field(default=None, alias="notas")

    _source_path: Path | None = PrivateAttr(default=None)

    @property
    def source_path(self) -> Path | None:
        return self._source_path

    def set_source_path(self, path: Path | None) -> None:
        self._source_path = path

    def effective_segments(self) -> list[Segment]:
        if self.segments:
            return list(self.segments)
        return [Segment(name="Rango completo", range=self.required_range)]

    def ids_for(self, role_key: str, segment: Segment | None = None) -> list[str]:
        """Equipment assigned to a role; a segment's own assignment wins over the common one."""
        if segment is not None and role_key in segment.assignments:
            return assignment_ids(segment.assignments[role_key])
        return assignment_ids(self.assignments.get(role_key))

    def to_yaml_dict(self) -> dict[str, Any]:
        return self.model_dump(by_alias=True, exclude_none=True, mode="json")
