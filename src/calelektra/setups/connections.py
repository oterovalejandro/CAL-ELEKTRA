"""Connections, connectors, reference planes and VNA calibration of a configured setup.

* Effective connections of each state: optional roles left unassigned are
  bridged (``cable.B – [adaptador] – dut.A`` becomes ``cable.B – dut.A``).
* Every coaxial connection is checked with the connector fields of the two
  equipment ports (family and gender).
* The reference planes (``planos_referencia``) give the connector where the
  calibration standards and the DUT are connected. From them the calibration
  is designed: OSM with one plane; TOSM (flush through) when both planes mate
  directly, UOSM (unknown through) when they do not. The cal kit is checked
  against the planes (family, genders of its standards, methods).

The procedure engine uses the same functions, so what the setup shows is what
the measurement does.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from calelektra.catalog.library import Library
from calelektra.catalog.model import Equipment
from calelektra.core.connectors import FAMILIES, GENDERS, MECHANICAL_GROUPS, Connector, mating_issue
from calelektra.core.issues import Severity
from calelektra.setups.model import ConfiguredSetup, Segment, SetupTemplate

PORT_CALIBRATION_METHODS = ("OSM", "TOSM", "UOSM")


@dataclass(frozen=True)
class Endpoint:
    role: str
    port: str | None

    @classmethod
    def parse(cls, text: str) -> "Endpoint":
        role, _, port = text.partition(".")
        return cls(role, port or None)

    def __str__(self) -> str:
        return f"{self.role}.{self.port}" if self.port else self.role


@dataclass
class ResolvedEndpoint:
    endpoint: Endpoint
    equipment: Equipment | None
    connector: Connector | None
    port_exists: bool

    def text(self) -> str:
        if self.equipment is None:
            return str(self.endpoint)
        port = f".{self.endpoint.port}" if self.endpoint.port else ""
        connector = f" ({self.connector.label()})" if self.connector else ""
        return f"{self.equipment.id}{port}{connector}"


@dataclass
class EffectiveConnection:
    a: Endpoint
    b: Endpoint
    kind: str = "coaxial"
    bridged: tuple[str, ...] = ()  # optional roles removed in between


@dataclass
class ConnectionCheck:
    state: str
    a: ResolvedEndpoint
    b: ResolvedEndpoint
    kind: str
    severity: Severity | None  # None: correct
    message: str
    bridged: tuple[str, ...] = ()


def _is_assigned(template: SetupTemplate, setup: ConfiguredSetup, role_key: str, segment: Segment | None) -> bool:
    role = template.roles.get(role_key)
    if role is None:
        return False
    return bool(setup.ids_for(role_key, segment if role.per_segment else None))


def effective_connections(
    template: SetupTemplate, setup: ConfiguredSetup, state: str, segment: Segment | None = None
) -> list[EffectiveConnection]:
    definition = template.connection_states.get(state)
    if definition is None:
        return []
    connections = [EffectiveConnection(Endpoint.parse(c.a), Endpoint.parse(c.b), c.kind) for c in definition.connections]
    pending = [key for key in template.roles if not _is_assigned(template, setup, key, segment)]
    for role_key in pending:
        touching = [c for c in connections if role_key in (c.a.role, c.b.role)]
        if not touching:
            continue
        connections = [c for c in connections if all(c is not t for t in touching)]
        others = [(c.b if c.a.role == role_key else c.a, c) for c in touching]
        others = [(endpoint, c) for endpoint, c in others if endpoint.role != role_key]
        if not template.roles[role_key].required and len(others) == 2:
            (first, c1), (second, c2) = others
            kind = "radiado" if "radiado" in (c1.kind, c2.kind) else "coaxial"
            connections.append(EffectiveConnection(first, second, kind, c1.bridged + (role_key,) + c2.bridged))
    return connections


def resolve_endpoint(
    endpoint: Endpoint, template: SetupTemplate, setup: ConfiguredSetup, library: Library, segment: Segment | None = None
) -> ResolvedEndpoint:
    role = template.roles.get(endpoint.role)
    ids = setup.ids_for(endpoint.role, segment if role is not None and role.per_segment else None)
    equipment = library.equipment.get(ids[0]) if ids else None
    if equipment is None:
        return ResolvedEndpoint(endpoint, None, None, False)
    if endpoint.port is None:
        return ResolvedEndpoint(endpoint, equipment, None, True)
    ports = {p.name for p in library.ports_of(equipment)}
    if endpoint.port not in ports:
        return ResolvedEndpoint(endpoint, equipment, None, False)
    return ResolvedEndpoint(endpoint, equipment, library.port_connector(equipment, endpoint.port), True)


def check_connections(
    template: SetupTemplate, setup: ConfiguredSetup, library: Library, segment: Segment | None = None
) -> list[ConnectionCheck]:
    checks: list[ConnectionCheck] = []
    for state in template.connection_states:
        for connection in effective_connections(template, setup, state, segment):
            a = resolve_endpoint(connection.a, template, setup, library, segment)
            b = resolve_endpoint(connection.b, template, setup, library, segment)
            if a.equipment is None or b.equipment is None:
                continue  # unassigned required role: reported by the role checks

            def add(severity: Severity | None, message: str) -> None:
                checks.append(ConnectionCheck(state, a, b, connection.kind, severity, message, connection.bridged))

            missing = [r for r in (a, b) if not r.port_exists]
            if missing:
                for r in missing:
                    names = ", ".join(p.name for p in library.ports_of(r.equipment)) or "ninguno"  # type: ignore[arg-type]
                    add(Severity.ERROR, f"«{r.equipment.id}» no tiene el puerto «{r.endpoint.port}» (puertos: {names})")  # type: ignore[union-attr]
                continue
            if connection.kind == "radiado":
                add(None, "acoplamiento radiado")
                continue
            if a.connector is None or b.connector is None:
                add(Severity.INFO, "sin datos de conector: no se comprueba")
                continue
            issue = mating_issue(a.connector, b.connector)
            if issue is None:
                add(None, "conectores compatibles")
            else:
                add(issue[0], issue[1])
    return checks


@dataclass
class PlaneInfo:
    name: str
    vna_port: int | None
    endpoint: ResolvedEndpoint | None
    candidates: list[str]

    @property
    def connector(self) -> Connector | None:
        return self.endpoint.connector if self.endpoint else None

    def text(self) -> str:
        where = self.endpoint.text() if self.endpoint else "sin equipo asignado"
        port = f" — puerto {self.vna_port} del VNA" if self.vna_port else ""
        return f"{self.name}: {where}{port}"


def reference_planes(
    template: SetupTemplate, setup: ConfiguredSetup, library: Library, segment: Segment | None = None
) -> list[PlaneInfo]:
    planes = []
    for name, definition in template.reference_planes.items():
        vna_port = None
        if isinstance(definition, dict):
            candidates = definition.get("en", [])
            vna_port = definition.get("puerto_vna")
        else:
            candidates = definition
        candidates = [candidates] if isinstance(candidates, str) else list(candidates or [])
        if vna_port is None:
            match = re.search(r"(\d+)$", name)
            vna_port = int(match.group(1)) if match else None
        endpoint = None
        for text in candidates:
            resolved = resolve_endpoint(Endpoint.parse(text), template, setup, library, segment)
            if resolved.equipment is not None:
                endpoint = resolved
                break
        planes.append(PlaneInfo(name, int(vna_port) if vna_port else None, endpoint, candidates))
    return planes


@dataclass
class CalibrationDesign:
    """How the VNA ports of a setup are calibrated (used by the setup view and by the procedures)."""

    method: str | None
    reason: str
    planes: list[PlaneInfo]
    vna: Equipment | None = None
    kit: Equipment | None = None
    kit_name: str | None = None
    kit_family: str | None = None
    checks: list[tuple[Severity | None, str]] = field(default_factory=list)

    @property
    def ports(self) -> tuple[int, ...]:
        return tuple(p.vna_port for p in self.planes if p.vna_port)

    @property
    def connectors(self) -> dict[int, Connector]:
        return {p.vna_port: p.connector for p in self.planes if p.vna_port and p.connector}

    @property
    def ok(self) -> bool:
        return not any(severity is Severity.ERROR for severity, _ in self.checks)


def _mechanically_compatible(a: str, b: str) -> bool:
    return any(a in group and b in group for group in MECHANICAL_GROUPS)


def design_calibration(
    template: SetupTemplate,
    setup: ConfiguredSetup,
    library: Library,
    segment: Segment | None = None,
    method_override: str | None = None,
) -> CalibrationDesign:
    allowed = [m for m in (template.vna_calibration.get("metodos") or []) if m in PORT_CALIBRATION_METHODS]
    planes = reference_planes(template, setup, library, segment)
    vna_ids = setup.ids_for("vna")
    vna = library.equipment.get(vna_ids[0]) if vna_ids else None
    if not allowed:
        reason = "La plantilla no calibra los puertos del VNA"
        if template.vna_calibration.get("metodos"):
            reason += f" (usa: {', '.join(map(str, template.vna_calibration['metodos']))})"
        return CalibrationDesign(None, reason, planes, vna=vna)
    design = CalibrationDesign(None, "", planes, vna=vna)
    add = design.checks.append

    for plane in planes:
        if plane.endpoint is None:
            add((Severity.ERROR, f"Plano {plane.name}: ningún equipo asignado en {', '.join(plane.candidates)}"))
        elif plane.connector is None:
            add((Severity.WARNING, f"Plano {plane.name} ({plane.endpoint.text()}): conector sin definir"))
        if plane.vna_port is None:
            add((Severity.ERROR, f"Plano {plane.name}: no se sabe a qué puerto del VNA corresponde (puerto_vna)"))

    if len(planes) == 1:
        method, reason = "OSM", "Un plano de referencia: calibración OSM de 1 puerto"
    elif len(planes) == 2:
        first, second = planes
        a, b = first.connector, second.connector
        if a is None or b is None:
            method, reason = "UOSM", "Conectores de los planos sin definir: through desconocido (UOSM)"
        else:
            issue = mating_issue(a, b)
            if issue is None or issue[0] is Severity.WARNING:
                method = "TOSM"
                reason = f"Los planos {first.name} ({a.label()}) y {second.name} ({b.label()}) se unen directamente: through plano (TOSM)"
            else:
                method = "UOSM"
                reason = (
                    f"Los planos {first.name} ({a.label()}) y {second.name} ({b.label()}) no se pueden unir "
                    f"directamente ({issue[1]}): through desconocido (UOSM)"
                )
        if method not in allowed and "UOSM" in allowed:
            method, reason = "UOSM", reason + "; la plantilla solo admite UOSM"
    else:
        add((Severity.ERROR, f"La plantilla define {len(planes)} planos de referencia; se admiten 1 o 2"))
        return design
    if method_override and method_override not in ("automatico", method):
        if method_override not in PORT_CALIBRATION_METHODS:
            add((Severity.ERROR, f"Método de calibración «{method_override}» no válido para calibrar los puertos"))
            return design
        if method_override == "TOSM" and method == "UOSM" and "no se pueden unir" in reason:
            add((Severity.ERROR, f"El procedimiento pide TOSM, pero {reason[0].lower()}{reason[1:]}"))
        reason = f"Método fijado por el procedimiento: {method_override} (automático sería {method})"
        method = method_override
    if method not in allowed:
        add((Severity.ERROR, f"La plantilla no admite {method} (admite: {', '.join(allowed)})"))
    design.method, design.reason = method, reason

    if vna is not None:
        vna_methods = library.field(vna, "metodos_calibracion", [])
        if method not in vna_methods:
            add((Severity.ERROR, f"El VNA «{vna.id}» no tiene el método {method} ({', '.join(vna_methods) or 'sin métodos'})"))
        port_count = int(library.field(vna, "num_puertos", 0) or 0)
        for port in design.ports:
            if port > port_count:
                add((Severity.ERROR, f"El VNA «{vna.id}» no tiene el puerto {port} ({port_count} puertos)"))

    kit_role = template.vna_calibration.get("kit")
    kit_ids = setup.ids_for(kit_role) if kit_role else []
    kit = library.equipment.get(kit_ids[0]) if kit_ids else None
    if kit is None:
        families = sorted({FAMILIES[c.family] for c in design.connectors.values()})
        add(
            (
                Severity.WARNING,
                "Sin kit de calibración asignado: se usará el kit que tenga seleccionado el VNA"
                + (f" para {', '.join(families)}" if families else "")
                + "; el kit no quedará registrado en el ensayo",
            )
        )
        return design
    design.kit = kit
    design.kit_family = library.field(kit, "familia_conector")
    design.kit_name = library.field(kit, "nombre_kit_vna")
    genders = library.field(kit, "generos_disponibles", [])
    kit_methods = library.field(kit, "metodos", [])
    for plane in planes:
        connector = plane.connector
        if connector is None or not connector.coaxial:
            continue
        if design.kit_family != connector.family:
            if design.kit_family and _mechanically_compatible(design.kit_family, connector.family):
                add((Severity.WARNING, f"Kit «{kit.id}» de {FAMILIES[design.kit_family]} en el plano {plane.name} ({connector.label()}): compatible mecánicamente"))
            else:
                kit_family = FAMILIES.get(design.kit_family or "", "?")
                add((Severity.ERROR, f"El kit «{kit.id}» es de {kit_family} y el plano {plane.name} es {connector.label()}"))
                continue
        needed = connector.mating_gender()
        if needed in GENDERS and needed != "sin_genero" and needed not in genders:
            add(
                (
                    Severity.ERROR,
                    f"El plano {plane.name} es {connector.label()}: necesita estándares {GENDERS[needed]} "
                    f"y el kit «{kit.id}» solo tiene {', '.join(genders) or 'ninguno'}",
                )
            )
    if method not in kit_methods:
        add((Severity.ERROR, f"El kit «{kit.id}» no admite {method} ({', '.join(kit_methods) or 'sin métodos'})"))
    if not design.kit_name:
        add((Severity.WARNING, f"El kit «{kit.id}» no indica su nombre en el VNA (campo «Nombre del kit en el VNA»)"))
    return design
