"""Editor of a configured hardware setup.

Diagram (left) + role panel (right) + coverage chart / segments / validation
(bottom). Every change re-runs the setup analysis and refreshes the views.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor
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
from calelektra.gui import style
from calelektra.gui.coverage_widget import CoverageWidget
from calelektra.gui.setup_diagram import SetupDiagramView
from calelektra.setups.analysis import SetupAnalysis, analyze_setup
from calelektra.setups.model import ConfiguredSetup, Role, Segment, SetupTemplate

UNASSIGNED = "— sin asignar —"
CARDINALITY_TEXT = {"1": "obligatorio, 1 equipo", "0..1": "opcional, 1 equipo", "0..n": "opcional, varios", "1..n": "obligatorio, varios"}


def _frequency_text(hz: float) -> str:
    return "DC" if hz == 0 else format_frequency(hz, decimal=".")


class RolePanel(QWidget):
    """Details and equipment assignment of the selected role."""

    assignment_changed = Signal()

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
        self.info.setText("<br>".join(details))

        if role.per_segment and setup.segments:
            self.assignment_form.addRow("Común (todos los segmentos)", self._selector(setup.assignments, role_key, role))
            for segment in setup.segments:
                self.assignment_form.addRow(f"Segmento «{segment.name}»", self._selector(segment.assignments, role_key, role, fallback=True))
        else:
            self.assignment_form.addRow("Equipo", self._selector(setup.assignments, role_key, role))

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

    def _selector(self, assignments: dict, role_key: str, role: Role, fallback: bool = False) -> QWidget:
        candidates = self.library.equipment_in_categories(role.categories)
        current = assignments.get(role_key)
        if role.multiple:
            widget = QListWidget()
            widget.setMaximumHeight(110)
            chosen = set(current or []) if not isinstance(current, str) else {current}
            for equipment in candidates:
                item = QListWidgetItem(self._candidate_text(equipment.id))
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
            combo.addItem(self._candidate_text(equipment.id), equipment.id)
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
        if equipment.calibration and equipment.calibration.due:
            parts.append(f"Calibración válida hasta: {equipment.calibration.due:%d/%m/%Y}")
        if equipment.connection and equipment.connection.simulated:
            parts.append("<i>Equipo en simulación (VNA dummy)</i>")
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


class SetupEditor(QWidget):
    saved = Signal(str)
    dirty_changed = Signal(bool)

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
        self.state_combo.currentIndexChanged.connect(lambda _i: self._redraw_diagram())
        self.save_button = QPushButton("Guardar")
        self.save_button.clicked.connect(self.save)
        fit_button = QPushButton("Ajustar vista")
        fit_button.clicked.connect(lambda: self.diagram.fit())
        self.badge = style.StatusBadge()
        header.addWidget(QLabel("Nombre:"))
        header.addWidget(self.name_edit, 2)
        header.addSpacing(12)
        header.addWidget(template_label, 3)
        header.addStretch(1)
        header.addWidget(self.badge)
        controls.addWidget(QLabel("Rango requerido:"))
        controls.addWidget(self.start_edit)
        controls.addWidget(QLabel("–"))
        controls.addWidget(self.stop_edit)
        controls.addSpacing(16)
        controls.addWidget(QLabel("Estado de conexión:"))
        controls.addWidget(self.state_combo)
        controls.addStretch(1)
        controls.addWidget(fit_button)
        controls.addWidget(self.save_button)
        layout.addLayout(header)
        layout.addLayout(controls)

        self.diagram = SetupDiagramView()
        self.diagram.role_clicked.connect(self._role_clicked)
        self.diagram.node_moved.connect(self._node_moved)
        self.role_panel = RolePanel(self.library)
        self.role_panel.assignment_changed.connect(self._changed)
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
        self.bottom_tabs = QTabWidget()
        self.bottom_tabs.addTab(coverage_scroll, "Cobertura en frecuencia")
        self.bottom_tabs.addTab(self.segments_panel, "Segmentos de frecuencia")
        self.bottom_tabs.addTab(self.issues, "Validación")

        vertical = QSplitter(Qt.Orientation.Vertical)
        vertical.addWidget(top)
        vertical.addWidget(self.bottom_tabs)
        vertical.setStretchFactor(0, 3)
        vertical.setStretchFactor(1, 2)
        layout.addWidget(vertical)

    # ── refresh ────────────────────────────────────────────────────────────
    def refresh(self) -> None:
        self.analysis = analyze_setup(self.setup, self.library)
        self._update_badge()
        self._redraw_diagram()
        self.coverage.set_analysis(self.analysis)
        self.segments_panel.load(self.setup)
        self.issues.load(self.analysis.issues)
        errors = sum(1 for i in self.analysis.issues if i.severity is Severity.ERROR)
        warnings = sum(1 for i in self.analysis.issues if i.severity is Severity.WARNING)
        self.bottom_tabs.setTabText(2, f"Validación ({errors} errores, {warnings} avisos)")
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
