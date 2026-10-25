"""Editor of a configured hardware setup.

Diagram (left) + role panel (right) + coverage chart / compatibility checks /
VNA calibration / segments / validation (bottom). Clicking a port, an
equipment of a path or a reference plane opens the path of that VNA port: its
equipment in order from the VNA, how each one is connected, the connector of
the plane and the DUT connector it admits. Every change re-runs the setup
analysis and refreshes the views.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QAction, QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from calelektra.catalog.library import Library
from calelektra.core.frequency import FrequencyRange
from calelektra.core.issues import Issue, Severity
from calelektra.core.units import UnitError, format_frequency, parse_frequency
from calelektra.core.connectors import GENDERS
from calelektra.gui import style
from calelektra.gui.coverage_widget import CoverageWidget
from calelektra.gui.icons import icon
from calelektra.gui.setup_diagram import PATH_ROLE, SetupDiagramView
from calelektra.gui.template_widgets import FrequencyEditor
from calelektra.setups.analysis import Check, SetupAnalysis, analyze_setup
from calelektra.setups.connections import CalibrationDesign, method_name
from calelektra.setups.model import PATH_SENSES, ConfiguredSetup, PathItem, Role, Segment, SetupTemplate

UNASSIGNED = "— sin asignar —"
CARDINALITY_TEXT = {"1": "obligatorio, 1 equipo", "0..1": "opcional, 1 equipo", "0..n": "opcional, varios", "1..n": "obligatorio, varios"}


def _frequency_text(hz: float) -> str:
    return "DC" if hz == 0 else format_frequency(hz, decimal=".")


class RolePanel(QWidget):
    """Details and equipment assignment of the selected role, or the equipment of the selected path."""

    assignment_changed = Signal()
    focus_changed = Signal(str)  # role (or ``camino:P1``) to keep selected after an edit

    def __init__(self, library: Library, parent: QWidget | None = None):
        super().__init__(parent)
        self.library = library
        layout = QVBoxLayout(self)
        self.title = style.title_label("Seleccione un rol en el diagrama")
        self.info = QLabel()
        self.info.setWordWrap(True)
        self.info.setTextFormat(Qt.TextFormat.RichText)
        self.assignment_box = QGroupBox("Asignación de equipos")
        self.assignment_form = QFormLayout(self.assignment_box)
        self.assignment_form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows)
        self.equipment_box = QGroupBox("Equipo asignado")
        self.equipment_info = QLabel()
        self.equipment_info.setWordWrap(True)
        self.equipment_info.setTextFormat(Qt.TextFormat.RichText)
        self.equipment_info.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        QVBoxLayout(self.equipment_box).addWidget(self.equipment_info)
        self.issues_box = QGroupBox("Validación del rol")
        self.issues_list = QListWidget()
        self.issues_list.setWordWrap(True)
        QVBoxLayout(self.issues_box).addWidget(self.issues_list)
        for widget in (self.title, self.info, self.assignment_box, self.equipment_box, self.issues_box):
            layout.addWidget(widget)
        layout.addStretch(1)

    def show_role(self, template: SetupTemplate, setup: ConfiguredSetup, role_key: str | None, analysis: SetupAnalysis) -> None:
        while self.assignment_form.rowCount():
            self.assignment_form.removeRow(0)
        self.issues_list.clear()
        self.assignment_box.setTitle("Asignación de equipos")
        path_key = role_key[len(PATH_ROLE) :] if role_key and role_key.startswith(PATH_ROLE) else None
        if role_key and path_key is None and analysis.expanded is not None:
            owner = analysis.expanded.path_of_role(role_key)
            path_key = owner.key if owner else None
        if path_key is not None and path_key in template.paths:
            self._show_path(template, setup, path_key, analysis, role_key)
            return
        if role_key is None or role_key not in template.roles:
            self.title.setText("Seleccione un rol en el diagrama")
            self.info.clear()
            self.equipment_info.clear()
            return
        role = template.roles[role_key]
        self.title.setText(role.display(role_key))
        categories = ", ".join(
            self.library.categories[c].name if c in self.library.categories else c for c in role.categories
        )
        details = [
            f"<b>Rol:</b> {role_key}",
            f"<b>Categoría:</b> {categories}",
            f"<b>Cardinalidad:</b> {CARDINALITY_TEXT[role.cardinality]}",
        ]
        if role.is_dut:
            details.append("<b>Es el DUT</b> (no limita el rango efectivo; se avisa si el requerido lo excede)")
        if role.per_segment:
            details.append("<b>Por segmento:</b> puede cambiar en cada segmento de frecuencia")
        if role.min_ports:
            details.append(f"<b>Puertos mínimos:</b> {role.min_ports}")
        if role.allowed_types:
            details.append(f"<b>Tipos admitidos:</b> {', '.join(role.allowed_types)}")
        if role.mounted_on:
            details.append(f"<b>Montado en:</b> {template.roles[role.mounted_on].display(role.mounted_on) if role.mounted_on in template.roles else role.mounted_on}")
        for note in role.notes:
            details.append(f"• {note}")
        details.append("<i>Cada equipo ocupa un solo lugar: los ya usados en otro rol o en un camino del setup no se ofrecen.</i>")
        self.info.setText("<br>".join(details))

        used = setup.used_outside(role_key)  # equipment already in another role or in a path: not offered
        if role.per_segment and setup.segments:
            self.assignment_form.addRow("Común (todos los segmentos)", self._selector(setup.assignments, role_key, role, used=used))
            for segment in setup.segments:
                self.assignment_form.addRow(f"Segmento «{segment.name}»", self._selector(segment.assignments, role_key, role, fallback=True, used=used))
        else:
            self.assignment_form.addRow("Equipo", self._selector(setup.assignments, role_key, role, used=used))

        ids = []
        for segment in setup.effective_segments():
            for equipment_id in setup.ids_for(role_key, segment if role.per_segment else None):
                if equipment_id not in ids:
                    ids.append(equipment_id)
        self.equipment_info.setText("<hr>".join(self._equipment_html(i) for i in ids) or "<i>Ninguno</i>")

        for issue in analysis.issues:
            if issue.role == role_key:
                item = QListWidgetItem(style.severity_icon(self, issue.severity), issue.message)
                self.issues_list.addItem(item)
        if not self.issues_list.count():
            self.issues_list.addItem(QListWidgetItem(style.severity_icon(self, None), "Sin incidencias"))

    # ── paths ──────────────────────────────────────────────────────────────
    def _show_path(self, template: SetupTemplate, setup: ConfiguredSetup, path_key: str, analysis: SetupAnalysis, role_key: str | None) -> None:
        definition = template.paths[path_key]
        path = analysis.expanded.path(path_key) if analysis.expanded is not None else None
        self.title.setText(definition.display(path_key))
        categories = ", ".join(self.library.categories[c].name if c in self.library.categories else c for c in definition.categories)
        port = f"P{definition.port_number(path_key)}" + (f" ({path.vna_connector.label()})" if path and path.vna_connector else "")
        details = [f"<b>Puerto del VNA:</b> {port}", f"<b>Admite:</b> {categories}"]
        if definition.sense != "ambos":
            details.append(f"<b>Sentido de la señal:</b> {PATH_SENSES[definition.sense]}")
        if definition.final and definition.final in template.roles:
            details.append(f"<b>Termina en:</b> {template.roles[definition.final].display(definition.final)} (después del plano de referencia)")
        if path is not None:
            end, dut = path.end_connector, path.dut_connector
            details.append(f"<b>Plano de referencia {path.plane}:</b> {end.label() if end else 'conector sin definir'}")
            details.append(f"<b>Conector de DUT admitido:</b> {dut.label() if dut else '—'}")
        details.append(
            "<i>El camino lleva el plano de referencia desde el puerto del VNA hasta el puerto del DUT; los equipos se "
            "conectan en este orden, empezando por el VNA.</i>"
        )
        self.info.setText("<br>".join(details))
        self.assignment_box.setTitle("Equipos del camino (desde el VNA)")
        items = setup.paths.get(path_key, [])
        elements = path.elements if path is not None else []
        listing = QListWidget()
        listing.setMaximumHeight(150)
        for index, item in enumerate(items):
            element = elements[index] if index < len(elements) else None
            known = item.equipment in self.library.equipment
            text = f"{index + 1}. " + (self._candidate_text(item.equipment) if known else f"{item.equipment}  ⚠ no existe")
            if element is not None and element.vna_side and element.dut_side:
                text += f"  ({element.vna_side} → {element.dut_side}" + (", invertido" if element.reversed else "") + ")"
            entry = QListWidgetItem(icon("cable", style.PRIMARY.name() if known else style.ERROR.name()), text)
            listing.addItem(entry)
        if not items:
            empty = QListWidgetItem("Sin equipos: el plano de referencia queda en el puerto del VNA")
            empty.setFlags(Qt.ItemFlag.NoItemFlags)
            listing.addItem(empty)
        selected = next((e.index - 1 for e in elements if e.role == role_key), 0 if items else -1)
        listing.setCurrentRow(selected)

        candidates = QComboBox()
        candidates.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        candidates.setMinimumContentsLength(22)
        used = set(setup.equipment_places())  # an equipment takes one place: not offered again (not even in this path)
        for equipment in self.library.equipment_in_categories(list(definition.categories)):
            if equipment.id not in used:
                candidates.addItem(self._candidate_text(equipment.id), equipment.id)
        candidates.setToolTip("Los equipos ya usados en el setup (en un camino o en otro rol) no se ofrecen")
        add = QPushButton(icon("nuevo", style.PRIMARY.name()), "Añadir al final")
        add_row = QWidget()
        add_layout = QHBoxLayout(add_row)
        add_layout.setContentsMargins(0, 0, 0, 0)
        add_layout.addWidget(candidates, 1)
        add_layout.addWidget(add)
        buttons = QWidget()
        button_layout = QHBoxLayout(buttons)
        button_layout.setContentsMargins(0, 0, 0, 0)
        up = QPushButton("Subir")
        down = QPushButton("Bajar")
        flip = QPushButton("Invertir")
        flip.setToolTip("Conectar el equipo por su otro extremo en el lado del VNA")
        remove = QPushButton(icon("eliminar", style.ERROR.name()), "Quitar")
        for button in (up, down, flip, remove):
            button_layout.addWidget(button)
        button_layout.addStretch(1)

        def changed(row: int) -> None:
            count = len(setup.paths.get(path_key, []))
            self.focus_changed.emit(f"{path_key}-{row + 1}" if 0 <= row < count else PATH_ROLE + path_key)
            self.assignment_changed.emit()

        def added() -> None:
            if candidates.currentData():
                setup.paths.setdefault(path_key, []).append(PathItem(equipment=candidates.currentData()))
                changed(len(setup.paths[path_key]) - 1)

        def move(step: int) -> None:
            row = listing.currentRow()
            target = row + step
            if 0 <= row < len(items) and 0 <= target < len(items):
                items[row], items[target] = items[target], items[row]
                changed(target)

        def flipped() -> None:
            row = listing.currentRow()
            if 0 <= row < len(items):
                items[row] = PathItem(equipment=items[row].equipment, reversed=not items[row].reversed)
                changed(row)

        def removed() -> None:
            row = listing.currentRow()
            if 0 <= row < len(items):
                del items[row]
                changed(min(row, len(items) - 1))

        add.clicked.connect(added)
        up.clicked.connect(lambda: move(-1))
        down.clicked.connect(lambda: move(1))
        flip.clicked.connect(flipped)
        remove.clicked.connect(removed)
        for button in (up, down, flip, remove):
            button.setEnabled(bool(items))
        add.setEnabled(candidates.count() > 0)

        def show_element(row: int) -> None:
            if 0 <= row < len(items):
                self.equipment_info.setText(self._equipment_html(items[row].equipment))
            else:
                self.equipment_info.setText("<i>Ningún equipo seleccionado</i>")

        listing.currentRowChanged.connect(show_element)
        show_element(selected)
        self.assignment_form.addRow(listing)
        self.assignment_form.addRow(add_row)
        self.assignment_form.addRow(buttons)
        self.path_list, self.path_candidates = listing, candidates  # for the tests
        self.path_buttons = {"subir": up, "bajar": down, "invertir": flip, "quitar": remove, "añadir": add}

        roles = {element.role for element in elements}
        for issue in analysis.issues:
            if issue.role in roles or (issue.code.startswith("camino.") and issue.role is None and path_key in issue.message):
                self.issues_list.addItem(QListWidgetItem(style.severity_icon(self, issue.severity), issue.message))
        if not self.issues_list.count():
            self.issues_list.addItem(QListWidgetItem(style.severity_icon(self, None), "Sin incidencias"))

    def _selector(self, assignments: dict, role_key: str, role: Role, fallback: bool = False, used: set[str] | None = None) -> QWidget:
        """Equipment of the categories of the role, except those already used elsewhere in the setup."""
        current = assignments.get(role_key)
        current_ids = set(current or []) if not isinstance(current, str) else {current}
        used = used or set()
        candidates = [e for e in self.library.equipment_in_categories(role.categories) if e.id not in used or e.id in current_ids]
        if role.multiple:
            widget = QListWidget()
            widget.setMaximumHeight(110)
            chosen = set(current or []) if not isinstance(current, str) else {current}
            for equipment in candidates:
                item = QListWidgetItem(self._candidate_text(equipment.id) + ("  ⚠ ya usado en el setup" if equipment.id in used else ""))
                item.setData(Qt.ItemDataRole.UserRole, equipment.id)
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(Qt.CheckState.Checked if equipment.id in chosen else Qt.CheckState.Unchecked)
                widget.addItem(item)

            def changed(_item: QListWidgetItem) -> None:
                selected = [
                    widget.item(i).data(Qt.ItemDataRole.UserRole)
                    for i in range(widget.count())
                    if widget.item(i).checkState() == Qt.CheckState.Checked
                ]
                if selected:
                    assignments[role_key] = selected
                else:
                    assignments.pop(role_key, None)
                self.assignment_changed.emit()

            widget.itemChanged.connect(changed)
            return widget

        combo = QComboBox()
        combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        combo.setMinimumContentsLength(22)
        combo.addItem("— usar asignación común —" if fallback else UNASSIGNED, None)
        current_id = current if isinstance(current, str) else (current[0] if current else None)
        known = {e.id for e in candidates}
        for equipment in candidates:
            combo.addItem(self._candidate_text(equipment.id) + ("  ⚠ ya usado en el setup" if equipment.id in used else ""), equipment.id)
        if current_id and current_id not in known:
            combo.addItem(f"{current_id}  ⚠ no compatible o inexistente", current_id)
        index = combo.findData(current_id) if current_id else 0
        combo.setCurrentIndex(max(index, 0))

        def selected(_index: int) -> None:
            value = combo.currentData()
            if value is None:
                assignments.pop(role_key, None)
            else:
                assignments[role_key] = value
            self.assignment_changed.emit()

        combo.currentIndexChanged.connect(selected)
        return combo

    def _candidate_text(self, equipment_id: str) -> str:
        equipment = self.library.equipment[equipment_id]
        model = self.library.model_of(equipment)
        rng = self.library.frequency_range_of(equipment).range
        range_text = f"  [{rng}]" if rng else ""
        name = model.designation if model else "?"
        state = "" if equipment.status == "en_servicio" else "  (fuera de servicio)"
        return f"{equipment.id} — {name}{range_text}{state}"

    def _equipment_html(self, equipment_id: str) -> str:
        equipment = self.library.equipment.get(equipment_id)
        if equipment is None:
            return f"<b>{equipment_id}</b>: no existe en el inventario"
        model = self.library.model_of(equipment)
        info = self.library.frequency_range_of(equipment)
        parts = [f"<b>{equipment.id}</b> — {model.display_name if model else equipment.model_id}"]
        if equipment.serial_number:
            parts.append(f"Nº serie: {equipment.serial_number}")
        parts.append(f"<b>Rango útil:</b> {info.range if info.range else 'sin definir'}")
        parts.extend(f"&nbsp;&nbsp;· {source}" for source in info.sources)
        connectors = []
        for port in self.library.ports_of(equipment):
            connector = self.library.port_connector(equipment, port.name)
            if connector is not None:
                connectors.append(f"{port.name}: {connector.label()}")
        if connectors:
            parts.append("<b>Conectores:</b> " + " · ".join(connectors))
        if equipment.calibration and equipment.calibration.due:
            parts.append(f"Calibración válida hasta: {equipment.calibration.due:%d/%m/%Y}")
        if equipment.connection and equipment.connection.simulated:
            parts.append("<i>Instrumento en simulación</i>")
        return "<br>".join(parts)


class SegmentsPanel(QWidget):
    """Frequency segments of the setup (ELEKTRA: one hardware setup per frequency range)."""

    changed = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setup: ConfiguredSetup | None = None
        layout = QVBoxLayout(self)
        help_label = QLabel(
            "Sin segmentos, todo el rango requerido usa las mismas asignaciones. Con segmentos, los roles "
            "«por segmento» (p. ej. las antenas de la NSA) se asignan en cada uno desde el panel del rol."
        )
        help_label.setWordWrap(True)
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Nombre", "Inicio", "Fin"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.cellChanged.connect(self._cell_changed)
        buttons = QHBoxLayout()
        add = QPushButton("Añadir segmento")
        remove = QPushButton("Eliminar segmento")
        add.clicked.connect(self._add)
        remove.clicked.connect(self._remove)
        buttons.addWidget(add)
        buttons.addWidget(remove)
        buttons.addStretch(1)
        layout.addWidget(help_label)
        layout.addWidget(self.table)
        layout.addLayout(buttons)
        self._loading = False

    def load(self, setup: ConfiguredSetup) -> None:
        self.setup = setup
        self._loading = True
        self.table.setRowCount(0)
        for segment in setup.segments:
            row = self.table.rowCount()
            self.table.insertRow(row)
            for column, text in enumerate(
                (segment.name, _frequency_text(segment.range.start), _frequency_text(segment.range.stop))
            ):
                self.table.setItem(row, column, QTableWidgetItem(text))
        self._loading = False

    def _cell_changed(self, row: int, column: int) -> None:
        if self._loading or self.setup is None or row >= len(self.setup.segments):
            return
        segment = self.setup.segments[row]
        item = self.table.item(row, column)
        text = item.text().strip()
        try:
            if column == 0:
                segment.name = text or f"Segmento {row + 1}"
            else:
                start = parse_frequency(self.table.item(row, 1).text())
                stop = parse_frequency(self.table.item(row, 2).text())
                segment.range = FrequencyRange(start, stop)
            item.setBackground(QColor("#ffffff"))
            item.setToolTip("")
        except (UnitError, ValueError) as exc:
            item.setBackground(QColor("#f9d6d2"))
            item.setToolTip(str(exc))
            return
        self.changed.emit()

    def _add(self) -> None:
        if self.setup is None:
            return
        required = self.setup.required_range
        if self.setup.segments:
            last = max(self.setup.segments, key=lambda s: s.range.stop)
            start = last.range.stop if last.range.stop < required.stop else last.range.start
        else:
            start = required.start
        segment = Segment(name=f"Segmento {len(self.setup.segments) + 1}", range=FrequencyRange(start, required.stop))
        self.setup.segments.append(segment)
        self.load(self.setup)
        self.changed.emit()

    def _remove(self) -> None:
        if self.setup is None:
            return
        rows = sorted({index.row() for index in self.table.selectedIndexes()}, reverse=True)
        for row in rows:
            if row < len(self.setup.segments):
                del self.setup.segments[row]
        self.load(self.setup)
        self.changed.emit()


class IssuesTree(QTreeWidget):
    role_requested = Signal(str)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setColumnCount(5)
        self.setHeaderLabels(["Gravedad", "Mensaje", "Rol", "Equipo", "Segmento"])
        self.setRootIsDecorated(False)
        self.setWordWrap(True)
        self.header().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.itemClicked.connect(self._clicked)

    def load(self, issues: list[Issue]) -> None:
        self.clear()
        for issue in sorted(issues, key=lambda i: -int(i.severity)):
            item = QTreeWidgetItem([issue.severity.label, issue.message, issue.role or "", issue.equipment_id or "", issue.segment or ""])
            item.setIcon(0, style.severity_icon(self, issue.severity))
            item.setData(0, Qt.ItemDataRole.UserRole, issue.role)
            item.setToolTip(1, issue.message)
            self.addTopLevelItem(item)
        for column in (0, 2, 3, 4):
            self.resizeColumnToContents(column)

    def _clicked(self, item: QTreeWidgetItem) -> None:
        role = item.data(0, Qt.ItemDataRole.UserRole)
        if role:
            self.role_requested.emit(role)


class ChecksTable(QTreeWidget):
    """Compatibility of the setup: requirements on the fields, connectors and calibration."""

    role_requested = Signal(str)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setColumnCount(4)
        self.setHeaderLabels(["Comprobación", "Resultado", "Detalle", "Segmento"])
        self.setRootIsDecorated(True)
        self.setWordWrap(True)
        self.header().setStretchLastSection(False)
        self.header().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)

    def load(self, checks: list[Check]) -> None:
        self.clear()
        groups: dict[str, QTreeWidgetItem] = {}
        icons = {"Camino": "cable", "Requisito": "procedimientos", "Conexión": "cable", "Calibración": "vna"}
        titles = {
            "Camino": "Caminos de los puertos del VNA",
            "Requisito": "Requisitos sobre los campos",
            "Conexión": "Conectores de cada conexión",
            "Calibración": "Calibración del VNA",
        }
        for check in checks:
            group = groups.get(check.group)
            if group is None:
                group = QTreeWidgetItem([titles.get(check.group, check.group), "", "", ""])
                group.setIcon(0, icon(icons.get(check.group, "info"), style.PRIMARY.name()))
                font = group.font(0)
                font.setBold(True)
                group.setFont(0, font)
                self.addTopLevelItem(group)
                groups[check.group] = group
            item = QTreeWidgetItem([check.subject, style.status_text(check.status), check.detail, check.segment or ""])
            item.setIcon(1, style.status_icon(check.status))
            item.setForeground(1, style.status_color(check.status))
            item.setToolTip(0, check.subject)
            item.setToolTip(2, check.detail)
            group.addChild(item)
        for group in groups.values():
            worst = max((group.child(i).text(1) for i in range(group.childCount())), key=lambda t: {"Error": 3, "Sin datos": 2, "Aviso": 2}.get(t, 0), default="")
            counts = sum(1 for i in range(group.childCount()) if group.child(i).text(1) in ("Error", "Aviso", "Sin datos"))
            group.setText(1, f"{counts} incidencia(s)" if counts else "correcto")
            group.setIcon(1, style.status_icon("error" if worst == "Error" else ("aviso" if counts else "ok")))
        self.expandAll()
        self.resizeColumnToContents(0)
        self.setColumnWidth(0, min(max(self.columnWidth(0), 260), 470))
        self.resizeColumnToContents(1)
        self.setColumnHidden(3, not any(check.segment for check in checks))


class CalibrationPanel(QWidget):
    """How the VNA ports will be calibrated (planes, method, kit), designed from the fields."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        self.summary.setTextFormat(Qt.TextFormat.RichText)
        self.planes = style.make_table(["Plano", "Puerto VNA", "Dónde", "Conector", "Estándares necesarios"], stretch_column=2)
        self.planes.setMaximumHeight(110)
        self.checks = QListWidget()
        self.checks.setWordWrap(True)
        layout.addWidget(self.summary)
        layout.addWidget(self.planes)
        layout.addWidget(self.checks, 1)

    def load(self, design: CalibrationDesign | None) -> None:
        self.planes.setRowCount(0)
        self.checks.clear()
        if design is None or design.method is None:
            reason = design.reason if design else "La plantilla no define la calibración del VNA"
            self.summary.setText(f"<b>Sin calibración de puertos.</b> {reason}.")
            self.planes.setVisible(False)
            return
        self.planes.setVisible(True)
        normalization = design.method == "normalizacion"
        kit = f"«{design.kit.id}»" + (f" — en el VNA: <i>{design.kit_name}</i>" if design.kit_name else "") if design.kit else "sin kit asignado"
        if normalization:
            through = f"<b>Through:</b> «{design.through.id}»" if design.through else "<b>Through:</b> plano (unión directa de los planos)"
            self.summary.setText(f"<b>Método: {method_name(design.method)}</b> — {design.reason}.<br>{through} · no usa el kit")
        else:
            self.summary.setText(f"<b>Método: {design.method}</b> — {design.reason}.<br><b>Kit:</b> {kit}")
        for plane in design.planes:
            connector = plane.connector
            standards = "—"
            if normalization:
                standards = "Through" + (f" «{design.through.id}»" if design.through else " plano")
            elif connector is not None and connector.gender in ("macho", "hembra"):
                standards = f"Open / Short / Match {GENDERS[connector.mating_gender()]}"
            elif connector is not None:
                standards = "Open / Short / Match"
            values = [
                plane.name,
                str(plane.vna_port or "?"),
                plane.endpoint.text() if plane.endpoint else "sin equipo",
                connector.label() if connector else "—",
                standards,
            ]
            row = self.planes.rowCount()
            self.planes.insertRow(row)
            for column, value in enumerate(values):
                self.planes.setItem(row, column, QTableWidgetItem(value))
        self.planes.resizeColumnsToContents()
        for severity, message in design.checks:
            self.checks.addItem(QListWidgetItem(style.severity_icon(self, severity), message))
        if not design.checks:
            self.checks.addItem(QListWidgetItem(style.severity_icon(self, None), "Kit, conectores y métodos compatibles"))


class SetupEditor(QWidget):
    saved = Signal(str)
    dirty_changed = Signal(bool)
    new_template = Signal(str)  # setup id: new test template that calls this setup

    def __init__(self, library: Library, setup: ConfiguredSetup, is_new: bool = False, parent: QWidget | None = None):
        super().__init__(parent)
        self.library = library
        self.setup = setup.model_copy(deep=True)
        self.setup.set_source_path(setup.source_path)
        self.is_new = is_new
        self.dirty = is_new
        self.analysis: SetupAnalysis | None = None
        self.template: SetupTemplate | None = library.resolved_template(setup.template)
        self.selected_role: str | None = next(iter(self.template.roles), None) if self.template else None
        self._build()
        self.refresh()
        QTimer.singleShot(0, self.diagram.fit)

    # ── layout ─────────────────────────────────────────────────────────────
    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 8)
        header = QHBoxLayout()
        controls = QHBoxLayout()
        self.name_edit = QLineEdit(self.setup.name)
        self.name_edit.setMinimumWidth(260)
        self.name_edit.textEdited.connect(self._name_edited)
        template_name = self.template.name if self.template else f"{self.setup.template} (no existe)"
        template_label = QLabel(f"Plantilla: <b>{template_name}</b>")
        self.start_edit = QLineEdit(_frequency_text(self.setup.required_range.start))
        self.stop_edit = QLineEdit(_frequency_text(self.setup.required_range.stop))
        for edit in (self.start_edit, self.stop_edit):
            edit.setFixedWidth(120)
            edit.setCursorPosition(0)
            edit.editingFinished.connect(self._range_edited)
        self.state_combo = QComboBox()
        for state_name in (self.template.connection_states if self.template else {}):
            self.state_combo.addItem(state_name)
        if self.state_combo.findText("medida") >= 0:
            self.state_combo.setCurrentIndex(self.state_combo.findText("medida"))
        self.state_combo.currentIndexChanged.connect(lambda _i: self._redraw_diagram())
        self.action_save = QAction(icon("guardar", style.PRIMARY.name()), "Guardar", self)
        self.action_save.triggered.connect(self.save)
        self.action_test = QAction(icon("procedimientos", style.OK.name()), "Nueva plantilla\nde ensayo", self)
        self.action_test.triggered.connect(self._new_template)
        self.action_fit = QAction(icon("buscar", style.PRIMARY.name()), "Ajustar\nvista", self)
        self.action_fit.triggered.connect(lambda: self.diagram.fit())
        self.save_button = QPushButton(icon("guardar", "#ffffff"), "Guardar")
        self.save_button.setObjectName("primary")
        self.save_button.clicked.connect(self.save)
        self.test_button = QPushButton(icon("procedimientos", style.OK.name()), "Nueva plantilla de ensayo…")
        self.test_button.setToolTip("Crear una plantilla de ensayo que llame a este hardware setup")
        self.test_button.clicked.connect(self._new_template)
        fit_button = QPushButton("Ajustar vista")
        fit_button.clicked.connect(lambda: self.diagram.fit())
        self.badge = style.StatusBadge()
        header.addWidget(QLabel("Nombre:"))
        header.addWidget(self.name_edit, 2)
        header.addSpacing(12)
        header.addWidget(template_label, 3)
        header.addStretch(1)
        header.addWidget(self.badge)
        self.frequency_summary = QLabel()
        self.frequency_summary.setObjectName("pageSubtitle")
        controls.addWidget(QLabel("Rango requerido:"))
        controls.addWidget(self.start_edit)
        controls.addWidget(QLabel("–"))
        controls.addWidget(self.stop_edit)
        controls.addWidget(self.frequency_summary)
        controls.addSpacing(16)
        controls.addWidget(QLabel("Estado de conexión:"))
        controls.addWidget(self.state_combo)
        controls.addStretch(1)
        controls.addWidget(fit_button)
        controls.addWidget(self.test_button)
        controls.addWidget(self.save_button)
        layout.addLayout(header)
        layout.addLayout(controls)

        self.diagram = SetupDiagramView()
        self.diagram.role_clicked.connect(self._role_clicked)
        self.diagram.node_moved.connect(self._node_moved)
        self.role_panel = RolePanel(self.library)
        self.role_panel.assignment_changed.connect(self._changed)
        self.role_panel.focus_changed.connect(self._focus)
        role_scroll = QScrollArea()
        role_scroll.setWidgetResizable(True)
        role_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        role_scroll.setMinimumWidth(360)
        role_scroll.setWidget(self.role_panel)
        top = QSplitter(Qt.Orientation.Horizontal)
        top.addWidget(self.diagram)
        top.addWidget(role_scroll)
        top.setStretchFactor(0, 3)
        top.setStretchFactor(1, 2)

        self.coverage = CoverageWidget()
        coverage_scroll = QScrollArea()
        coverage_scroll.setWidgetResizable(True)
        coverage_scroll.setWidget(self.coverage)
        self.segments_panel = SegmentsPanel()
        self.segments_panel.changed.connect(self._changed)
        self.issues = IssuesTree()
        self.issues.role_requested.connect(self._role_clicked)
        self.checks = ChecksTable()
        self.calibration_panel = CalibrationPanel()
        self.frequency_editor = FrequencyEditor(
            resolver=lambda reference: self.library.setup_list_path(self.setup, reference),
            importer=self.library.copy_setup_list,
        )
        self.frequency_editor.load(self.setup.frequencies, self.setup.required_range)
        self.frequency_editor.changed.connect(self._frequencies_edited)
        self.frequency_editor.list_loaded.connect(self._list_loaded)
        frequency_page = QWidget()
        frequency_layout = QHBoxLayout(frequency_page)
        frequency_layout.setContentsMargins(12, 8, 12, 8)
        frequency_layout.addWidget(self.frequency_editor, 1)
        frequency_layout.addStretch(1)
        self.bottom_tabs = QTabWidget()
        self.bottom_tabs.addTab(frequency_page, icon("grafica", style.PRIMARY.name()), "Frecuencias")
        self.bottom_tabs.addTab(coverage_scroll, icon("grafica", style.PRIMARY.name()), "Cobertura en frecuencia")
        self.bottom_tabs.addTab(self.checks, icon("ok", style.PRIMARY.name()), "Compatibilidad")
        self.bottom_tabs.addTab(self.calibration_panel, icon("vna", style.PRIMARY.name()), "Calibración del VNA")
        self.bottom_tabs.addTab(self.segments_panel, icon("ajustes", style.PRIMARY.name()), "Segmentos de frecuencia")
        self.bottom_tabs.addTab(self.issues, icon("aviso", style.PRIMARY.name()), "Validación")

        vertical = QSplitter(Qt.Orientation.Vertical)
        vertical.addWidget(top)
        vertical.addWidget(self.bottom_tabs)
        vertical.setStretchFactor(0, 3)
        vertical.setStretchFactor(1, 2)
        layout.addWidget(vertical)

    # ── refresh ────────────────────────────────────────────────────────────
    def refresh(self) -> None:
        self.template = self.library.resolved_template(self.setup.template)  # the library may have been reloaded
        self.analysis = analyze_setup(self.setup, self.library)
        self._update_badge()
        self._redraw_diagram()
        self.coverage.set_analysis(self.analysis)
        self.segments_panel.load(self.setup)
        self.issues.load(self.analysis.issues)
        self.checks.load(self.analysis.checks)
        self.calibration_panel.load(self.analysis.calibration)
        errors = sum(1 for i in self.analysis.issues if i.severity is Severity.ERROR)
        warnings = sum(1 for i in self.analysis.issues if i.severity is Severity.WARNING)
        failed = sum(1 for c in self.analysis.checks if c.status not in ("ok", "info"))
        tabs = self.bottom_tabs
        tabs.setTabText(tabs.indexOf(self.checks), f"Compatibilidad ({failed} incidencias)" if failed else "Compatibilidad")
        method = self.analysis.calibration.method if self.analysis.calibration else None
        tabs.setTabText(tabs.indexOf(self.calibration_panel), f"Calibración del VNA ({'normalización' if method == 'normalizacion' else method})" if method else "Calibración del VNA")
        tabs.setTabText(tabs.indexOf(self.issues), f"Validación ({errors} errores, {warnings} avisos)")
        plan = self.analysis.frequency_plan
        frequency_problem = any(i.code.startswith("frecuencias.") and i.severity is Severity.ERROR for i in self.analysis.issues)
        tabs.setTabText(0, f"Frecuencias ({plan.points} puntos)" if plan is not None else "Frecuencias")
        tabs.setTabIcon(0, style.severity_icon(self, Severity.ERROR) if frequency_problem else icon("grafica", style.PRIMARY.name()))
        self.frequency_summary.setText(f"· {self.setup.frequencies.summary()}" + (f" → {plan.points} puntos" if plan is not None else ""))
        self.frequency_editor.set_required_range(self.setup.required_range)
        self.test_button.setEnabled(self.analysis.ok and not self.dirty)
        self.action_test.setEnabled(self.test_button.isEnabled())
        if self.template:
            self.role_panel.show_role(self.template, self.setup, self.selected_role, self.analysis)

    def _redraw_diagram(self) -> None:
        if self.template and self.analysis:
            self.diagram.show_setup(
                self.template, self.setup, self.library, self.analysis, self.state_combo.currentText(), self.selected_role
            )

    def _update_badge(self) -> None:
        analysis = self.analysis
        assert analysis is not None
        if analysis.template is None:
            self.badge.set_state("Plantilla inexistente", style.ERROR)
        elif not analysis.covered:
            self.badge.set_state("ALARMA: rango no cubierto", style.ERROR)
        elif not analysis.ok:
            self.badge.set_state("Setup con errores", style.ERROR)
        elif any(i.severity is Severity.WARNING for i in analysis.issues):
            self.badge.set_state("Válido con avisos", style.WARNING)
        else:
            self.badge.set_state("Setup válido", style.OK)

    # ── events ─────────────────────────────────────────────────────────────
    def _set_dirty(self, dirty: bool = True) -> None:
        if self.dirty != dirty:
            self.dirty = dirty
            self.dirty_changed.emit(dirty)
        if hasattr(self, "test_button") and self.analysis is not None:
            self.test_button.setEnabled(self.analysis.ok and not self.dirty)
            self.test_button.setToolTip("Guarde el setup para crear una plantilla de ensayo que lo llame" if self.dirty else "Crear una plantilla de ensayo que llame a este hardware setup")
            self.action_test.setEnabled(self.test_button.isEnabled())

    def _new_template(self) -> None:
        if not self.dirty and self.analysis is not None and self.analysis.ok:
            self.new_template.emit(self.setup.id)

    def _frequencies_edited(self) -> None:
        try:
            settings = self.frequency_editor.settings()
        except ValueError:
            return  # the editor shows what is missing
        if settings != self.setup.frequencies:
            self.setup.frequencies = settings
            self._changed()

    def _list_loaded(self, frequency_range: FrequencyRange) -> None:
        """A new CSV list: the required range becomes the range of the list."""
        self.start_edit.setText(_frequency_text(frequency_range.start))
        self.stop_edit.setText(_frequency_text(frequency_range.stop))
        self._range_edited()

    def ribbon_actions(self) -> list[QAction | None]:
        return [self.action_save, None, self.action_test, None, self.action_fit]

    def _changed(self) -> None:
        self._set_dirty()
        QTimer.singleShot(0, self.refresh)  # deferred: the sender widget is rebuilt by refresh()

    def _name_edited(self, text: str) -> None:
        self.setup.name = text
        self._set_dirty()

    def _range_edited(self) -> None:
        try:
            new = FrequencyRange(parse_frequency(self.start_edit.text()), parse_frequency(self.stop_edit.text()))
        except (UnitError, ValueError) as exc:
            for edit in (self.start_edit, self.stop_edit):
                edit.setStyleSheet("background: #f9d6d2;")
                edit.setToolTip(str(exc))
            return
        for edit in (self.start_edit, self.stop_edit):
            edit.setStyleSheet("")
            edit.setToolTip("")
        if new != self.setup.required_range:
            self.setup.required_range = new
            self._changed()

    def _focus(self, role_key: str) -> None:
        self.selected_role = role_key

    def show_path(self, path_key: str) -> None:
        """Select the path of a VNA port (its equipment in the role panel)."""
        self._role_clicked(PATH_ROLE + path_key)

    def _role_clicked(self, role_key: str) -> None:
        if role_key == self.selected_role:
            return
        self.selected_role = role_key
        if self.template and self.analysis:
            self.role_panel.show_role(self.template, self.setup, role_key, self.analysis)

    def _node_moved(self, key: str, x: float, y: float) -> None:
        self.setup.layout[key] = [round(x, 1), round(y, 1)]
        self._set_dirty()

    def save(self) -> bool:
        if self.is_new and self.setup.id in self.library.setups:
            QMessageBox.warning(self, "Guardar setup", f"Ya existe un setup con identificador «{self.setup.id}».")
            return False
        stored = self.setup.model_copy(deep=True)  # the library keeps its own copy; edits stay in the editor
        stored.set_source_path(self.setup.source_path)
        try:
            path = self.library.save_setup(stored)
        except OSError as exc:
            QMessageBox.critical(self, "Guardar setup", f"No se pudo guardar: {exc}")
            return False
        self.setup.set_source_path(path)
        self.is_new = False
        self._set_dirty(False)
        self.saved.emit(self.setup.id)
        return True
