"""List of hardware setup templates and configured setups."""

from __future__ import annotations

import html
import re

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from calelektra.catalog.library import Library
from calelektra.core.frequency import FrequencyRange
from calelektra.core.issues import Severity
from calelektra.gui import style
from calelektra.setups.analysis import analyze_setup
from calelektra.setups.model import ConfiguredSetup


def slugify(text: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_-]+", "_", text.strip()).strip("_")
    return slug or "setup"


def setup_status(library: Library, setup: ConfiguredSetup) -> tuple[Severity | None, str]:
    analysis = analyze_setup(setup, library)
    if not analysis.covered:
        return Severity.ERROR, "ALARMA: rango no cubierto"
    if not analysis.ok:
        return Severity.ERROR, "con errores"
    if any(i.severity is Severity.WARNING for i in analysis.issues):
        return Severity.WARNING, "válido con avisos"
    return None, "válido"


class SetupsView(QWidget):
    open_setup = Signal(str)
    new_setup = Signal(object)  # ConfiguredSetup not yet saved

    def __init__(self, library: Library, parent: QWidget | None = None):
        super().__init__(parent)
        self.library = library
        layout = QVBoxLayout(self)
        layout.addWidget(style.title_label("Hardware setups"))
        columns = QHBoxLayout()

        templates_box = QGroupBox("Plantillas genéricas")
        templates_layout = QVBoxLayout(templates_box)
        self.templates = QListWidget()
        self.templates.currentItemChanged.connect(self._template_selected)
        self.template_info = QLabel()
        self.template_info.setWordWrap(True)
        self.template_info.setTextFormat(Qt.TextFormat.RichText)
        new_button = QPushButton("Nuevo setup a partir de la plantilla…")
        new_button.clicked.connect(self._create)
        templates_layout.addWidget(self.templates)
        templates_layout.addWidget(self.template_info)
        templates_layout.addWidget(new_button)

        setups_box = QGroupBox("Setups configurados")
        setups_layout = QVBoxLayout(setups_box)
        self.setups = QListWidget()
        self.setups.itemDoubleClicked.connect(lambda item: self.open_setup.emit(item.data(Qt.ItemDataRole.UserRole)))
        open_button = QPushButton("Abrir")
        open_button.clicked.connect(self._open_current)
        duplicate_button = QPushButton("Duplicar…")
        duplicate_button.clicked.connect(self._duplicate)
        buttons = QHBoxLayout()
        buttons.addWidget(open_button)
        buttons.addWidget(duplicate_button)
        buttons.addStretch(1)
        setups_layout.addWidget(self.setups)
        setups_layout.addLayout(buttons)

        columns.addWidget(templates_box, 2)
        columns.addWidget(setups_box, 3)
        layout.addLayout(columns)
        self.reload()

    def reload(self) -> None:
        self.templates.clear()
        for template in sorted(self.library.templates.values(), key=lambda t: t.name):
            suffix = f"  (extiende {template.extends})" if template.extends else ""
            item = QListWidgetItem(f"{template.name}{suffix}")
            item.setData(Qt.ItemDataRole.UserRole, template.id)
            self.templates.addItem(item)
        self.setups.clear()
        for setup in sorted(self.library.setups.values(), key=lambda s: s.name):
            severity, text = setup_status(self.library, setup)
            item = QListWidgetItem(style.severity_icon(self, severity), f"{setup.name}   —   {setup.required_range}   [{text}]")
            item.setData(Qt.ItemDataRole.UserRole, setup.id)
            item.setToolTip(f"Plantilla: {setup.template}\nFichero: {setup.source_path}")
            self.setups.addItem(item)
        if self.templates.count():
            self.templates.setCurrentRow(0)

    def _template_selected(self, item: QListWidgetItem | None) -> None:
        if item is None:
            self.template_info.clear()
            return
        template = self.library.resolved_template(item.data(Qt.ItemDataRole.UserRole))
        if template is None:
            return
        roles = ", ".join(role.display(key) for key, role in template.roles.items())
        default = f"<br><b>Rango por defecto:</b> {template.default_range}" if template.default_range else ""
        description = f"{html.escape(template.description)}<br>" if template.description else ""
        self.template_info.setText(f"{description}<b>Roles:</b> {roles}{default}<br><small>v{template.version} · {template.status or ''}</small>")

    def _open_current(self) -> None:
        item = self.setups.currentItem()
        if item is not None:
            self.open_setup.emit(item.data(Qt.ItemDataRole.UserRole))

    def _ask_identity(self, title: str, suggestion: str) -> tuple[str, str] | None:
        name, ok = QInputDialog.getText(self, title, "Nombre del setup:", text=suggestion)
        if not ok or not name.strip():
            return None
        setup_id = slugify(name)
        if setup_id in self.library.setups:
            QMessageBox.warning(self, title, f"Ya existe un setup con identificador «{setup_id}».")
            return None
        return setup_id, name.strip()

    def _create(self) -> None:
        item = self.templates.currentItem()
        if item is None:
            return
        template = self.library.resolved_template(item.data(Qt.ItemDataRole.UserRole))
        if template is None:
            return
        identity = self._ask_identity("Nuevo setup", f"{template.name} — nuevo")
        if identity is None:
            return
        setup = ConfiguredSetup(
            id=identity[0],
            name=identity[1],
            template=template.id,
            required_range=template.default_range or FrequencyRange(1e6, 1e9),
        )
        self.new_setup.emit(setup)

    def _duplicate(self) -> None:
        item = self.setups.currentItem()
        if item is None:
            return
        original = self.library.setups[item.data(Qt.ItemDataRole.UserRole)]
        identity = self._ask_identity("Duplicar setup", f"{original.name} (copia)")
        if identity is None:
            return
        copy = original.model_copy(deep=True, update={"id": identity[0], "name": identity[1]})
        copy.set_source_path(None)
        self.new_setup.emit(copy)
