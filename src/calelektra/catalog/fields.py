"""Typed fields that each equipment category declares (``campos``).

A category says *which* fields describe its equipment (type, unit, level,
whether they are mandatory). Models give the values common to a model and
units (equipment) give their own values or override the model where allowed.

Setups read these fields to decide whether an equipment is compatible with a
measurement; procedures read them while the measurement runs.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from calelektra.core.connectors import FAMILIES, Connector, ConnectorError, family_key
from calelektra.core.frequency import FrequencyRange
from calelektra.core.issues import Issue, Severity
from calelektra.core.units import UnitError, format_frequency, parse_frequency

FieldType = Literal[
    "texto",
    "numero",
    "entero",
    "booleano",
    "opcion",
    "multiopcion",
    "lista",
    "frecuencia",
    "rango_frecuencia",
    "rango_numero",
    "conector",
    "familia_conector",
    "fecha",
]
FieldLevel = Literal["modelo", "equipo", "ambos"]

TYPE_NAMES = {
    "texto": "texto",
    "numero": "número",
    "entero": "entero",
    "booleano": "sí / no",
    "opcion": "opción",
    "multiopcion": "varias opciones",
    "lista": "lista",
    "frecuencia": "frecuencia",
    "rango_frecuencia": "rango de frecuencia",
    "rango_numero": "rango numérico",
    "conector": "conector",
    "familia_conector": "familia de conector",
    "fecha": "fecha",
}
LEVEL_NAMES = {"modelo": "modelo", "equipo": "equipo", "ambos": "modelo o equipo"}


class FieldError(ValueError):
    pass


class FieldSpec(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="allow")

    label: str = Field(alias="etiqueta")
    type: FieldType = Field(alias="tipo")
    level: FieldLevel = Field(default="ambos", alias="nivel")
    required: bool = Field(default=False, alias="obligatorio")
    unit: str | None = Field(default=None, alias="unidad")
    options: list[str] | None = Field(default=None, alias="opciones")
    default: Any = Field(default=None, alias="por_defecto")
    minimum: float | None = Field(default=None, alias="minimo")
    maximum: float | None = Field(default=None, alias="maximo")
    group: str = Field(default="General", alias="grupo")
    description: str | None = Field(default=None, alias="descripcion")

    def allowed_in(self, where: str) -> bool:
        return self.level == "ambos" or self.level == where


@dataclass(frozen=True)
class NumberRange:
    low: float
    high: float

    def __str__(self) -> str:
        return f"{self.low:g} – {self.high:g}"


def _number(value: Any, spec: FieldSpec) -> float:
    if isinstance(value, bool):
        raise FieldError("se esperaba un número")
    try:
        number = float(str(value).replace(",", ".")) if isinstance(value, str) else float(value)
    except (TypeError, ValueError) as exc:
        raise FieldError(f"«{value}» no es un número") from exc
    if spec.minimum is not None and number < spec.minimum:
        raise FieldError(f"{number:g} es menor que el mínimo {spec.minimum:g}")
    if spec.maximum is not None and number > spec.maximum:
        raise FieldError(f"{number:g} es mayor que el máximo {spec.maximum:g}")
    return number


def coerce(spec: FieldSpec, value: Any) -> Any:
    """Normalise a YAML/GUI value to its Python type; ``None`` stays ``None``."""
    if value is None or (isinstance(value, str) and not value.strip() and spec.type != "texto"):
        return None
    kind = spec.type
    try:
        if kind == "texto":
            return str(value)
        if kind == "numero":
            return _number(value, spec)
        if kind == "entero":
            number = _number(value, spec)
            if number != int(number):
                raise FieldError(f"{number:g} no es un entero")
            return int(number)
        if kind == "booleano":
            if isinstance(value, bool):
                return value
            text = str(value).strip().lower()
            if text in ("si", "sí", "true", "1", "yes"):
                return True
            if text in ("no", "false", "0"):
                return False
            raise FieldError(f"«{value}» no es sí/no")
        if kind == "opcion":
            text = str(value)
            if spec.options and text not in spec.options:
                raise FieldError(f"«{text}» no es una opción válida ({', '.join(spec.options)})")
            return text
        if kind in ("multiopcion", "lista"):
            items = [value] if isinstance(value, str) else list(value)
            items = [str(item) for item in items]
            if kind == "multiopcion" and spec.options:
                unknown = [item for item in items if item not in spec.options]
                if unknown:
                    raise FieldError(f"opciones no válidas: {', '.join(unknown)}")
            return items
        if kind == "frecuencia":
            return parse_frequency(value)
        if kind == "rango_frecuencia":
            return FrequencyRange.parse(value)
        if kind == "rango_numero":
            if isinstance(value, NumberRange):
                return value
            low, high = value
            low, high = _number(low, spec), _number(high, spec)
            if high < low:
                raise FieldError("el máximo es menor que el mínimo")
            return NumberRange(low, high)
        if kind == "conector":
            return Connector.parse(value)
        if kind == "familia_conector":
            return family_key(str(value))
        if kind == "fecha":
            if isinstance(value, date):
                return value
            return date.fromisoformat(str(value))
    except (UnitError, ConnectorError, ValueError, TypeError) as exc:
        if isinstance(exc, FieldError):
            raise
        raise FieldError(str(exc)) from exc
    raise FieldError(f"tipo de campo desconocido: {kind}")


def to_yaml(spec: FieldSpec, value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, FrequencyRange):
        return value.to_yaml()
    if isinstance(value, NumberRange):
        return [value.low, value.high]
    if isinstance(value, Connector):
        return value.to_yaml()
    if spec.type == "frecuencia":
        return format_frequency(value, decimal=".")
    if isinstance(value, date):
        return value.isoformat()
    return value


def display(spec: FieldSpec, value: Any) -> str:
    if value is None:
        return "—"
    if spec.type == "booleano":
        return "Sí" if value else "No"
    if spec.type in ("multiopcion", "lista"):
        return ", ".join(value) if value else "—"
    if spec.type == "frecuencia":
        return format_frequency(value)
    if spec.type == "familia_conector":
        return FAMILIES.get(value, value)
    if spec.type == "fecha":
        return f"{value:%d/%m/%Y}"
    if isinstance(value, float):
        text = f"{value:g}"
    else:
        text = str(value)
    if spec.unit and spec.type in ("numero", "entero", "rango_numero"):
        text += f" {spec.unit}"
    return text


@dataclass
class ResolvedField:
    key: str
    spec: FieldSpec
    value: Any
    source: str | None  # "equipo" | "modelo" | "defecto" | None

    @property
    def text(self) -> str:
        return display(self.spec, self.value)


def resolve(
    specs: dict[str, FieldSpec], model_values: dict[str, Any], equipment_values: dict[str, Any] | None = None
) -> dict[str, ResolvedField]:
    """Effective value of every field: equipment (if allowed) → model (if allowed) → default."""
    equipment_values = equipment_values or {}
    resolved: dict[str, ResolvedField] = {}
    for key, spec in specs.items():
        value, source = None, None
        for where, values in (("equipo", equipment_values), ("modelo", model_values)):
            if key in values and values[key] is not None and spec.allowed_in(where):
                try:
                    value, source = coerce(spec, values[key]), where
                except FieldError:
                    value, source = None, None
                if value is not None:
                    break
        if value is None and spec.default is not None:
            try:
                value, source = coerce(spec, spec.default), "defecto"
            except FieldError:
                value, source = None, None
        resolved[key] = ResolvedField(key, spec, value, source)
    return resolved


def validate_values(
    specs: dict[str, FieldSpec], values: dict[str, Any], where: str, owner: str, source: str | None = None
) -> list[Issue]:
    """Check the values given by a model (``where="modelo"``) or an equipment (``"equipo"``)."""
    issues: list[Issue] = []

    def add(severity: Severity, code: str, message: str) -> None:
        issues.append(Issue(severity, code, f"{owner}: {message}", source=source))

    for key, value in values.items():
        spec = specs.get(key)
        if spec is None:
            add(Severity.WARNING, "campo.desconocido", f"el campo «{key}» no está definido en la categoría")
            continue
        if not spec.allowed_in(where):
            other = "equipo" if where == "modelo" else "modelo"
            add(Severity.WARNING, "campo.nivel", f"«{spec.label}» se define en el {other}, no en el {where}; se ignora")
            continue
        try:
            coerce(spec, value)
        except FieldError as exc:
            add(Severity.ERROR, "campo.valor", f"«{spec.label}»: {exc}")
    return issues


def missing_required(
    specs: dict[str, FieldSpec], resolved: dict[str, ResolvedField], owner: str, levels: tuple[str, ...]
) -> list[Issue]:
    issues = []
    for key, spec in specs.items():
        if spec.required and spec.level in levels and resolved.get(key) is not None and resolved[key].value is None:
            issues.append(
                Issue(Severity.ERROR, "campo.obligatorio", f"{owner}: falta el campo obligatorio «{spec.label}»")
            )
    return issues
