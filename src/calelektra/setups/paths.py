"""Paths of a hardware setup: from each VNA port to the port of the DUT.

A setup template declares the VNA ports it uses (``caminos``: P1, P2…). Each
configured setup fills every path with an ordered list of equipment from the
VNA towards the DUT (measurement cables, adapters, chamber paths,
preamplifiers…). The path extends the reference plane of its port: the
plane (PR1, PR2…) is at the DUT-side end of the path, so its connector says
which DUT connectors the setup can calibrate. An empty path leaves the
plane on the VNA port. The element that ends a path after the plane (e.g.
the antenna of a chamber path, ``final``) is an ordinary role of the
template.

Orientation of each element: directional equipment (ports IN / OUT) follows
the direction of the path (``sentido``: tx from the VNA, rx towards the
VNA); two-port passive equipment is connected by its first port on the VNA
side unless only the other one mates with the previous connector
(automatic) or the setup says ``invertido``.

To check and run a setup, :func:`expand` turns the paths into ordinary
roles (``P1-1``, ``P1-2``…), connections (``vna.P1 – P1-1.A``, ``P1-1.B –
P1-2.A``…, present in every state: the paths stay connected) and reference
planes. In the connection states of the template, the endpoint ``P1`` is
the DUT-side end of path P1 and ``dut.#1``, ``dut.#2``… are the ports of the
DUT measured on P1, P2… (the order of its category, or the one given by the
test template).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from calelektra.core.connectors import Connector, mating_issue
from calelektra.core.issues import Issue, Severity
from calelektra.setups.model import Connection, ConfiguredSetup, ConnectionState, PathDefinition, Role, SetupTemplate

if TYPE_CHECKING:
    from calelektra.catalog.library import Library
    from calelektra.catalog.model import Equipment

DIRECTIONAL_PORTS = frozenset({"IN", "OUT"})
DUT_PORT = re.compile(r"^#(\d+)$")


def element_role(path_key: str, index: int) -> str:
    """Role key of the ``index``-th equipment (1, 2…) of a path, e.g. ``P1-2``."""
    return f"{path_key}-{index}"


def plane_name(path_key: str, vna_port: int) -> str:
    return f"PR{vna_port}"


def vna_role_of(template: SetupTemplate) -> str | None:
    return next((key for key, role in template.roles.items() if "vna" in role.categories), None)


def dut_role_of(template: SetupTemplate | None) -> str | None:
    if template is None:
        return None
    return next((key for key, role in template.roles.items() if role.is_dut), None)


def admitted_dut_connector(end: Connector | None) -> Connector | None:
    """Connector of a DUT port that mates directly with the end of a path."""
    if end is None:
        return None
    return Connector(end.family, end.mating_gender())


def _mates(a: Connector | None, b: Connector | None) -> bool:
    if a is None or b is None:
        return False
    issue = mating_issue(a, b)
    return issue is None or issue[0] is not Severity.ERROR


@dataclass
class PathElement:
    index: int  # 1, 2… from the VNA
    role: str  # role key of the expanded setup, e.g. P1-1
    equipment_id: str
    equipment: Equipment | None
    category: str | None
    vna_side: str | None  # port towards the VNA
    dut_side: str | None  # port towards the DUT
    vna_connector: Connector | None
    dut_connector: Connector | None
    reversed: bool = False  # connected by its second port on the VNA side
    automatic: bool = False  # orientation chosen from the connectors

    def text(self) -> str:
        sides = f" ({self.vna_side}→{self.dut_side})" if self.vna_side and self.dut_side else ""
        return f"{self.equipment_id}{sides}"


@dataclass
class ResolvedPath:
    """A path of a configured setup with its equipment, orientation and connectors."""

    key: str  # P1
    definition: PathDefinition
    vna_port: int
    vna_role: str | None
    vna_connector: Connector | None  # connector of the VNA port
    elements: list[PathElement] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)

    @property
    def label(self) -> str:
        return self.definition.display(self.key)

    @property
    def plane(self) -> str:
        return plane_name(self.key, self.vna_port)

    @property
    def end_endpoint(self) -> str:
        """Endpoint (``role.port``) of the DUT-side end of the path: where the reference plane is."""
        last = self.elements[-1] if self.elements else None
        if last is not None:
            return f"{last.role}.{last.dut_side}" if last.dut_side else last.role
        return f"{self.vna_role or 'vna'}.P{self.vna_port}"

    @property
    def end_connector(self) -> Connector | None:
        return self.elements[-1].dut_connector if self.elements else self.vna_connector

    @property
    def dut_connector(self) -> Connector | None:
        """DUT connector that the setup can calibrate on this path."""
        return admitted_dut_connector(self.end_connector)

    def chain(self) -> list[tuple[str, str]]:
        """Connections from the VNA port to the end of the path."""
        connections = []
        previous = f"{self.vna_role or 'vna'}.P{self.vna_port}"
        for element in self.elements:
            if element.vna_side is None or element.dut_side is None:
                previous = element.role
                continue
            connections.append((previous, f"{element.role}.{element.vna_side}"))
            previous = f"{element.role}.{element.dut_side}"
        return connections

    def describe(self) -> str:
        """«P1 (2,92 mm (K) macho) → CAB-PT-292-01 (A→B) → PR1: 2,92 mm (K) hembra»."""
        port = f"P{self.vna_port}" + (f" ({self.vna_connector.label()})" if self.vna_connector else "")
        parts = [port, *(element.text() for element in self.elements)]
        end = self.end_connector
        return " → ".join(parts) + f" → {self.plane}" + (f": {end.label()}" if end else "")

    def to_record(self, library: Library) -> dict[str, Any]:
        """Section of the test record (and of the hardware setup snapshot) that describes the path."""
        entry: dict[str, Any] = {
            "etiqueta": self.label,
            "puerto_vna": self.vna_port,
            "conector_puerto_vna": self.vna_connector.label() if self.vna_connector else None,
            "plano": self.plane,
            "conector_plano": self.end_connector.label() if self.end_connector else None,
            "conector_dut_admitido": self.dut_connector.label() if self.dut_connector else None,
            "elementos": [],
        }
        if self.definition.sense != "ambos":
            entry["sentido"] = self.definition.sense
        for element in self.elements:
            item: dict[str, Any] = {"posicion": element.index, "id": element.equipment_id}
            equipment = element.equipment
            if equipment is not None:
                model = library.model_of(equipment)
                item["modelo"] = model.display_name if model else equipment.model_id
                item["categoria"] = element.category
                if equipment.serial_number:
                    item["numero_serie"] = equipment.serial_number
                if equipment.calibration is not None:
                    item["certificado"] = equipment.calibration.certificate
                    if equipment.calibration.due:
                        item["calibracion_vence"] = equipment.calibration.due.isoformat()
                if equipment.s_parameters is not None:
                    item["datos_s"] = equipment.s_parameters.file.replace("\\", "/").rsplit("/", 1)[-1]
            item["lado_vna"] = element.vna_side
            item["lado_dut"] = element.dut_side
            item["conector_lado_vna"] = element.vna_connector.label() if element.vna_connector else None
            item["conector_lado_dut"] = element.dut_connector.label() if element.dut_connector else None
            if element.reversed:
                item["invertido"] = True
            entry["elementos"].append({k: v for k, v in item.items() if v is not None})
        if self.definition.final:
            entry["final"] = self.definition.final
        return {k: v for k, v in entry.items() if v is not None}


def describe_path_record(entry: dict[str, Any]) -> str:
    """Text of a path kept in a test record (``caminos``), like :meth:`ResolvedPath.describe`."""
    port = f"P{entry.get('puerto_vna', '?')}" + (f" ({entry['conector_puerto_vna']})" if entry.get("conector_puerto_vna") else "")
    parts = [port]
    for element in entry.get("elementos", []):
        sides = f" ({element['lado_vna']}→{element['lado_dut']})" if element.get("lado_vna") and element.get("lado_dut") else ""
        parts.append(f"{element.get('id')}{sides}")
    plane = str(entry.get("plano", "PR?")) + (f": {entry['conector_plano']}" if entry.get("conector_plano") else "")
    text = " → ".join([*parts, plane])
    if entry.get("conector_dut_admitido"):
        text += f" · DUT: {entry['conector_dut_admitido']}"
    if entry.get("final"):
        text += f" · termina en el rol «{entry['final']}»"
    return text


def resolve_paths(template: SetupTemplate, setup: ConfiguredSetup, library: Library) -> list[ResolvedPath]:
    """The paths of the template filled with the equipment of the setup."""
    vna_role = vna_role_of(template)
    vna_ids = setup.ids_for(vna_role) if vna_role else []
    vna = library.equipment.get(vna_ids[0]) if vna_ids else None
    paths = []
    for key, definition in template.paths.items():
        port = definition.port_number(key)
        vna_connector = library.port_connector(vna, f"P{port}") if vna is not None else None
        path = ResolvedPath(key, definition, port, vna_role, vna_connector)
        previous = vna_connector
        for index, item in enumerate(setup.paths.get(key, []), start=1):
            element = _resolve_element(path, index, item.equipment, item.reversed, previous, library)
            path.elements.append(element)
            previous = element.dut_connector
        paths.append(path)
    return paths


def _resolve_element(
    path: ResolvedPath, index: int, equipment_id: str, reversed_: bool, previous: Connector | None, library: Library
) -> PathElement:
    role = element_role(path.key, index)
    equipment = library.equipment.get(equipment_id)
    element = PathElement(index, role, equipment_id, equipment, None, None, None, None, None)
    if equipment is None:
        return element  # reported by the role checks
    element.category = library.category_id_of(equipment)
    ports = [port.name for port in library.ports_of(equipment)]
    if len(ports) != 2:
        path.issues.append(
            Issue(
                Severity.ERROR,
                "camino.puertos",
                f"«{equipment_id}» ({path.label}, posición {index}) tiene {len(ports)} puertos: en un camino solo van "
                "equipos de 2 puertos (cables, adaptadores, paths, preamplificadores…)",
                role=role,
                equipment_id=equipment_id,
            )
        )
        return element
    first, second = ports
    if set(ports) == DIRECTIONAL_PORTS:
        sense = path.definition.sense
        element.vna_side, element.dut_side = ("OUT", "IN") if sense == "rx" else ("IN", "OUT")
        if sense == "ambos":
            path.issues.append(
                Issue(
                    Severity.WARNING,
                    "camino.direccional",
                    f"«{equipment_id}» es direccional (IN → OUT) y {path.label} no indica el sentido de la señal en la plantilla: "
                    "se conecta con la entrada hacia el VNA",
                    role=role,
                    equipment_id=equipment_id,
                )
            )
        if reversed_:
            path.issues.append(
                Issue(
                    Severity.WARNING,
                    "camino.invertido",
                    f"«{equipment_id}» es direccional: su orientación la da el sentido de {path.label}, no «invertido»",
                    role=role,
                    equipment_id=equipment_id,
                )
            )
    elif reversed_:
        element.vna_side, element.dut_side, element.reversed = second, first, True
    else:
        a, b = library.port_connector(equipment, first), library.port_connector(equipment, second)
        if previous is not None and not _mates(previous, a) and _mates(previous, b):
            element.vna_side, element.dut_side, element.reversed, element.automatic = second, first, True, True
        else:
            element.vna_side, element.dut_side = first, second
    element.vna_connector = library.port_connector(equipment, element.vna_side)
    element.dut_connector = library.port_connector(equipment, element.dut_side)
    return element


@dataclass
class ExpandedSetup:
    """A configured setup with its paths as roles, connections and reference planes."""

    template: SetupTemplate  # expanded: path roles, chains in every state, reference planes
    setup: ConfiguredSetup  # expanded: path equipment assigned to the path roles
    paths: list[ResolvedPath]
    dut_ports: list[str] = field(default_factory=list)  # DUT ports measured on P1, P2… (``dut.#1``…)
    other_issues: list[Issue] = field(default_factory=list)

    @property
    def issues(self) -> list[Issue]:
        return [issue for path in self.paths for issue in path.issues] + self.other_issues

    def path(self, key: str) -> ResolvedPath | None:
        return next((p for p in self.paths if p.key == key), None)

    def path_of_role(self, role: str) -> ResolvedPath | None:
        return next((p for p in self.paths if any(e.role == role for e in p.elements)), None)

    def dut_connectors(self) -> dict[str, Connector | None]:
        """Path → DUT connector that the setup can calibrate there."""
        return {path.key: path.dut_connector for path in self.paths}

    def dut_connectors_text(self) -> str:
        parts = []
        for path in self.paths:
            connector = path.dut_connector
            parts.append(f"{path.plane}: {connector.label() if connector else 'sin definir'}")
        return "; ".join(parts)


def default_dut_ports(library: Library, dut: Equipment | None) -> list[str]:
    """Ports of the DUT in the order of its category (those with a connector first)."""
    if dut is None:
        return []
    ports = library.ports_of(dut)
    return [p.name for p in ports if p.connector_field] + [p.name for p in ports if not p.connector_field]


def expand(
    template: SetupTemplate, setup: ConfiguredSetup, library: Library, dut_ports: list[str] | None = None
) -> ExpandedSetup:
    """Template and setup with the paths turned into roles, connections and reference planes."""
    paths = resolve_paths(template, setup, library)
    dut_role = dut_role_of(template)
    dut_ids = setup.ids_for(dut_role) if dut_role else []
    dut = library.equipment.get(dut_ids[0]) if dut_ids else None
    ports = list(dut_ports) if dut_ports else default_dut_ports(library, dut)
    roles: dict[str, Role] = {}
    vna_role = vna_role_of(template)
    for key, role in template.roles.items():
        roles[key] = role
        if key == vna_role:
            roles.update(_path_roles(paths))
    if vna_role is None:
        roles.update(_path_roles(paths))
    ends = {path.key: path.end_endpoint for path in paths}
    chains = [connection for path in paths for connection in path.chain()]

    def endpoint(text: str) -> str:
        if text in ends:
            return ends[text]
        role, _, port = text.partition(".")
        match = DUT_PORT.match(port)
        if role == dut_role and match:
            index = int(match.group(1)) - 1
            return f"{role}.{ports[index]}" if 0 <= index < len(ports) else text
        return text

    states = {}
    for name, state in template.connection_states.items():
        connections = [Connection(a=a, b=b) for a, b in chains]
        connections += [Connection(a=endpoint(c.a), b=endpoint(c.b), kind=c.kind) for c in state.connections]
        states[name] = ConnectionState(description=state.description, connections=connections)
    planes = {path.plane: {"en": [path.end_endpoint], "puerto_vna": path.vna_port, "camino": path.key} for path in paths}
    expanded_template = template.model_copy(update={"roles": roles, "connection_states": states, "reference_planes": planes})
    expanded_template.set_source_path(template.source_path)
    expanded_setup = setup.model_copy(deep=True)
    expanded_setup.set_source_path(setup.source_path)
    for path in paths:
        for element in path.elements:
            expanded_setup.assignments[element.role] = element.equipment_id
    unknown = [
        Issue(Severity.WARNING, "camino.desconocido", f"La plantilla no tiene el camino «{key}»; sus equipos se ignoran")
        for key in setup.paths
        if key not in template.paths
    ]
    return ExpandedSetup(expanded_template, expanded_setup, paths, ports, unknown)


def _path_roles(paths: list[ResolvedPath]) -> dict[str, Role]:
    roles = {}
    for path in paths:
        for element in path.elements:
            roles[element.role] = Role(
                categories=list(path.definition.categories),
                label=f"{path.label} · {element.index}",
                description=f"Equipo {element.index} de {path.label}, desde el VNA",
            )
    return roles

