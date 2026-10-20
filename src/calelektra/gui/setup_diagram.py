"""Interactive diagram of a hardware setup (ELEKTRA-like, generalised).

Each role is a node (multi-port instruments get one node per used port, e.g.
«VNA · P1» and «VNA · P2»), connections are drawn for the selected connection
state, positioners appear under the antenna they hold. Nodes can be dragged;
their positions are stored in the configured setup.
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

NODE_W, NODE_H = 160.0, 76.0
DX, DY = 192.0, 118.0
MIN_FIT_SCALE = 0.8


@dataclass
class NodeSpec:
    key: str
    role: str
    title: str
    lines: list[str]
    kind: str
    severity: Severity | None
    assigned: bool
    optional: bool
    is_dut: bool


@dataclass
class DiagramModel:
    nodes: dict[str, NodeSpec] = field(default_factory=dict)
    edges: list[tuple[str, str, str]] = field(default_factory=list)  # (a, b, kind)
    mounts: list[tuple[str, str]] = field(default_factory=list)  # (positioner, mounted)


def build_diagram_model(
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
    return ([describe(i) for i in ids] or ["se elige en cada ensayo" if is_dut else "sin asignar"]), bool(ids)


def compute_layout(model: DiagramModel) -> dict[str, tuple[float, float]]:
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
        return QRectF(-2, -2, NODE_W + 4, NODE_H + 4)

    def border_color(self) -> QColor:
        if not self.spec.assigned:
            return style.NEUTRAL if self.spec.optional else (style.DUT if self.spec.is_dut else style.ERROR)
        if self.spec.severity is not None and self.spec.severity >= Severity.WARNING:
            return style.SEVERITY_COLORS[self.spec.severity]
        return style.OK

    def paint(self, painter: QPainter, option: QStyleOptionGraphicsItem, widget: QWidget | None = None) -> None:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(0, 0, NODE_W, NODE_H)
        border = self.border_color()
        pen = QPen(border, 3 if self.isSelected() else 2)
        if not self.spec.assigned:
            pen.setStyle(Qt.PenStyle.DashLine)
        painter.setPen(pen)
        painter.setBrush(QBrush(QColor("#ffffff") if self.spec.assigned else QColor("#f6f7f9")))
        painter.drawRoundedRect(rect, 8, 8)

        header = QRectF(0, 0, NODE_W, 24)
        header_color = style.DUT if self.spec.is_dut else style.KIND_COLORS.get(self.spec.kind, style.ACCENT)
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
            painter.fontMetrics().elidedText(title, Qt.TextElideMode.ElideRight, int(NODE_W - 16)),
        )
        font.setBold(False)
        font.setPointSizeF(8)
        painter.setFont(font)
        painter.setPen(QColor("#2c3e50") if self.spec.assigned else QColor("#7f8c8d"))
        lines = list(self.spec.lines)
        if not self.spec.assigned and self.spec.optional:
            lines = ["(opcional) sin asignar"]
        for index, line in enumerate(lines[:3]):
            painter.drawText(
                QRectF(8, 28 + index * 15, NODE_W - 16, 15),
                Qt.AlignmentFlag.AlignVCenter,
                painter.fontMetrics().elidedText(line, Qt.TextElideMode.ElideRight, int(NODE_W - 16)),
            )
        if self.spec.severity is not None and self.spec.severity >= Severity.WARNING and self.spec.assigned:
            painter.setBrush(border)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawEllipse(QPointF(NODE_W - 10, NODE_H - 10), 5, 5)

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
        if self.kind == "montaje":
            start, end = QPointF(rb.center().x(), rb.bottom()), QPointF(ra.center().x(), ra.top())
        elif abs(ra.center().y() - rb.center().y()) > NODE_H and abs(ra.center().x() - rb.center().x()) < NODE_W:
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
