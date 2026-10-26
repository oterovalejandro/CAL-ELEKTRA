"""Interactive diagram of a hardware setup (ELEKTRA-like, generalised).

Every setup has the same structure: the VNA on the left; from it a line that
splits into its ports (P1, P2…), with the connector of each port; from each
port, its path (one row per port: measurement cables, adapters…, in order);
at the end of each path the reference plane (PR1, PR2…) with its connector
and the DUT connector it admits; then the DUT (or the antennas that end the
paths of a chamber) and the loads that take the free ports of the DUT. The
other roles (cal kit, through, enclosure, positioners…) go in a row below.
Connections are drawn for the selected connection state. Nodes can be
dragged; their positions are stored in the configured setup.

Each node is drawn as in ELEKTRA: its title, a tile with the pictogram of its
family (``equipment_icons``) and, below, the equipment and its connectors;
the connections reach the sides of the tiles. The ports of the EUT are named
where their connections arrive. A directional coupler joined to two planes is
drawn with its four ports: its main line (In, Out) and its coupled line
(Coupl. Fwd, Coupl. Rev), the ports joined to the planes on the left and the
ones that go to the loads on the right, so the diagram shows how it is
really connected.

Templates without paths keep the generic layout: one node per role
(multi-port instruments split per port) placed along the connections.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Callable

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (
    QGraphicsItem,
    QGraphicsPathItem,
    QGraphicsScene,
    QGraphicsSimpleTextItem,
    QGraphicsView,
    QStyleOptionGraphicsItem,
    QWidget,
)

from calelektra.catalog.library import Library
from calelektra.core.issues import Severity
from calelektra.gui import style
from calelektra.gui.equipment_icons import glyph_for, paint_icon, paint_tile
from calelektra.setups.analysis import SetupAnalysis
from calelektra.setups.model import ConfiguredSetup, Connection, Role, SetupTemplate
from calelektra.setups.paths import ExpandedSetup, dut_role_of, expand, vna_role_of

NODE_W, NODE_H = 176.0, 104.0  # width of the texts; title, tile and two lines
TILE = 52.0
TILE_Y = 18.0  # the title goes above the tile
ANCHOR_Y = TILE_Y + TILE / 2  # height where the connections reach a tile
DX, DY = 192.0, 132.0
COUPLER_PORT_X = 8.0  # end of the port stubs of a coupler, from the side of its node
STUB = 12.0
MIN_FIT_SCALE = 0.8
PATH_ROLE = "camino:"  # role emitted when a port or a plane is clicked: the path of the port
PORT_LABEL = QColor("#6a4c99")


@dataclass
class CouplerLayout:
    """A 4-port coupler drawn with its ports: the corner of each one («lt», «lb», «rt», «rb»)."""

    corners: dict[str, str]
    lines: list[tuple[str, str]]  # main line (In, Out) and coupled line (Coupl. Fwd, Coupl. Rev)


@dataclass
class NodeSpec:
    key: str
    role: str
    title: str
    lines: list[str]
    kind: str  # category kind (instrumento, pasivo…), or «puerto» / «plano» of a path
    severity: Severity | None
    assigned: bool
    optional: bool
    is_dut: bool
    width: float = NODE_W
    height: float = NODE_H
    glyph: str = "generico"
    coupler: CouplerLayout | None = None


@dataclass
class EdgeSpec:
    a: str
    b: str
    kind: str = "coaxial"
    a_port: str | None = None  # port of the EUT at the end ``a`` (named on the diagram)
    b_port: str | None = None


@dataclass
class DiagramModel:
    nodes: dict[str, NodeSpec] = field(default_factory=dict)
    edges: list[EdgeSpec] = field(default_factory=list)
    mounts: list[tuple[str, str]] = field(default_factory=list)  # (positioner, mounted)
    positions: dict[str, tuple[float, float]] = field(default_factory=dict)  # fixed layout (setups with paths)


def build_diagram_model(
    template: SetupTemplate,
    setup: ConfiguredSetup,
    library: Library,
    analysis: SetupAnalysis | None,
    state_name: str | None,
    expanded: ExpandedSetup | None = None,
) -> DiagramModel:
    """Nodes, edges and (for setups with paths) positions of the diagram of a setup.

    ``expanded``: the setup expanded for one measurement of the EUT (a pair of
    ports of a coupler), to draw how the EUT is connected for it.
    """
    if expanded is None and analysis is not None:
        expanded = analysis.expanded
    if expanded is None and template.paths:
        expanded = expand(template, setup, library)
    if expanded is not None and expanded.paths:
        return _path_diagram_model(expanded, library, analysis, state_name)
    return _role_diagram_model(template, setup, library, analysis, state_name)


def _describe(library: Library, equipment_id: str) -> str:
    equipment = library.equipment.get(equipment_id)
    model = library.model_of(equipment) if equipment else None
    return f"{equipment_id} — {model.designation}" if model else equipment_id


def equipment_glyph(library: Library, equipment_id: str) -> str | None:
    """Pictogram of an equipment of the library: that of its category and type (``None`` if it does not exist)."""
    equipment = library.equipment.get(equipment_id)
    if equipment is None:
        return None
    return glyph_for(library.category_id_of(equipment), library.field(equipment, "tipo"))


def _role_glyph(library: Library, role: Role, ids: list[str]) -> str:
    """Pictogram of a role: that of its first equipment or, unassigned, that of its categories (and allowed type).

    A role open to categories with different pictograms (the EUT of a generic
    template) shows a generic component until it is assigned.
    """
    glyph = equipment_glyph(library, ids[0]) if ids else None
    if glyph is None:
        kind = role.allowed_types[0] if role.allowed_types else None
        glyphs = {glyph_for(category, kind) for category in role.categories} or {"generico"}
        glyph = glyphs.pop() if len(glyphs) == 1 else "generico"
    return glyph


def _port_names(library: Library, role: Role, ids: list[str]) -> list[str]:
    """Port names of the equipment of a role or, unassigned, of its categories when they all have the same ones."""
    equipment = library.equipment.get(ids[0]) if ids else None
    if equipment is not None:
        return [port.name for port in library.ports_of(equipment)]
    lists = {tuple(port.name for port in category.port_defs({})) for category in map(library.categories.get, role.categories) if category}
    return list(lists.pop()) if len(lists) == 1 else []


def _port_label(port: str | None, names: list[str]) -> str | None:
    """Name of a port of the EUT: ``#2`` is the second port of its category (none while it is not known)."""
    if not port:
        return None
    if port.startswith("#") and port[1:].isdigit():
        index = int(port[1:]) - 1
        return names[index] if 0 <= index < len(names) else None
    return port


def coupler_layout(ports: list[str], links: dict[str, tuple[float, float]]) -> CouplerLayout:
    """Corners of the ports of a 4-port coupler (In, Out, Coupl. Fwd, Coupl. Rev, in this order).

    ``links``: for each connected port, where what it is connected to lies
    (x, y, relative to the coupler). The ports joined to something on the left
    (the planes) take the left corners, top first; the others the right ones,
    so that the main line and the coupled line are two parallel lines: both
    vertical when the two ports on the left are the ends of one line, both
    horizontal otherwise. In and Coupl. Fwd are at the same end of the
    coupler, Out and Coupl. Rev at the other.
    """
    main, coupled = (ports[0], ports[1]), (ports[2], ports[3])
    line = {port: pair for pair in (main, coupled) for port in pair}
    other = {port: pair[1 - pair.index(port)] for pair in (main, coupled) for port in pair}
    partner = {main[0]: coupled[0], coupled[0]: main[0], main[1]: coupled[1], coupled[1]: main[1]}
    left = sorted((port for port, (x, _y) in links.items() if x < 0 and port in line), key=lambda port: links[port][1])
    if len(left) >= 2 and line[left[0]] == line[left[1]]:
        top, bottom = left[0], left[1]
        corners = {top: "lt", bottom: "lb", partner[top]: "rt", partner[bottom]: "rb"}
    elif len(left) >= 2:
        top, bottom = left[0], left[1]
        corners = {top: "lt", other[top]: "rt", bottom: "lb", other[bottom]: "rb"}
    elif left:
        top = left[0]
        corners = {top: "lt", other[top]: "rt", partner[top]: "lb", other[partner[top]]: "rb"}
    else:
        corners = {main[0]: "lt", main[1]: "rt", coupled[0]: "lb", coupled[1]: "rb"}
    return CouplerLayout(corners, [main, coupled])


def _path_diagram_model(expanded: ExpandedSetup, library: Library, analysis: SetupAnalysis | None, state_name: str | None) -> DiagramModel:
    template, setup = expanded.template, expanded.setup
    model = DiagramModel()
    severity_of = analysis.role_severity if analysis is not None else {}

    def kind_of(role_key: str) -> str:
        role = template.roles[role_key]
        category = library.categories.get(role.categories[0]) if role.categories else None
        return category.kind if category else "pasivo"

    def add_role(key: str) -> None:
        role = template.roles[key]
        lines, assigned = _assignment_lines(key, role.per_segment, setup, library, role.is_dut)
        model.nodes[key] = NodeSpec(
            key, key, role.display(key), lines, kind_of(key), severity_of.get(key), assigned, not role.required, role.is_dut,
            glyph=_role_glyph(library, role, setup.ids_for(key)),
        )

    vna_role = vna_role_of(template)
    path_roles = {element.role for path in expanded.paths for element in path.elements}
    if vna_role is not None:
        add_role(vna_role)
    chains: set[tuple[str, str]] = set()
    ends: dict[str, str] = {}
    widest = max((len(path.elements) for path in expanded.paths), default=0)
    plane_x = (2 + widest) * DX  # the planes of all the paths in one column
    rows = {}
    for row, path in enumerate(expanded.paths):
        y = row * DY
        rows[path.key] = y
        problems = [issue.severity for issue in path.issues]
        port_key = f"puerto:{path.key}"
        connector = path.vna_connector.label() if path.vna_connector else "conector sin definir"
        model.nodes[port_key] = NodeSpec(
            port_key, PATH_ROLE + path.key, f"P{path.vna_port}", [connector, path.label], "puerto",
            max(problems) if problems else None, True, False, False, glyph="puerto",
        )
        model.positions[port_key] = (DX, y)
        if vna_role is not None:
            model.edges.append(EdgeSpec(vna_role, port_key))
        previous = port_key
        for index, element in enumerate(path.elements):
            add_role(element.role)
            node = model.nodes[element.role]
            node.lines = [_describe(library, element.equipment_id)]
            node.glyph = equipment_glyph(library, element.equipment_id) or node.glyph
            if element.vna_side and element.dut_side:
                end = f" · {element.dut_connector.label()}" if element.dut_connector else ""
                node.lines.append(f"{element.vna_side} → {element.dut_side}{end}")
            model.positions[element.role] = ((2 + index) * DX, y)
            model.edges.append(EdgeSpec(previous, element.role))
            previous = element.role
        plane_key = f"plano:{path.plane}"
        end_connector = path.end_connector.label() if path.end_connector else "conector sin definir"
        dut_connector = path.dut_connector.label() if path.dut_connector else "—"
        model.nodes[plane_key] = NodeSpec(
            plane_key, PATH_ROLE + path.key, f"Plano {path.plane}", [end_connector, f"DUT: {dut_connector}"], "plano",
            None, True, False, False, glyph="plano",
        )
        model.positions[plane_key] = (plane_x, y)
        model.edges.append(EdgeSpec(previous, plane_key))
        ends[path.end_endpoint] = plane_key
        chains.update(path.chain())
    if vna_role is not None:
        model.positions[vna_role] = (0.0, (min(rows.values()) + max(rows.values())) / 2 if rows else 0.0)

    for key in template.roles:
        if key not in model.nodes and key not in path_roles:
            add_role(key)
    state = template.connection_states.get(state_name) if state_name else None

    def node_for(endpoint: str) -> str | None:
        if endpoint in ends:
            return ends[endpoint]
        role = endpoint.partition(".")[0]
        return role if role in model.nodes else None

    names: dict[str, list[str]] = {}

    def port_of(endpoint: str) -> str | None:
        """Name of the port of the EUT at an endpoint (``dut.#1`` → In); other equipment: no name."""
        role, _, port = endpoint.partition(".")
        if role not in template.roles or not template.roles[role].is_dut or not port:
            return None
        if role not in names:
            names[role] = _port_names(library, template.roles[role], setup.ids_for(role))
        return _port_label(port, names[role])

    attached: dict[str, list[str]] = {}  # node → plane nodes or nodes it is connected to in the state
    for connection in state.connections if state else []:
        if (connection.a, connection.b) in chains:
            continue
        a, b = node_for(connection.a), node_for(connection.b)
        if a is None or b is None or a == b:
            continue
        model.edges.append(EdgeSpec(a, b, connection.kind, port_of(connection.a), port_of(connection.b)))
        attached.setdefault(a, []).append(b)
        attached.setdefault(b, []).append(a)

    # after the planes: what is connected to them (DUT, antennas…), then what is connected to those (loads)
    after_x = plane_x + DX if ends else 2 * DX
    placed = set(model.positions)
    plane_rows = {plane: model.positions[plane][1] for plane in ends.values()}
    first_ring = [key for key in model.nodes if key not in placed and any(other in plane_rows for other in attached.get(key, []))]
    for key in first_ring:
        ys = [plane_rows[other] for other in attached[key] if other in plane_rows]
        model.positions[key] = (after_x, sum(ys) / len(ys))
        if model.nodes[key].glyph == "acoplador" and key in template.roles:
            _draw_as_coupler(model, key, after_x, plane_rows, _port_names(library, template.roles[key], setup.ids_for(key)))
    placed = set(model.positions)
    second_x = after_x + DX
    row = 0
    for key in [k for k in model.nodes if k not in placed and any(other in first_ring for other in attached.get(k, []))]:
        model.positions[key] = (second_x, row * DY)
        placed.add(key)
        row += 1
    for key, role in template.roles.items():
        if role.mounted_on and role.mounted_on in model.nodes and key in model.nodes:
            model.mounts.append((role.mounted_on, key))
            if role.mounted_on not in placed and key in placed:
                x, y = model.positions[key]
                model.positions[role.mounted_on] = (x + DX, y)
                placed.add(role.mounted_on)
    bottom = max((y + model.nodes[key].height for key, (_x, y) in model.positions.items()), default=0.0) + DY - NODE_H
    for index, key in enumerate(k for k in model.nodes if k not in placed):
        model.positions[key] = (index * DX, bottom)
    return model


def _draw_as_coupler(model: DiagramModel, key: str, x: float, plane_rows: dict[str, float], ports: list[str]) -> None:
    """A 4-port coupler joined to two planes: drawn with its ports between their rows, the loads in front of its ports.

    ``ports``: its port names in the order of its category (In, Out, Coupl. Fwd, Coupl. Rev).
    """
    links: dict[str, tuple[float, float]] = {}
    for edge in model.edges:
        for own, port, other in ((edge.a, edge.a_port, edge.b), (edge.b, edge.b_port, edge.a)):
            if own == key and port and other in plane_rows:
                links[port] = (model.positions[other][0] - x, model.positions[other][1])
    rows = sorted({y for _dx, y in links.values()})
    if len(ports) != 4 or len(rows) != 2:
        return
    node = model.nodes[key]
    node.coupler = coupler_layout(ports, links)
    node.height = rows[1] - rows[0] + NODE_H
    model.positions[key] = (x, rows[0])
    for edge in model.edges:  # the loads in front of their ports
        for own, port, other in ((edge.a, edge.a_port, edge.b), (edge.b, edge.b_port, edge.a)):
            corner = node.coupler.corners.get(port or "")
            if own == key and corner and corner[0] == "r" and other not in plane_rows:
                model.positions[other] = (x + DX, rows[0] if corner[1] == "t" else rows[1])


def _role_diagram_model(
    template: SetupTemplate,
    setup: ConfiguredSetup,
    library: Library,
    analysis: SetupAnalysis | None,
    state_name: str | None,
) -> DiagramModel:
    model = DiagramModel()
    state = template.connection_states.get(state_name) if state_name else None
    connections: list[Connection] = state.connections if state else []

    ports_used: dict[str, set[str]] = {}
    for connection in connections:
        for endpoint in (connection.a, connection.b):
            role, port = Connection.endpoint(endpoint)
            ports_used.setdefault(role, set()).add(port or "")

    def node_key(role_key: str, port: str | None) -> str:
        return f"{role_key}.{port}" if port and role_key in split_roles else role_key

    split_roles: set[str] = set()
    for key, role in template.roles.items():
        category = library.categories.get(role.categories[0]) if role.categories else None
        if category and category.kind == "instrumento" and len(ports_used.get(key, ())) > 1:
            split_roles.add(key)

    for key, role in template.roles.items():
        category = library.categories.get(role.categories[0]) if role.categories else None
        kind = category.kind if category else "pasivo"
        severity = analysis.role_severity.get(key) if analysis else None
        lines, assigned = _assignment_lines(key, role.per_segment, setup, library, role.is_dut)
        glyph = _role_glyph(library, role, setup.ids_for(key))
        ports = sorted(ports_used.get(key, ())) if key in split_roles else [None]
        for port in ports:
            title = role.display(key) + (f" · {port}" if port else "")
            model.nodes[node_key(key, port)] = NodeSpec(
                key=node_key(key, port),
                role=key,
                title=title,
                lines=lines,
                kind=kind,
                severity=severity,
                assigned=assigned,
                optional=not role.required,
                is_dut=role.is_dut,
                glyph=glyph,
            )
    for connection in connections:
        (role_a, port_a), (role_b, port_b) = Connection.endpoint(connection.a), Connection.endpoint(connection.b)
        a, b = node_key(role_a, port_a), node_key(role_b, port_b)
        if a in model.nodes and b in model.nodes:
            model.edges.append(EdgeSpec(a, b, connection.kind))
    for key, role in template.roles.items():
        if role.mounted_on and role.mounted_on in model.nodes and key in model.nodes:
            model.mounts.append((role.mounted_on, key))
    return model


def _assignment_lines(key: str, per_segment: bool, setup: ConfiguredSetup, library: Library, is_dut: bool = False) -> tuple[list[str], bool]:
    def describe(equipment_id: str) -> str:
        equipment = library.equipment.get(equipment_id)
        model = library.model_of(equipment) if equipment else None
        return f"{equipment_id} — {model.designation}" if model else equipment_id

    if per_segment and setup.segments:
        lines = []
        assigned = True
        for segment in setup.segments:
            ids = setup.ids_for(key, segment)
            assigned = assigned and bool(ids)
            lines.append(f"{segment.name}: {', '.join(ids) if ids else '—'}")
        return lines, assigned
    ids = setup.ids_for(key)
    return ([describe(i) for i in ids] or ["lo define la plantilla de ensayo" if is_dut else "sin asignar"]), bool(ids)


def compute_layout(model: DiagramModel) -> dict[str, tuple[float, float]]:
    if model.positions:
        return dict(model.positions)
    adjacency: dict[str, list[str]] = {}
    for edge in model.edges:
        adjacency.setdefault(edge.a, []).append(edge.b)
        adjacency.setdefault(edge.b, []).append(edge.a)

    grid: dict[str, tuple[int, int]] = {}
    occupied: set[tuple[int, int]] = set()

    def place(key: str, col: int, row: int) -> None:
        while (col, row) in occupied:
            row += 1
        grid[key] = (col, row)
        occupied.add((col, row))

    start_row = 0
    for start in [edge.a for edge in model.edges] + list(adjacency):
        if start in grid:
            continue
        chain = _longest_chain(start, adjacency)
        for index, key in enumerate(chain):
            place(key, index, start_row)
        queue = deque(chain)
        while queue:
            current = queue.popleft()
            for neighbour in adjacency.get(current, []):
                if neighbour not in grid:
                    place(neighbour, grid[current][0], grid[current][1] + 1)
                    queue.append(neighbour)
        start_row = max(row for _, row in grid.values()) + 1

    mounted_positioners = {positioner for positioner, _ in model.mounts}
    rest = [k for k in model.nodes if k not in grid and k not in mounted_positioners]
    bottom = (max((row for _, row in grid.values()), default=-1) + 1) + (1 if model.mounts else 0)
    for index, key in enumerate(rest):
        place(key, index, bottom)
    for positioner, mounted in model.mounts:
        if positioner not in grid and mounted in grid:
            place(positioner, grid[mounted][0], grid[mounted][1] + 1)
    for key in model.nodes:
        if key not in grid:
            place(key, 0, bottom + 1)
    return {key: (col * DX, row * DY) for key, (col, row) in grid.items()}


def _longest_chain(start: str, adjacency: dict[str, list[str]]) -> list[str]:
    best: list[str] = [start]

    def dfs(node: str, path: list[str], visited: set[str]) -> None:
        nonlocal best
        if len(path) > len(best):
            best = list(path)
        for neighbour in adjacency.get(node, []):
            if neighbour not in visited:
                visited.add(neighbour)
                path.append(neighbour)
                dfs(neighbour, path, visited)
                path.pop()
                visited.discard(neighbour)

    dfs(start, [start], {start})
    return best


def _font(size: float, bold: bool = False) -> QFont:
    font = QFont()
    font.setPointSizeF(size)
    font.setBold(bold)
    return font


class NodeItem(QGraphicsItem):
    def __init__(self, spec: NodeSpec, on_moved: Callable[[str, QPointF], None]):
        super().__init__()
        self.spec = spec
        self._on_moved = on_moved
        self.edges: list[EdgeItem] = []
        self.setFlags(
            QGraphicsItem.GraphicsItemFlag.ItemIsMovable
            | QGraphicsItem.GraphicsItemFlag.ItemIsSelectable
            | QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges
        )
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("\n".join([spec.title, *spec.lines]))

    def boundingRect(self) -> QRectF:  # noqa: N802 (Qt API)
        return QRectF(-2, -2, self.spec.width + 4, self.spec.height + 4)

    def tile_rect(self) -> QRectF:
        return QRectF((self.spec.width - TILE) / 2, TILE_Y, TILE, TILE)

    def state_border(self) -> tuple[QColor | None, bool]:
        """Colour of the tile border for the state of the role (``None``: normal) and whether it is dashed."""
        if self.spec.kind == "plano":
            return style.INDIGO, True
        if not self.spec.assigned:
            return (style.NEUTRAL if self.spec.optional else (style.DUT if self.spec.is_dut else style.ERROR)), True
        if self.spec.severity is not None and self.spec.severity >= Severity.WARNING:
            return style.SEVERITY_COLORS[self.spec.severity], False
        return None, False

    def info_lines(self) -> list[str]:
        if not self.spec.assigned and self.spec.optional:
            return ["(opcional) sin asignar"]
        return list(self.spec.lines)

    def paint(self, painter: QPainter, option: QStyleOptionGraphicsItem, widget: QWidget | None = None) -> None:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if self.spec.coupler is not None:
            self._paint_coupler(painter)
            return
        self._paint_title(painter)
        border, dashed = self.state_border()
        tile = self.tile_rect()
        paint_icon(painter, tile, self.spec.glyph, self.isSelected(), not self.spec.assigned, border, dashed)
        self._paint_lines(painter, tile.bottom() + 4)
        if self.spec.severity is not None and self.spec.severity >= Severity.WARNING and self.spec.assigned:
            painter.setBrush(style.SEVERITY_COLORS[self.spec.severity])
            painter.setPen(QPen(QColor("#ffffff"), 1.5))
            painter.drawEllipse(QPointF(tile.right() - 2, tile.top() + 2), 6, 6)

    def _paint_title(self, painter: QPainter) -> None:
        painter.setFont(_font(8, bold=True))
        painter.setPen(style.DUT if self.spec.is_dut else (style.INDIGO if self.spec.kind == "plano" else QColor(style.TEXT)))
        title = self.spec.title + (" · DUT" if self.spec.is_dut and "DUT" not in self.spec.title else "")
        painter.drawText(
            QRectF(0, 0, self.spec.width, TILE_Y - 2),
            Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
            painter.fontMetrics().elidedText(title, Qt.TextElideMode.ElideRight, int(self.spec.width)),
        )

    def _paint_lines(self, painter: QPainter, top: float) -> None:
        painter.setFont(_font(7.5))
        painter.setPen(QColor(style.TEXT) if self.spec.assigned else QColor(style.MUTED))
        room = int((self.spec.height - top + 1) // 13)
        for index, line in enumerate(self.info_lines()[: max(room, 0)]):
            painter.drawText(
                QRectF(0, top + index * 13, self.spec.width, 13),
                Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
                painter.fontMetrics().elidedText(line, Qt.TextElideMode.ElideRight, int(self.spec.width)),
            )

    # — directional coupler drawn with its ports —

    def coupler_points(self) -> dict[str, QPointF]:
        """End of the stub of each corner (where the connections arrive), in the coordinates of the node."""
        width, top = self.spec.width, ANCHOR_Y
        bottom = self.spec.height - NODE_H + ANCHOR_Y
        return {
            "lt": QPointF(COUPLER_PORT_X, top),
            "lb": QPointF(COUPLER_PORT_X, bottom),
            "rt": QPointF(width - COUPLER_PORT_X, top),
            "rb": QPointF(width - COUPLER_PORT_X, bottom),
        }

    def _paint_coupler(self, painter: QPainter) -> None:
        layout = self.spec.coupler
        assert layout is not None
        points = self.coupler_points()
        left, right = COUPLER_PORT_X + STUB, self.spec.width - COUPLER_PORT_X - STUB
        top, bottom = points["lt"].y(), points["lb"].y()
        box = QRectF(left, top - 20, right - left, bottom - top + 40)
        self._paint_title(painter)
        border, dashed = self.state_border()
        selected = self.isSelected()
        ink = paint_tile(painter, box, selected, border, dashed, radius=8)  # the box: a tile, like the other nodes

        def line_pen(width: float) -> QPen:
            pen = QPen(ink, width)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            return pen

        side_x = {"l": left, "r": right}
        inner_x = {"l": left + 18, "r": right - 18}
        stub_pen = QPen(QColor("#34495e"), 2.2)  # outside the box: the colour of the connections
        stub_pen.setCapStyle(Qt.PenCapStyle.FlatCap)
        for corner, point in points.items():  # stubs: from the box to where the connections arrive
            painter.setPen(stub_pen)
            painter.drawLine(point, QPointF(side_x[corner[0]], point.y()))
        middle: list[tuple[QPointF, QPointF]] = []  # the two lines, as (start, end) of their middle part
        for index, (a, b) in enumerate(layout.lines):
            ca, cb = layout.corners[a], layout.corners[b]
            ya, yb = points[ca].y(), points[cb].y()
            path = QPainterPath()
            if ca[0] == cb[0]:  # both ends on one side: a vertical line
                x = inner_x[ca[0]]
                path.moveTo(side_x[ca[0]], ya)
                path.lineTo(x, ya)
                path.lineTo(x, yb)
                path.lineTo(side_x[cb[0]], yb)
                middle.append((QPointF(x, ya), QPointF(x, yb)))
            else:
                path.moveTo(side_x[ca[0]], ya)
                path.lineTo(side_x[cb[0]], yb)
                middle.append((QPointF(side_x[ca[0]], ya), QPointF(side_x[cb[0]], yb)))
            painter.setPen(line_pen(2.6 if index == 0 else 1.8))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(path)
        # coupling: two arrows from the main line to the coupled line
        (m0, m1), (c0, c1) = (sorted(ends, key=lambda point: (point.x(), point.y())) for ends in middle)
        painter.setPen(line_pen(1.4))
        for t in (0.38, 0.62):
            start = QPointF(m0.x() + (m1.x() - m0.x()) * t, m0.y() + (m1.y() - m0.y()) * t)
            end = QPointF(c0.x() + (c1.x() - c0.x()) * t, c0.y() + (c1.y() - c0.y()) * t)
            direction = QPointF(end.x() - start.x(), end.y() - start.y())
            length = max((direction.x() ** 2 + direction.y() ** 2) ** 0.5, 1.0)
            ux, uy = direction.x() / length, direction.y() / length
            start = QPointF(start.x() + ux * 8, start.y() + uy * 8)
            end = QPointF(end.x() - ux * 8, end.y() - uy * 8)
            painter.drawLine(start, end)
            for side in (-1, 1):
                painter.drawLine(end, QPointF(end.x() - ux * 6 - side * uy * 4, end.y() - uy * 6 + side * ux * 4))
        # port names, inside the box next to their stub
        painter.setFont(_font(7.5, bold=True))
        painter.setPen(ink if selected else PORT_LABEL)
        for port, corner in layout.corners.items():
            point = points[corner]
            rect = QRectF(left + 4, point.y() - 16, right - left - 8, 14) if corner[1] == "t" else QRectF(left + 4, point.y() + 2, right - left - 8, 14)
            align = Qt.AlignmentFlag.AlignLeft if corner[0] == "l" else Qt.AlignmentFlag.AlignRight
            painter.drawText(rect, align | Qt.AlignmentFlag.AlignVCenter, port)
        self._paint_lines(painter, box.bottom() + 4)

    # — where the connections arrive —

    def anchor_for(self, edge: EdgeItem) -> tuple[QPointF, QPointF]:
        """Point (scene) where ``edge`` reaches this node and the direction it leaves in."""
        port = edge.a_port if edge.a is self else edge.b_port
        other = edge.b if edge.a is self else edge.a
        if self.spec.coupler is not None and port in self.spec.coupler.corners:
            corner = self.spec.coupler.corners[port]
            return self.mapToScene(self.coupler_points()[corner]), QPointF(-1.0 if corner[0] == "l" else 1.0, 0.0)
        side = self._side_toward(other)
        siblings = [e for e in self.edges if self._side_toward(e.b if e.a is self else e.a) == side]
        tile = self.tile_rect()
        if side in ("left", "right"):
            siblings.sort(key=lambda e: (e.b if e.a is self else e.a).tile_center().y())
            offset = 0.0
            if len(siblings) > 1:
                step = min(14.0, (TILE - 12) / (len(siblings) - 1))
                offset = (siblings.index(edge) - (len(siblings) - 1) / 2) * step
            x = tile.left() if side == "left" else tile.right()
            return self.mapToScene(QPointF(x, tile.center().y() + offset)), QPointF(-1.0 if side == "left" else 1.0, 0.0)
        y = 0.0 if side == "top" else self.spec.height
        return self.mapToScene(QPointF(tile.center().x(), y)), QPointF(0.0, -1.0 if side == "top" else 1.0)

    def tile_center(self) -> QPointF:
        return self.mapToScene(self.tile_rect().center())

    def _side_toward(self, other: NodeItem) -> str:
        here, there = self.tile_center(), other.tile_center()
        dx, dy = there.x() - here.x(), there.y() - here.y()
        if abs(dy) > NODE_H and abs(dx) < NODE_W:
            return "bottom" if dy > 0 else "top"
        return "right" if dx >= 0 else "left"

    def itemChange(self, change, value):  # noqa: N802 (Qt API)
        if change == QGraphicsItem.GraphicsItemChange.ItemPositionHasChanged:
            for edge in {e for own in self.edges for node in (own.a, own.b) for e in node.edges}:
                edge.update_path()
            self._on_moved(self.spec.key, self.pos())
        return super().itemChange(change, value)


class EdgeItem(QGraphicsPathItem):
    def __init__(self, a: NodeItem, b: NodeItem, kind: str, a_port: str | None = None, b_port: str | None = None):
        super().__init__()
        self.a, self.b, self.kind = a, b, kind
        self.a_port, self.b_port = a_port, b_port
        pen = QPen(QColor("#34495e"), 1.8)
        if kind == "radiado":
            pen = QPen(style.ACCENT, 2, Qt.PenStyle.DotLine)
        elif kind == "montaje":
            pen = QPen(QColor("#8d6e63"), 1.5, Qt.PenStyle.DashLine)
        self.setPen(pen)
        self.setZValue(-1)
        self.label = QGraphicsSimpleTextItem("campo" if kind == "radiado" else "", self)
        self.label.setBrush(style.ACCENT)
        self.port_labels: list[tuple[QGraphicsSimpleTextItem, NodeItem]] = []
        for node, port in ((a, a_port), (b, b_port)):
            if port and node.spec.coupler is None:  # a coupler names its ports itself
                text = QGraphicsSimpleTextItem(port, self)
                text.setBrush(PORT_LABEL)
                text.setFont(_font(7.5, bold=True))
                self.port_labels.append((text, node))
        a.edges.append(self)
        b.edges.append(self)
        self.update_path()

    def update_path(self) -> None:
        start, out = self.a.anchor_for(self)
        end, into = self.b.anchor_for(self)
        path = QPainterPath(start)
        middle = QPointF((start.x() + end.x()) / 2, (start.y() + end.y()) / 2)
        label_at = QPointF(middle.x(), middle.y() - 18)
        if self.kind == "radiado" and abs(start.y() - end.y()) < 1:
            # arc above both antennas, so the link and its label stay visible
            top_a = self.a.mapToScene(QPointF(self.a.spec.width / 2, 0))
            top_b = self.b.mapToScene(QPointF(self.b.spec.width / 2, 0))
            path = QPainterPath(top_a)
            path.cubicTo(QPointF(top_a.x(), top_a.y() - 55), QPointF(top_b.x(), top_b.y() - 55), top_b)
            label_at = QPointF((top_a.x() + top_b.x()) / 2, top_a.y() - 60)
        elif self.kind == "montaje" or (abs(start.y() - end.y()) < 1 and out.y() == 0 and into.y() == 0):
            path.lineTo(end)
        else:
            reach = max(30.0, (abs(end.x() - start.x()) + abs(end.y() - start.y())) / 3)
            path.cubicTo(
                QPointF(start.x() + out.x() * reach, start.y() + out.y() * reach),
                QPointF(end.x() + into.x() * reach, end.y() + into.y() * reach),
                end,
            )
        self.setPath(path)
        bounds = self.label.boundingRect()
        if self.kind == "radiado" and abs(start.y() - end.y()) >= 1:  # between two rows: beside the link
            self.label.setPos(middle.x() + 6, middle.y() - bounds.height() / 2)
        else:
            self.label.setPos(label_at.x() - bounds.width() / 2, label_at.y())
        for text, node in self.port_labels:
            point, direction = node.anchor_for(self)
            bounds = text.boundingRect()
            if direction.x() < 0:  # on the left of the tile: right-aligned, above the connection
                text.setPos(point.x() - bounds.width() - 4, point.y() - bounds.height() - 1)
            elif direction.x() > 0:
                text.setPos(point.x() + 4, point.y() - bounds.height() - 1)
            else:
                text.setPos(point.x() + 4, point.y() + (2 if direction.y() > 0 else -bounds.height() - 2))


def _populate(
    scene: QGraphicsScene,
    model: DiagramModel,
    saved: dict[str, list[float]],
    on_moved: Callable[[str, QPointF], None],
    selected_role: str | None = None,
) -> tuple[dict[str, NodeItem], list[EdgeItem]]:
    """Nodes (at their saved position, if any) and connections of a diagram in a scene."""
    positions = compute_layout(model)
    nodes: dict[str, NodeItem] = {}
    for key, spec in model.nodes.items():
        item = NodeItem(spec, on_moved)
        position = saved.get(key)
        x, y = (position[0], position[1]) if position and len(position) == 2 else positions[key]
        item.setPos(x, y)
        scene.addItem(item)
        nodes[key] = item
        if selected_role and spec.role == selected_role:
            item.setSelected(True)
    edges = [EdgeItem(nodes[edge.a], nodes[edge.b], edge.kind, edge.a_port, edge.b_port) for edge in model.edges]
    edges += [EdgeItem(nodes[mounted], nodes[positioner], "montaje") for positioner, mounted in model.mounts]
    for edge in edges:  # now that every node knows all its connections
        edge.update_path()
        scene.addItem(edge)
    return nodes, edges


def connection_picture(expanded: ExpandedSetup, library: Library, state_name: str = "medida") -> QPixmap | None:
    """How the EUT is connected in a state: the planes, the EUT and what takes its free ports (the loads).

    The same drawing as the diagram of the setup, cut to the end of the paths:
    for the operator requests (each pair of ports of a coupler).
    """
    model = _path_diagram_model(expanded, library, None, state_name)
    dut = dut_role_of(expanded.template)
    if dut is None or dut not in model.nodes:
        return None
    keep = {dut} | {key for key in model.nodes if key.startswith("plano:")}
    keep |= {edge.b for edge in model.edges if edge.a == dut} | {edge.a for edge in model.edges if edge.b == dut}
    scene = QGraphicsScene()
    nodes, _edges = _populate(scene, model, {}, lambda _key, _pos: None)
    area = QRectF()
    for key in keep:
        area = area.united(nodes[key].sceneBoundingRect())
    area.adjust(-8, -6, 8, 6)  # and the end of the paths that arrive at the planes
    ratio = 2
    image = QPixmap(int(area.width() * ratio), int(area.height() * ratio))
    image.fill(QColor("#ffffff"))
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    scene.render(painter, QRectF(0, 0, image.width(), image.height()), area)
    painter.end()
    image.setDevicePixelRatio(ratio)
    return image


class SetupDiagramView(QGraphicsView):
    role_clicked = Signal(str)
    node_moved = Signal(str, float, float)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setScene(QGraphicsScene(self))
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setBackgroundBrush(QColor("#f8fafc"))
        self.nodes: dict[str, NodeItem] = {}
        self.edges: list[EdgeItem] = []
        self._building = False
        self.scene().selectionChanged.connect(self._selection_changed)

    def show_setup(
        self,
        template: SetupTemplate,
        setup: ConfiguredSetup,
        library: Library,
        analysis: SetupAnalysis | None,
        state_name: str | None,
        selected_role: str | None = None,
        expanded: ExpandedSetup | None = None,
    ) -> None:
        self._building = True
        self.scene().clear()
        self.nodes = {}
        self.edges = []
        model = build_diagram_model(template, setup, library, analysis, state_name, expanded)
        self.nodes, self.edges = _populate(self.scene(), model, setup.layout, self._moved, selected_role)
        bounds = self.scene().itemsBoundingRect().adjusted(-40, -40, 40, 40)
        self.scene().setSceneRect(bounds)
        self._building = False

    def fit(self) -> None:
        """Fit the whole setup, but never below a readable zoom."""
        self.fitInView(self.scene().sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)
        scale = self.transform().m11()
        if scale < MIN_FIT_SCALE:
            self.scale(MIN_FIT_SCALE / scale, MIN_FIT_SCALE / scale)
            self.horizontalScrollBar().setValue(self.horizontalScrollBar().minimum())
            self.verticalScrollBar().setValue(self.verticalScrollBar().minimum())
        elif scale > 1.0:
            self.resetTransform()

    def wheelEvent(self, event) -> None:  # noqa: N802 (Qt API)
        factor = 1.15 ** (event.angleDelta().y() / 120)
        scale = self.transform().m11() * factor
        if 0.2 <= scale <= 3.0:
            self.scale(factor, factor)

    def _moved(self, key: str, pos: QPointF) -> None:
        if not self._building:
            self.node_moved.emit(key, pos.x(), pos.y())

    def _selection_changed(self) -> None:
        if self._building:
            return
        selected = [item for item in self.scene().selectedItems() if isinstance(item, NodeItem)]
        if selected:
            self.role_clicked.emit(selected[0].spec.role)
