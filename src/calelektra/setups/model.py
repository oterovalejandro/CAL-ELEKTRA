"""Hardware setup templates (generic) and configured setups (equipment assigned).

Every setup has the same structure: the VNA on the left; from it, one line
per port used (P1, P2…) and, from each port, a **path** (``caminos``) that
extends the reference plane from the VNA connector to the port of the DUT.
A path is an ordered list of equipment (measurement cables, adapters,
chamber paths, preamplifiers…): its DUT-side connector says which DUT
connectors the setup can calibrate (``setups/paths.py``).

A template declares the paths (port, admitted categories, direction of the
signal, element that ends the path such as an antenna), the other *roles*
(typed slots: VNA, cal kit, DUT, loads, antennas…), the connections of each
state (measurement, calibration…) — the endpoint ``P1`` is the DUT-side end
of path P1 —, the VNA calibration, the test algorithms that can use it and
the field-based requirements (``requisitos``) of the assigned equipment.

A configured setup assigns the equipment of the roles and of the paths and
gives the frequency points of the measurements made with it
(``frecuencias``, inside ``rango_requerido``).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, field_serializer, field_validator, model_validator

from calelektra.core.fields import FrequencyRangeField, StrList
from calelektra.core.units import format_frequency, parse_frequency

SWEEP_MODES = {"lineal": "Lineal", "logaritmico": "Logarítmico", "lista": "Lista de frecuencias (CSV)"}
LINEAR_DEFINITIONS = {"puntos": "Nº de puntos", "paso": "Paso de frecuencia"}

Cardinality = Literal["1", "0..1", "0..n", "1..n"]
SETUP_FORMAT_VERSION = "0.4"  # 0.4: paths per VNA port (0.3 setups had one role per cable and adapter)
PATH_SENSES = {
    "ambos": "Ambos sentidos",
    "tx": "Transmisión (del VNA hacia el DUT)",
    "rx": "Recepción (del DUT hacia el VNA)",
}
DEFAULT_PATH_CATEGORIES = ["cable_medida", "adaptador", "path"]


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


class PathDefinition(BaseModel):
    """Path of a VNA port in a setup template: equipment that takes the reference plane from the VNA port to the DUT."""

    model_config = ConfigDict(populate_by_name=True, extra="allow")

    vna_port: int | None = Field(default=None, alias="puerto_vna", ge=1)  # default: the number of the key (P1 → 1)
    label: str | None = Field(default=None, alias="etiqueta")
    description: str | None = Field(default=None, alias="descripcion")
    categories: StrList = Field(default_factory=lambda: list(DEFAULT_PATH_CATEGORIES), alias="categorias")
    sense: Literal["ambos", "tx", "rx"] = Field(default="ambos", alias="sentido")  # direction of the signal (directional elements)
    final: str | None = None  # role that ends the path after the reference plane (e.g. an antenna)

    def port_number(self, key: str) -> int:
        if self.vna_port:
            return self.vna_port
        match = re.search(r"(\d+)$", key)
        return int(match.group(1)) if match else 1

    def display(self, key: str) -> str:
        return self.label or f"Camino {key}"


class SetupTemplate(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="allow", arbitrary_types_allowed=True)

    format: str = Field(default=f"calelektra/plantilla_setup@{SETUP_FORMAT_VERSION}", alias="formato")
    id: str
    name: str = Field(alias="nombre")
    version: str = "0.1"
    status: str | None = Field(default=None, alias="estado")
    description: str | None = Field(default=None, alias="descripcion")
    extends: str | None = Field(default=None, alias="extiende")
    default_range: FrequencyRangeField | None = Field(default=None, alias="rango_medida")
    algorithms: StrList = Field(default_factory=list, alias="algoritmos")  # test algorithms that can use the setups
    paths: dict[str, PathDefinition] = Field(default_factory=dict, alias="caminos")
    roles: dict[str, Role] = Field(default_factory=dict)
    connection_states: dict[str, ConnectionState] = Field(default_factory=dict, alias="estados_conexion")
    # derived from the paths when the setup is expanded (``setups/paths.py``); not written in the templates
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
        paths = {key: definition.model_copy() for key, definition in parent.paths.items()}
        for key, definition in self.paths.items():
            if key in paths:
                merged = paths[key].model_dump(by_alias=False)
                merged.update(definition.model_dump(by_alias=False, exclude_unset=True))
                paths[key] = PathDefinition.model_validate(merged)
            else:
                paths[key] = definition
        merged_template = self.model_copy(
            update={
                "roles": roles,
                "paths": paths,
                "algorithms": self.algorithms or parent.algorithms,
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


class FrequencySettings(BaseModel):
    """Frequency points of the setup inside its required range.

    * lineal: by number of points or by frequency step;
    * logaritmico: by number of points;
    * lista: CSV file with the frequencies (relative to the setup YAML); the
      points outside the required range are not measured.
    """

    model_config = ConfigDict(populate_by_name=True, extra="allow")

    mode: Literal["lineal", "logaritmico", "lista"] = Field(default="lineal", alias="modo")
    linear_definition: Literal["puntos", "paso"] = Field(default="puntos", alias="definicion")
    points: int = Field(default=801, alias="puntos", ge=2)
    step: float | None = Field(default=None, alias="paso")
    list_file: str | None = Field(default=None, alias="fichero_lista")

    @field_validator("step", mode="before")
    @classmethod
    def _step(cls, value: Any) -> float | None:
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        return parse_frequency(value)

    @field_serializer("step")
    def _step_out(self, value: float | None) -> str | None:
        return None if value is None else format_frequency(value, decimal=".")

    @model_validator(mode="after")
    def _consistent(self) -> "FrequencySettings":
        if self.mode == "lineal" and self.linear_definition == "paso" and not (self.step and self.step > 0):
            raise ValueError("el barrido lineal por paso necesita el paso de frecuencia")
        if self.mode == "lista" and not self.list_file:
            raise ValueError("el barrido por lista necesita el fichero CSV con las frecuencias")
        return self

    @property
    def by_step(self) -> bool:
        return self.mode == "lineal" and self.linear_definition == "paso"

    def summary(self) -> str:
        """Short text, e.g. «Lineal, paso 50 MHz» or «Lista puntos_cable_N.csv»."""
        if self.mode == "lista":
            return f"Lista {Path(self.list_file or '').name}"
        if self.by_step:
            return f"Lineal, paso {format_frequency(self.step or 0)}"
        return f"{SWEEP_MODES[self.mode]}, {self.points} puntos"


class PathItem(BaseModel):
    """An equipment of a path, from the VNA towards the DUT (``"CAB-PT-N-01"`` or ``{equipo, invertido}``).

    Two-port equipment is connected by its first port on the VNA side unless
    that does not mate and the other port does (automatic), or ``invertido``
    says so. Directional equipment (IN / OUT) follows the direction of the path.
    """

    model_config = ConfigDict(populate_by_name=True)

    equipment: str = Field(alias="equipo")
    reversed: bool = Field(default=False, alias="invertido")

    @model_validator(mode="before")
    @classmethod
    def _from_text(cls, data: Any) -> Any:
        return {"equipo": data} if isinstance(data, str) else data

    def to_yaml(self) -> str | dict[str, Any]:
        return {"equipo": self.equipment, "invertido": True} if self.reversed else self.equipment


class ConfiguredSetup(BaseModel):
    """A template with equipment assigned to its roles and paths, a required frequency range and its frequency points."""

    model_config = ConfigDict(populate_by_name=True, extra="allow", arbitrary_types_allowed=True)

    format: str = Field(default=f"calelektra/setup@{SETUP_FORMAT_VERSION}", alias="formato")
    id: str
    name: str = Field(alias="nombre")
    template: str = Field(alias="plantilla")
    required_range: FrequencyRangeField = Field(alias="rango_requerido")
    frequencies: FrequencySettings = Field(default_factory=FrequencySettings, alias="frecuencias")
    paths: dict[str, list[PathItem]] = Field(default_factory=dict, alias="caminos")  # VNA port → equipment towards the DUT
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

    def path_ids(self, key: str) -> list[str]:
        return [item.equipment for item in self.paths.get(key, [])]

    def equipment_places(self) -> dict[str, list[str]]:
        """Where each equipment is used: role keys (once per role, whatever the segment) and path positions (``P1-1``…).

        Every equipment can take only one place in a setup (``equipo.repetido`` in the analysis).
        """
        places: dict[str, list[str]] = {}

        def add(equipment_id: str, place: str) -> None:
            found = places.setdefault(equipment_id, [])
            if place not in found:
                found.append(place)

        for role, value in self.assignments.items():
            for equipment_id in assignment_ids(value):
                add(equipment_id, role)
        for segment in self.segments:
            for role, value in segment.assignments.items():
                for equipment_id in assignment_ids(value):
                    add(equipment_id, role)
        for key, items in self.paths.items():
            for index, item in enumerate(items, start=1):
                add(item.equipment, f"{key}-{index}")
        return places

    def used_outside(self, place: str | None = None) -> set[str]:
        """Equipment already used in the setup in a place other than ``place`` (a role key or a path position)."""
        return {equipment_id for equipment_id, places in self.equipment_places().items() if any(p != place for p in places)}

    def equipment_ids(self) -> set[str]:
        """Every equipment of the setup: roles, segments and paths."""
        ids: set[str] = set()
        for value in self.assignments.values():
            ids.update(assignment_ids(value))
        for segment in self.segments:
            for value in segment.assignments.values():
                ids.update(assignment_ids(value))
        for items in self.paths.values():
            ids.update(item.equipment for item in items)
        return ids

    def to_yaml_dict(self) -> dict[str, Any]:
        data = self.model_dump(by_alias=True, exclude_none=True, mode="json")
        data["caminos"] = {key: [item.to_yaml() for item in items] for key, items in self.paths.items()}
        ordered = {}
        for key, value in data.items():  # paths right after the frequencies, before the other assignments
            if key != "caminos":
                ordered[key] = value
            if key == "frecuencias":
                ordered["caminos"] = data["caminos"]
        ordered.setdefault("caminos", data["caminos"])
        return ordered
