"""Hardware setups: configured setups with their status and the generic templates."""

from __future__ import annotations

import html
import re

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from calelektra.catalog.library import Library, LibraryError
from calelektra.core.frequency import FrequencyRange
from calelektra.core.issues import Severity
from calelektra.core.units import UnitError, format_frequency, parse_frequency
from calelektra.gui import style
from calelektra.gui.frequency_input import install_frequency_completion
from calelektra.gui.icons import icon
from calelektra.procedures.model import ALGORITHMS
from calelektra.setups.analysis import SetupAnalysis, analyze_setup
from calelektra.setups.model import PATH_SENSES, ConfiguredSetup, SetupTemplate


def slugify(text: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_-]+", "_", text.strip()).strip("_")
    return slug or "setup"


def status_of(analysis: SetupAnalysis) -> tuple[Severity | None, str]:
    if analysis.template is None:
        return Severity.ERROR, "plantilla inexistente"
    if not analysis.covered:
        return Severity.ERROR, "ALARMA: rango no cubierto"
    if not analysis.ok:
        return Severity.ERROR, "con errores"
    if any(i.severity is Severity.WARNING for i in analysis.issues):
        return Severity.WARNING, "válido con avisos"
    return None, "válido"


def setup_status(library: Library, setup: ConfiguredSetup) -> tuple[Severity | None, str]:
    return status_of(analyze_setup(setup, library))


def template_html(library: Library, template: SetupTemplate) -> str:
    parts = []
    if template.description:
        parts.append(html.escape(template.description.strip()))
    roles = []
    for key, role in template.roles.items():
        tags = []
        if role.is_dut:
            tags.append("DUT")
        if not role.required:
            tags.append("opcional")
        if role.per_segment:
            tags.append("por segmento")
        categories = ", ".join(library.categories[c].name if c in library.categories else c for c in role.categories)
        roles.append(f"<li><b>{html.escape(role.display(key))}</b>{' (' + ', '.join(tags) + ')' if tags else ''} — {html.escape(categories)}</li>")
    if template.paths:
        paths = []
        for key, definition in template.paths.items():
            categories = ", ".join(library.categories[c].name if c in library.categories else c for c in definition.categories)
            text = f"<b>{html.escape(definition.display(key))}</b> (puerto P{definition.port_number(key)} → plano PR{definition.port_number(key)}): {html.escape(categories)}"
            if definition.sense != "ambos":
                text += f" · {html.escape(PATH_SENSES[definition.sense].lower())}"
            if definition.final:
                text += f" · termina en {html.escape(template.roles[definition.final].display(definition.final) if definition.final in template.roles else definition.final)}"
            paths.append(f"<li>{text}</li>")
        parts.append("<b>Caminos de los puertos del VNA</b> (del VNA al plano de referencia)<ul style='margin-top:2px'>" + "".join(paths) + "</ul>")
    parts.append("<b>Otros roles</b><ul style='margin-top:2px'>" + "".join(roles) + "</ul>")
    if template.algorithms:
        parts.append("<b>Ensayos:</b> " + html.escape("; ".join(ALGORITHMS.get(a, a) for a in template.algorithms)))
    else:
        parts.append("<b>Ensayos:</b> sin algoritmo de ensayo todavía")
    methods = template.vna_calibration.get("metodos")
    if methods:
        parts.append("<b>Calibración del VNA:</b> " + html.escape(", ".join(map(str, methods))))
    if template.requirements:
        items = "".join(
            f"<li>{html.escape(r.message or f'{r.field} {r.operator} {r.value}')}</li>" for r in template.requirements
        )
        parts.append(f"<b>Requisitos sobre los campos ({len(template.requirements)})</b><ul style='margin-top:2px'>{items}</ul>")
    if template.default_range:
        parts.append(f"<b>Rango por defecto:</b> {template.default_range}")
    extends = f" · extiende «{template.extends}»" if template.extends else ""
    parts.append(f"<small>v{template.version} · {template.status or ''}{extends}</small>")
    return "<br>".join(parts)


class NewSetupDialog(QDialog):
    """Template + name (+ required range) of a new configured setup."""

    def __init__(self, library: Library, template_id: str | None = None, parent: QWidget | None = None):
        super().__init__(parent)
        self.library = library
        self.setWindowTitle("Nuevo hardware setup")
        self.resize(860, 560)
        layout = QVBoxLayout(self)
        layout.addWidget(style.PageHeader("Nuevo hardware setup", "Elija la plantilla genérica; después, en el editor, ponga los equipos de cada camino (del puerto del VNA al DUT) y asigne los demás roles."))
        body = QHBoxLayout()
        self.templates = QListWidget()
        self.templates.setMaximumWidth(340)
        for template in sorted(library.templates.values(), key=lambda t: t.name):
            item = QListWidgetItem(icon("setups", style.PRIMARY.name()), template.name)
            item.setData(Qt.ItemDataRole.UserRole, template.id)
            self.templates.addItem(item)
        self.info = QLabel()
        self.info.setWordWrap(True)
        self.info.setTextFormat(Qt.TextFormat.RichText)
        self.info.setAlignment(Qt.AlignmentFlag.AlignTop)
        card = style.Card("Plantilla")
        card.body.addWidget(self.info)
        card.body.addStretch(1)
        body.addWidget(self.templates)
        body.addWidget(card, 1)
        layout.addLayout(body, 1)
        form = QFormLayout()
        self.name = QLineEdit()
        self.start = QLineEdit()
        self.stop = QLineEdit()
        for edit in (self.start, self.stop):
            install_frequency_completion(edit)
        range_row = QHBoxLayout()
        range_row.addWidget(self.start)
        range_row.addWidget(QLabel("–"))
        range_row.addWidget(self.stop)
        form.addRow("Nombre del setup", self.name)
        form.addRow("Rango requerido", range_row)
        layout.addLayout(form)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("Cancelar")
        cancel.clicked.connect(self.reject)
        create = QPushButton("Crear y configurar")
        create.setObjectName("primary")
        create.clicked.connect(self._create)
        buttons.addWidget(cancel)
        buttons.addWidget(create)
        layout.addLayout(buttons)
        self.setup: ConfiguredSetup | None = None
        self.templates.currentItemChanged.connect(self._selected)
        if self.templates.count():
            row = 0
            for i in range(self.templates.count()):
                if self.templates.item(i).data(Qt.ItemDataRole.UserRole) == template_id:
                    row = i
            self.templates.setCurrentRow(row)

    def _selected(self, item: QListWidgetItem | None) -> None:
        if item is None:
            return
        template = self.library.resolved_template(item.data(Qt.ItemDataRole.UserRole))
        if template is None:
            return
        self.info.setText(template_html(self.library, template))
        if not self.name.isModified():
            self.name.setText(f"{template.name} — nuevo")
        default = template.default_range or FrequencyRange(1e6, 1e9)
        self.start.setText("DC" if default.start == 0 else format_frequency(default.start, decimal="."))
        self.stop.setText(format_frequency(default.stop, decimal="."))

    def _create(self) -> None:
        item = self.templates.currentItem()
        name = self.name.text().strip()
        if item is None or not name:
            QMessageBox.warning(self, self.windowTitle(), "Elija una plantilla e indique el nombre del setup.")
            return
        setup_id = slugify(name)
        if setup_id in self.library.setups:
            QMessageBox.warning(self, self.windowTitle(), f"Ya existe un setup con identificador «{setup_id}».")
            return
        try:
            required = FrequencyRange(parse_frequency(self.start.text()), parse_frequency(self.stop.text()))
        except (UnitError, ValueError) as exc:
            QMessageBox.warning(self, self.windowTitle(), f"Rango requerido no válido: {exc}")
            return
        template = self.library.templates.get(item.data(Qt.ItemDataRole.UserRole))
        paths = {key: [] for key in template.paths} if template else {}
        self.setup = ConfiguredSetup(id=setup_id, name=name, template=item.data(Qt.ItemDataRole.UserRole), required_range=required, paths=paths)
        self.accept()


class SetupsView(QWidget):
    open_setup = Signal(str)
    new_setup = Signal(object)  # ConfiguredSetup not yet saved
    new_template = Signal(str)  # setup id: new test template that calls it
    setups_changed = Signal()
    title = "Hardware setups"

    def __init__(self, library: Library, parent: QWidget | None = None):
        super().__init__(parent)
        self.library = library
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 10)
        layout.addWidget(
            style.PageHeader(
                "Hardware setups",
                "Setups configurados (equipos asignados a los roles de una plantilla) y plantillas genéricas. "
                "El estado incluye rango, requisitos sobre los campos, conectores y calibración.",
            )
        )
        splitter = QSplitter(Qt.Orientation.Horizontal)
        left = style.Card("Setups configurados", icon_name="setups")
        self.table = style.make_table(["Setup", "Plantilla", "Rango requerido", "Frecuencias", "Calibración", "Estado"])
        self.table.itemDoubleClicked.connect(lambda _item: self._open_current())
        self.table.itemSelectionChanged.connect(self._update_actions)
        left.body.addWidget(self.table)
        right = style.Card("Plantillas genéricas", icon_name="procedimientos")
        self.templates = QListWidget()
        self.templates.setMaximumHeight(210)
        self.templates.currentItemChanged.connect(self._template_selected)
        self.template_info = QLabel()
        self.template_info.setWordWrap(True)
        self.template_info.setTextFormat(Qt.TextFormat.RichText)
        self.template_info.setAlignment(Qt.AlignmentFlag.AlignTop)
        from_template = QPushButton(icon("nuevo", style.PRIMARY.name()), "Nuevo setup con esta plantilla")
        from_template.clicked.connect(lambda: self.create(self._current_template()))
        right.body.addWidget(self.templates)
        right.body.addWidget(self.template_info, 1)
        right.body.addWidget(from_template, 0, Qt.AlignmentFlag.AlignLeft)
        splitter.addWidget(left)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        layout.addWidget(splitter, 1)

        self.action_new = QAction(icon("nuevo", style.PRIMARY.name()), "Nuevo\nsetup", self)
        self.action_open = QAction(icon("abrir", style.PRIMARY.name()), "Abrir", self)
        self.action_copy = QAction(icon("duplicar", style.PRIMARY.name()), "Duplicar", self)
        self.action_delete = QAction(icon("eliminar", style.ERROR.name()), "Eliminar", self)
        self.action_test = QAction(icon("procedimientos", style.OK.name()), "Nueva plantilla\nde ensayo", self)
        self.action_new.triggered.connect(lambda: self.create(None))
        self.action_open.triggered.connect(self._open_current)
        self.action_copy.triggered.connect(self._duplicate)
        self.action_delete.triggered.connect(self._delete)
        self.action_test.triggered.connect(lambda: self._current_setup() and self.new_template.emit(self._current_setup()))
        self.reload()

    def ribbon_actions(self) -> list[QAction | None]:
        return [self.action_new, self.action_open, self.action_copy, self.action_delete, None, self.action_test]

    def set_library(self, library: Library) -> None:
        self.library = library
        self.reload()

    def reload(self) -> None:
        selected = self._current_setup()
        self.table.setRowCount(0)
        for setup in sorted(self.library.setups.values(), key=lambda s: s.name):
            analysis = analyze_setup(setup, self.library)
            severity, text = status_of(analysis)
            template = self.library.templates.get(setup.template)
            method = analysis.calibration.method if analysis.calibration and analysis.calibration.method else "—"
            row = self.table.rowCount()
            self.table.insertRow(row)
            values = [setup.name, template.name if template else setup.template, str(setup.required_range), setup.frequencies.summary(), method, text]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setData(Qt.ItemDataRole.UserRole, setup.id)
                if column == 0:
                    item.setIcon(icon("setups", style.PRIMARY.name(), 16))
                    item.setToolTip(f"{setup.id}\n{setup.source_path}")
                if column == 5:
                    item.setIcon(style.severity_icon(self, severity))
                    item.setForeground(style.SEVERITY_COLORS.get(severity, style.OK) if severity else style.OK)
                self.table.setItem(row, column, item)
        self.table.resizeColumnsToContents()
        for column, limit in ((0, 330), (1, 270), (2, 170), (3, 230), (4, 90)):
            self.table.setColumnWidth(column, min(self.table.columnWidth(column) + 12, limit))
        self.table.horizontalHeader().setStretchLastSection(True)
        for row in range(self.table.rowCount()):
            if self.table.item(row, 0).data(Qt.ItemDataRole.UserRole) == selected:
                self.table.selectRow(row)
        current_template = self._current_template()
        self.templates.clear()
        for template in sorted(self.library.templates.values(), key=lambda t: t.name):
            item = QListWidgetItem(icon("setups", style.ACCENT.name(), 16), template.name)
            item.setData(Qt.ItemDataRole.UserRole, template.id)
            self.templates.addItem(item)
            if template.id == current_template:
                self.templates.setCurrentItem(item)
        if self.templates.currentItem() is None and self.templates.count():
            self.templates.setCurrentRow(0)
        self._update_actions()

    def _current_setup(self) -> str | None:
        rows = self.table.selectionModel().selectedRows()
        return self.table.item(rows[0].row(), 0).data(Qt.ItemDataRole.UserRole) if rows else None

    def _current_template(self) -> str | None:
        item = self.templates.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def _update_actions(self) -> None:
        has = self._current_setup() is not None
        for action in (self.action_open, self.action_copy, self.action_delete, self.action_test):
            action.setEnabled(has)

    def _template_selected(self, item: QListWidgetItem | None) -> None:
        if item is None:
            self.template_info.clear()
            return
        template = self.library.resolved_template(item.data(Qt.ItemDataRole.UserRole))
        if template is not None:
            self.template_info.setText(template_html(self.library, template))

    def _open_current(self) -> None:
        setup_id = self._current_setup()
        if setup_id:
            self.open_setup.emit(setup_id)

    def create(self, template_id: str | None) -> None:
        dialog = NewSetupDialog(self.library, template_id, self)
        if dialog.exec() and dialog.setup is not None:
            self.new_setup.emit(dialog.setup)

    def _duplicate(self) -> None:
        setup_id = self._current_setup()
        if setup_id is None:
            return
        original = self.library.setups[setup_id]
        name = f"{original.name} (copia)"
        new_id, counter = slugify(name), 2
        while new_id in self.library.setups:
            new_id, counter = slugify(f"{name} {counter}"), counter + 1
        copy = original.model_copy(deep=True, update={"id": new_id, "name": name})
        copy.set_source_path(None)
        self.new_setup.emit(copy)

    def _delete(self) -> None:
        setup_id = self._current_setup()
        if setup_id is None:
            return
        name = self.library.setups[setup_id].name
        templates = self.library.templates_using(setup_id=setup_id)
        if templates:
            QMessageBox.warning(
                self, "Eliminar setup", f"«{name}» lo usan las plantillas de ensayo: {', '.join(templates)}.\nCámbielas antes de eliminarlo."
            )
            return
        answer = QMessageBox.question(self, "Eliminar setup", f"¿Eliminar el setup «{name}»? Los ensayos ya realizados se conservan.")
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            self.library.delete_setup(setup_id)
        except (LibraryError, OSError) as exc:
            QMessageBox.warning(self, "Eliminar setup", str(exc))
            return
        self.reload()
        self.setups_changed.emit()
