"""Evaluation of the field-based requirements of a setup template (``requisitos``).

Each requirement compares a field of the equipment assigned to a role with a
literal value or with a field of another role::

    requisitos:
      - campo: vna.num_puertos
        operador: ">="
        valor: 2
        mensaje: El VNA debe tener al menos 2 puertos de medida
      - campo: kit_cal.familia_conector
        operador: "=="
        valor: "@vna.conector_puertos.familia"
        gravedad: aviso

``campo`` is ``rol.campo[.atributo]``. A value starting with ``@`` is another
reference, or ``@rango_requerido`` (the required range of the setup or of the
segment). Attributes: ``familia`` / ``genero`` of a connector, ``inicio`` /
``fin`` of a frequency range, ``min`` / ``max`` of a numeric range.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from calelektra.catalog.fields import FieldSpec, NumberRange, display
from calelektra.catalog.library import Library
from calelektra.catalog.model import Equipment
from calelektra.core.connectors import FAMILIES, GENDERS, Connector, ConnectorError, family_key
from calelektra.core.frequency import FrequencyRange
from calelektra.core.units import UnitError, format_frequency, parse_frequency
from calelektra.setups.model import OPERATORS, Requirement

REQUIRED_RANGE = "@rango_requerido"
ATTRIBUTES = {"familia", "genero", "inicio", "fin", "min", "max"}


class RequirementError(ValueError):
    pass


@dataclass
class Operand:
    value: Any
    text: str
    label: str  # what it is: field label or «rango requerido»


@dataclass
class RequirementResult:
    requirement: Requirement
    ok: bool | None  # None: cannot be evaluated (missing value)
    subject: str  # e.g. «VNA-01» (VNA) · Nº de puertos de medida = 2
    detail: str


def _format(value: Any, spec: FieldSpec | None = None) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "Sí" if value else "No"
    if spec is not None:
        try:
            return display(spec, value)
        except (TypeError, ValueError):
            pass
    if isinstance(value, Connector):
        return value.label()
    if isinstance(value, (list, tuple, set)):
        return ", ".join(_format(item) for item in value) or "—"
    if isinstance(value, float):
        return f"{value:g}"
    if isinstance(value, str) and value in FAMILIES:
        return FAMILIES[value]
    return str(value)


def _attribute(value: Any, attribute: str | None, spec: FieldSpec | None) -> tuple[Any, str | None]:
    """Value of ``campo.atributo`` and the kind of the result (for formatting literals)."""
    if attribute is None or value is None:
        return value, spec.type if spec else None
    if isinstance(value, Connector):
        if attribute == "familia":
            return value.family, "familia_conector"
        if attribute == "genero":
            return value.gender, "genero"
    if isinstance(value, FrequencyRange):
        if attribute == "inicio":
            return value.start, "frecuencia"
        if attribute == "fin":
            return value.stop, "frecuencia"
    if isinstance(value, NumberRange):
        if attribute == "min":
            return value.low, "numero"
        if attribute == "max":
            return value.high, "numero"
    raise RequirementError(f"el atributo «{attribute}» no se aplica a este campo")


def _reference(
    reference: str, equipment: dict[str, Equipment], library: Library, required_range: FrequencyRange | None
) -> tuple[Operand, str | None, Equipment | None]:
    if reference == REQUIRED_RANGE:
        return Operand(required_range, str(required_range) if required_range else "—", "rango requerido"), "rango_frecuencia", None
    parts = reference.split(".")
    if len(parts) < 2 or len(parts) > 3:
        raise RequirementError(f"referencia no válida: «{reference}» (use rol.campo[.atributo])")
    role, key = parts[0], parts[1]
    attribute = parts[2] if len(parts) == 3 else None
    if attribute is not None and attribute not in ATTRIBUTES:
        raise RequirementError(f"atributo desconocido «{attribute}» en «{reference}»")
    eq = equipment.get(role)
    if eq is None:
        raise RequirementError(f"el rol «{role}» no tiene equipo asignado")
    resolved = library.fields_of(eq).get(key)
    if resolved is None:
        category = library.category_of(eq)
        raise RequirementError(f"la categoría «{category.name if category else '?'}» no tiene el campo «{key}»")
    value, kind = _attribute(resolved.value, attribute, resolved.spec)
    label = resolved.spec.label + (f" ({attribute})" if attribute else "")
    if attribute is None:
        text = _format(value, resolved.spec)
    elif kind == "frecuencia":
        text = format_frequency(value) if value is not None else "—"
    elif kind == "genero":
        text = GENDERS.get(value, str(value))
    else:
        text = _format(value)
    return Operand(value, text, label), kind, eq


def _literal(value: Any, kind: str | None, operator: str) -> Any:
    """Convert a literal of the YAML to the type of the field it is compared with."""
    many = operator in ("en", "no_en", "contiene", "contiene_alguno")
    if many and isinstance(value, (list, tuple)):
        return [_literal(item, kind, "==") for item in value]
    try:
        if kind in ("frecuencia",):
            return parse_frequency(value)
        if kind == "rango_frecuencia" and operator in ("cubre", "dentro_de"):
            if isinstance(value, (list, tuple)):
                return FrequencyRange.parse(value)
            return parse_frequency(value)
        if kind == "rango_numero" and operator in ("cubre", "dentro_de"):
            if isinstance(value, (list, tuple)):
                return NumberRange(float(value[0]), float(value[1]))
            return float(value)
        if kind == "familia_conector":
            return family_key(str(value))
        if kind == "conector":
            return Connector.parse(value)
        if kind in ("numero", "entero"):
            return float(value)
    except (UnitError, ConnectorError, ValueError, TypeError) as exc:
        raise RequirementError(f"valor «{value}» no válido para el campo: {exc}") from exc
    return value


def _contains(container: Any, item: Any) -> bool:
    if isinstance(container, FrequencyRange):
        if isinstance(item, FrequencyRange):
            return container.contains(item)
        return container.start <= float(item) <= container.stop
    if isinstance(container, NumberRange):
        if isinstance(item, NumberRange):
            return container.low <= item.low and item.high <= container.high
        return container.low <= float(item) <= container.high
    raise RequirementError("«cubre» / «dentro_de» necesitan un rango")


def _compare(operator: str, subject: Any, value: Any) -> bool:
    if operator == "existe":
        return subject is not None and subject != []
    if operator in ("==", "!="):
        equal = subject == value
        if isinstance(subject, (int, float)) and isinstance(value, (int, float)):
            equal = abs(float(subject) - float(value)) <= 1e-9 * max(1.0, abs(float(value)))
        return equal if operator == "==" else not equal
    if operator in (">=", "<=", ">", "<"):
        try:
            a, b = float(subject), float(value)
        except (TypeError, ValueError) as exc:
            raise RequirementError(f"«{operator}» necesita números") from exc
        return {">=": a >= b, "<=": a <= b, ">": a > b, "<": a < b}[operator]
    if operator in ("en", "no_en"):
        options = value if isinstance(value, (list, tuple, set)) else [value]
        inside = subject in options
        return inside if operator == "en" else not inside
    if operator in ("contiene", "contiene_alguno"):
        items = subject if isinstance(subject, (list, tuple, set)) else [subject]
        wanted = value if isinstance(value, (list, tuple, set)) else [value]
        if operator == "contiene":
            return all(item in items for item in wanted)
        return any(item in items for item in wanted)
    if operator == "cubre":
        return _contains(subject, value)
    if operator == "dentro_de":
        return _contains(value, subject)
    raise RequirementError(f"operador desconocido: {operator}")


def evaluate(
    requirement: Requirement,
    equipment: dict[str, Equipment],
    library: Library,
    role_labels: dict[str, str],
    required_range: FrequencyRange | None = None,
) -> RequirementResult:
    """Evaluate one requirement for one combination of assigned equipment (role → equipment)."""
    role = requirement.role
    try:
        subject, kind, subject_eq = _reference(requirement.field, equipment, library, required_range)
    except RequirementError as exc:
        return RequirementResult(requirement, None, requirement.field, str(exc))
    owner = f"«{subject_eq.id}» ({role_labels.get(role, role)})" if subject_eq else role_labels.get(role, role)
    subject_text = f"{owner} · {subject.label} = {subject.text}"
    operator_text = OPERATORS.get(requirement.operator, requirement.operator)
    if requirement.operator != "existe" and subject.value is None:
        return RequirementResult(requirement, None, subject_text, f"no se puede comprobar: «{subject.label}» no tiene valor")
    try:
        if isinstance(requirement.value, str) and requirement.value.startswith("@"):
            reference = requirement.value if requirement.value == REQUIRED_RANGE else requirement.value[1:]
            other, _, other_eq = _reference(reference, equipment, library, required_range)
            if other.value is None:
                return RequirementResult(requirement, None, subject_text, f"no se puede comprobar: «{other.label}» no tiene valor")
            expected, expected_text = other.value, other.text
            if other_eq is not None:
                expected_text = f"{other.text} ({other.label} de «{other_eq.id}»)"
        elif requirement.operator == "existe":
            expected, expected_text = None, ""
        else:
            expected = _literal(requirement.value, kind, requirement.operator)
            expected_text = _format(expected)
            if kind == "frecuencia" and isinstance(expected, float):
                expected_text = format_frequency(expected)
        ok = _compare(requirement.operator, subject.value, expected)
    except RequirementError as exc:
        return RequirementResult(requirement, None, subject_text, str(exc))
    if requirement.operator == "existe":
        detail = "definido" if ok else f"«{subject.label}» no está definido"
    else:
        detail = f"se requiere {operator_text} {expected_text}"
    if not ok and requirement.message:
        detail = f"{requirement.message} ({detail})"
    return RequirementResult(requirement, ok, subject_text, detail)
