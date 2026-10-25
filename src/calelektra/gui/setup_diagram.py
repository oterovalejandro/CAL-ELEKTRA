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

Templates without paths keep the generic layout: one node per role
(multi-port instruments split per port) placed along the connections.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Callable

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QPainter, QPainterPath, QPen
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
from calelektra.setups.analysis import SetupAnalysis
from calelektra.setups.model import ConfiguredSetup, Connection, SetupTemplate
from calelektra.setups.paths import ExpandedSetup, expand, vna_role_of

NODE_W, NODE_H = 160.0, 76.0
PORT_W, PORT_H = 140.0, 64.0
PLANE_W, PLANE_H = 140.0, 64.0
DX, DY = 192.0, 118.0
MIN_FIT_SCALE = 0.8
PATH_ROLE = "camino:"  # role emitted when a port or a plane is clicked: the path of the port


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


@dataclass
class DiagramModel:
    nodes: dict[str, NodeSpec] = field(default_factory=dict)
    edges: list[tuple[str, str, str]] = field(default_factory=list)  # (a, b, kind)
    mounts: list[tuple[str, str]] = field(default_factory=list)  # (positioner, mounted)
    positions: dict[str, tuple[float, float]] = field(default_factory=dict)  # fixed layout (setups with paths)


def build_diagram_model(
    template: SetupTemplate,
    setup: ConfiguredSetup,
    library: Library,
    analysis: SetupAnalysis | None,
    state_name: str | None,
) -> DiagramModel:
    """Nodes, edges and (for setups with paths) positions of the diagram of a setup."""
    expanded = analysis.expanded if analysis is not None and analysis.expanded is not None else None
    if expanded is None and template.paths:
        expanded = expand(template, setup, library)
    if expanded is not None and expanded.paths:
        return _path_diagram_model(expanded, library, analysis, state_name)
    return _role_diagram_model(template, setup, library, analysis, state_name)


def _describe(library: Library, equipment_id: str) -> str:
    equipment = library.equipment.get(equipment_id)
    model = library.model_of(equipment) if equipment else None
    return f"{equipment_id} — {model.designation}" if model else equipment_id


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
        model.nodes[key] = NodeSpec(key, key, role.display(key), lines, kind_of(key), severity_of.get(key), assigned, not role.required, role.is_dut)

    vna_role = vna_role_of(template)
    path_roles = {element.role for path in expanded.paths for element in path.elements}
    if vna_role is not None:
        add_role(vna_role)
        model.nodes[vna_role].title = template.roles[vna_role].display(vna_role)
    chains: set[tuple[str, str]] = set()
    ends: dict[str, str] = {}
    widest = max((len(path.elements) for path in expanded.paths), default=0)
    first_x = DX - 10 + PORT_W + 50  # first equipment of the paths
    plane_x = first_x + widest * DX  # the planes of all the paths in one column
    rows = {}
    for row, path in enumerate(expanded.paths):
        y = row * DY
        rows[path.key] = y
        problems = [issue.severity for issue in path.issues]
        port_key = f"puerto:{path.key}"
        connector = path.vna_connector.label() if path.vna_connector else "conector sin definir"
        model.nodes[port_key] = NodeSpec(
            port_key, PATH_ROLE + path.key, f"P{path.vna_port}", [connector, path.label], "puerto",
            max(problems) if problems else None, True, False, False, PORT_W, PORT_H,
        )
        model.positions[port_key] = (DX - 10, y + (NODE_H - PORT_H) / 2)
        if vna_role is not None:
            model.edges.append((vna_role, port_key, "coaxial"))
        previous = port_key
        for index, element in enumerate(path.elements):
            add_role(element.role)
            node = model.nodes[element.role]
            node.lines = [_describe(library, element.equipment_id)]
            if element.vna_side and element.dut_side:
                end = f" · {element.dut_connector.label()}" if element.dut_connector else ""
                node.lines.append(f"{element.vna_side} → {element.dut_side}{end}")
            model.positions[element.role] = (first_x + index * DX, y)
            model.edges.append((previous, element.role, "coaxial"))
            previous = element.role
        plane_key = f"plano:{path.plane}"
        end_connector = path.end_connector.label() if path.end_connector else "conector sin definir"
        dut_connector = path.dut_connector.label() if path.dut_connector else "—"
        model.nodes[plane_key] = NodeSpec(
            plane_key, PATH_ROLE + path.key, f"Plano {path.plane}", [end_connector, f"DUT: {dut_connector}"], "plano",
            None, True, False, False, PLANE_W, PLANE_H,
        )
        model.positions[plane_key] = (plane_x, y + (NODE_H - PLANE_H) / 2)
        model.edges.append((previous, plane_key, "coaxial"))
        ends[path.end_endpoint] = plane_key
        chains.update(path.chain())
    if vna_role is not None:
        model.positions[vna_role] = (-60.0, (min(rows.values()) + max(rows.values())) / 2 if rows else 0.0)

    for key in template.roles:
        if key not in model.nodes and key not in path_roles:
            add_role(key)
    state = template.connection_states.get(state_name) if state_name else None

    def node_for(endpoint: str) -> str | None:
        if endpoint in ends:
            return ends[endpoint]
        role = endpoint.partition(".")[0]
        return role if role in model.nodes else None

    attached: dict[str, list[str]] = {}  # node → plane nodes or nodes it is connected to in the state
    for connection in state.connections if state else []:
        if (connection.a, connection.b) in chains:
            continue
        a, b = node_for(connection.a), node_for(connection.b)
        if a is None or b is None or a == b:
            continue
        model.edges.append((a, b, connection.kind))
        attached.setdefault(a, []).append(b)
        attached.setdefault(b, []).append(a)

    # after the planes: what is connected to them (DUT, antennas…), then what is connected to those (loads)
    after_x = model.positions[next(iter(ends.values()))][0] + PLANE_W + 70 if ends else 2 * DX
    placed = set(model.positions)
    plane_rows = {plane: model.positions[plane][1] - (NODE_H - PLANE_H) / 2 for plane in ends.values()}
    first_ring = [key for key in model.nodes if key not in placed and any(other in plane_rows for other in attached.get(key, []))]
    for key in first_ring:
        ys = [plane_rows[other] for other in attached[key] if other in plane_rows]
        model.positions[key] = (after_x, sum(ys) / len(ys))
    placed.update(first_ring)
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
    bottom = max((y for _x, y in model.positions.values()), default=0.0) + DY + 10
    for index, key in enumerate(k for k in model.nodes if k not in placed):
        model.positions[key] = (-60.0 + index * DX, bottom)
    return model


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
            )
    for connection in connections:
        (role_a, port_a), (role_b, port_b) = Connection.endpoint(connection.a), Connection.endpoint(connection.b)
        a, b = node_key(role_a, port_a), node_key(role_b, port_b)
        if a in model.nodes and b in model.nodes:
            model.edges.append((a, b, connection.kind))
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
    for a, b, _ in model.edges:
        adjacency.setdefault(a, []).append(b)
        adjacency.setdefault(b, []).append(a)

    grid: dict[str, tuple[int, int]] = {}
    occupied: set[tuple[int, int]] = set()

    def place(key: str, col: int, row: int) -> None:
        while (col, row) in occupied:
            row += 1
        grid[key] = (col, row)
        occupied.add((col, row))

    start_row = 0
    for start in [a for a, _, _ in model.edges] + list(adjacency):
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

    def border_color(self) -> QColor:
        if self.spec.kind == "plano":
            return style.INDIGO
        if not self.spec.assigned:
            return style.NEUTRAL if self.spec.optional else (style.DUT if self.spec.is_dut else style.ERROR)
        if self.spec.severity is not None and self.spec.severity >= Severity.WARNING:
            return style.SEVERITY_COLORS[self.spec.severity]
        return style.OK

    def paint(self, painter: QPainter, option: QStyleOptionGraphicsItem, widget: QWidget | None = None) -> None:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        width, height = self.spec.width, self.spec.height
        rect = QRectF(0, 0, width, height)
        border = self.border_color()
        pen = QPen(border, 3 if self.isSelected() else 2)
        if not self.spec.assigned or self.spec.kind == "plano":
            pen.setStyle(Qt.PenStyle.DashLine)
        painter.setPen(pen)
        painter.setBrush(QBrush(QColor("#ffffff") if self.spec.assigned else QColor("#f6f7f9")))
        painter.drawRoundedRect(rect, 8, 8)

        header = QRectF(0, 0, width, 24)
        header_color = style.DUT if self.spec.is_dut else style.KIND_COLORS.get(self.spec.kind, style.ACCENT)
        if self.spec.kind == "puerto":
            header_color = style.KIND_COLORS["instrumento"].darker(115)
        elif self.spec.kind == "plano":
            header_color = style.INDIGO
        if not self.spec.assigned:
            header_color = header_color.lighter(150)
        painter.save()
        painter.setClipRect(header.adjusted(0, 0, 0, 0.5))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(header_color)
        painter.drawRoundedRect(rect, 8, 8)
        painter.restore()

        font = QFont()
        font.setPointSizeF(8.5)
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(QColor("#ffffff"))
        title = self.spec.title + ("  [DUT]" if self.spec.is_dut else "")
        painter.drawText(
            header.adjusted(8, 0, -8, 0),
            Qt.AlignmentFlag.AlignVCenter,
            painter.fontMetrics().elidedText(title, Qt.TextElideMode.ElideRight, int(width - 16)),
        )
        font.setBold(False)
        font.setPointSizeF(8)
        painter.setFont(font)
        painter.setPen(QColor("#2c3e50") if self.spec.assigned else QColor("#7f8c8d"))
        lines = list(self.spec.lines)
        if not self.spec.assigned and self.spec.optional:
            lines = ["(opcional) sin asignar"]
        for index, line in enumerate(lines[: int((height - 28) // 15)]):
            painter.drawText(
                QRectF(8, 28 + index * 15, width - 16, 15),
                Qt.AlignmentFlag.AlignVCenter,
                painter.fontMetrics().elidedText(line, Qt.TextElideMode.ElideRight, int(width - 16)),
            )
        if self.spec.severity is not None and self.spec.severity >= Severity.WARNING and self.spec.assigned:
            color = style.SEVERITY_COLORS[self.spec.severity] if self.spec.kind == "plano" else border
            painter.setBrush(color)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawEllipse(QPointF(width - 10, height - 10), 5, 5)

    def itemChange(self, change, value):  # noqa: N802 (Qt API)
        if change == QGraphicsItem.GraphicsItemChange.ItemPositionHasChanged:
            for edge in self.edges:
                edge.update_path()
            self._on_moved(self.spec.key, self.pos())
        return super().itemChange(change, value)


class EdgeItem(QGraphicsPathItem):
    def __init__(self, a: NodeItem, b: NodeItem, kind: str):
        super().__init__()
        self.a, self.b, self.kind = a, b, kind
        pen = QPen(QColor("#34495e"), 2)
        if kind == "radiado":
            pen = QPen(style.ACCENT, 2, Qt.PenStyle.DotLine)
        elif kind == "montaje":
            pen = QPen(QColor("#8d6e63"), 1.5, Qt.PenStyle.DashLine)
        self.setPen(pen)
        self.setZValue(-1)
        self.label = QGraphicsSimpleTextItem("campo" if kind == "radiado" else "", self)
        self.label.setBrush(style.ACCENT)
        a.edges.append(self)
        b.edges.append(self)
        self.update_path()

    def update_path(self) -> None:
        ra = self.a.sceneBoundingRect().adjusted(2, 2, -2, -2)
        rb = self.b.sceneBoundingRect().adjusted(2, 2, -2, -2)
        path = QPainterPath()
        if abs(ra.center().y() - rb.center().y()) > NODE_H and abs(ra.center().x() - rb.center().x()) < NODE_W:
            top, bottom = (ra, rb) if ra.center().y() < rb.center().y() else (rb, ra)
            start, end = QPointF(top.center().x(), top.bottom()), QPointF(bottom.center().x(), bottom.top())
        else:
            left, right = (ra, rb) if ra.center().x() <= rb.center().x() else (rb, ra)
            start, end = QPointF(left.right(), left.center().y()), QPointF(right.left(), right.center().y())
        path.moveTo(start)
        middle_x = (start.x() + end.x()) / 2
        label_y = (start.y() + end.y()) / 2 - 18
        if self.kind == "radiado" and abs(start.y() - end.y()) < 1:
            # arc above both antennas, so the link and its label stay visible
            start = QPointF(start.x() - NODE_W / 2, start.y() - NODE_H / 2)
            end = QPointF(end.x() + NODE_W / 2, end.y() - NODE_H / 2)
            path = QPainterPath(start)
            path.cubicTo(QPointF(start.x(), start.y() - 55), QPointF(end.x(), end.y() - 55), end)
            label_y = start.y() - 60
        elif self.kind == "montaje" or abs(start.y() - end.y()) < 1:
            path.lineTo(end)
        else:
            path.cubicTo(QPointF(middle_x, start.y()), QPointF(middle_x, end.y()), end)
        self.setPath(path)
        width = self.label.boundingRect().width()
        self.label.setPos(middle_x - width / 2, label_y)


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
    ) -> None:
        self._building = True
        self.scene().clear()
        self.nodes = {}
        model = build_diagram_model(template, setup, library, analysis, state_name)
        positions = compute_layout(model)
        for key, spec in model.nodes.items():
            item = NodeItem(spec, self._moved)
            saved = setup.layout.get(key)
            x, y = (saved[0], saved[1]) if saved and len(saved) == 2 else positions[key]
            item.setPos(x, y)
            self.scene().addItem(item)
            self.nodes[key] = item
            if selected_role and spec.role == selected_role:
                item.setSelected(True)
        for a, b, kind in model.edges:
            self.scene().addItem(EdgeItem(self.nodes[a], self.nodes[b], kind))
        for positioner, mounted in model.mounts:
            self.scene().addItem(EdgeItem(self.nodes[mounted], self.nodes[positioner], "montaje"))
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
