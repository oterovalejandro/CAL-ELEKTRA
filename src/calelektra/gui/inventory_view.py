"""Equipment inventory: filterable table and detail cards with the fields of each equipment."""

from __future__ import annotations

import html
from datetime import date, timedelta
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSplitter,
    QTableWidgetItem,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from calelektra.catalog.library import Library
from calelektra.catalog.model import STATUS_NAMES, Equipment
from calelektra.core.connectors import FAMILIES, Connector
from calelektra.core.touchstone import TouchstoneError
from calelektra.gui import style
from calelektra.gui.equipment_dialogs import EquipmentDialog, confirm_delete_equipment
from calelektra.gui.icons import icon
from calelektra.gui.plots import SParameterDialog, VnaCheckDialog
from calelektra.instruments.factory import driver_description

COLUMNS = ["ID", "Categoría", "Modelo", "Nº serie", "Rango útil", "Conectores", "Calibración hasta", "Estado"]
SOURCE_NAMES = {"equipo": "equipo", "modelo": "modelo", "defecto": "por defecto", None: ""}


def short_connector(connector: Connector) -> str:
    family = FAMILIES[connector.family].split(" (")[0]
    gender = {"macho": "m", "hembra": "h"}.get(connector.gender, "")
    return f"{family} {gender}".strip()


def connectors_text(library: Library, equipment: Equipment) -> str:
    parts = []
    for port in library.ports_of(equipment):
        connector = library.port_connector(equipment, port.name)
        if connector is not None:
            parts.append(f"{port.name}: {short_connector(connector)}")
    if len(parts) > 2 and len({p.split(": ", 1)[1] for p in parts}) == 1:
        return f"{len(parts)}× {parts[0].split(': ', 1)[1]}"
    return " · ".join(parts)


class EquipmentDetail(QWidget):
    changed = Signal(str)
    edit_requested = Signal(str)

    def __init__(self, library: Library, parent: QWidget | None = None):
        super().__init__(parent)
        self.library = library
        self.equipment: Equipment | None = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 0, 4, 4)
        layout.setSpacing(10)

        self.header = style.Card()
        top = QHBoxLayout()
        self.title = QLabel("Seleccione un equipo")
        self.title.setObjectName("pageTitle")
        self.chips = QHBoxLayout()
        self.chips.setSpacing(6)
        top.addWidget(self.title)
        top.addStretch(1)
        top.addLayout(self.chips)
        self.subtitle = QLabel()
        self.subtitle.setObjectName("pageSubtitle")
        self.subtitle.setWordWrap(True)
        self.header.body.addLayout(top)
        self.header.body.addWidget(self.subtitle)
        edit = QPushButton(icon("editar", style.PRIMARY.name()), "Editar")
        edit.clicked.connect(lambda: self.equipment and self.edit_requested.emit(self.equipment.id))
        top.addWidget(edit)

        self.fields_card = style.Card("Campos de la categoría", icon_name="ajustes")
        self.fields = QTreeWidget()
        self.fields.setColumnCount(3)
        self.fields.setHeaderLabels(["Campo", "Valor", "Origen"])
        self.fields.setRootIsDecorated(True)
        self.fields.header().setStretchLastSection(False)
        self.fields.header().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.fields.header().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.fields_card.body.addWidget(self.fields)

        self.calibration_card = style.Card("Calibración del equipo", icon_name="ok")
        self.calibration_label = QLabel()
        self.calibration_label.setWordWrap(True)
        self.calibration_card.body.addWidget(self.calibration_label)

        self.s_card = style.Card("Datos S (parámetros S calibrados)", icon_name="grafica")
        self.s_label = QLabel()
        self.s_label.setWordWrap(True)
        self.s_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        s_buttons = QHBoxLayout()
        self.plot_button = QPushButton(icon("grafica", style.PRIMARY.name()), "Ver parámetros S")
        self.plot_button.clicked.connect(self._plot)
        s_buttons.addWidget(self.plot_button)
        s_buttons.addStretch(1)
        self.s_card.body.addWidget(self.s_label)
        self.s_card.body.addLayout(s_buttons)

        self.connection_card = style.Card("Conexión y driver", icon_name="vna")
        self.connection_label = QLabel()
        self.connection_label.setWordWrap(True)
        self.check_button = QPushButton(icon("ejecutar", style.PRIMARY.name()), "Comprobación funcional…")
        self.check_button.clicked.connect(self._functional_check)
        self.connection_card.body.addWidget(self.connection_label)
        self.connection_card.body.addWidget(self.check_button, 0, Qt.AlignmentFlag.AlignLeft)

        self.usage_card = style.Card("Uso en hardware setups y plantillas de ensayo", icon_name="setups")
        self.usage_label = QLabel()
        self.usage_label.setWordWrap(True)
        self.usage_card.body.addWidget(self.usage_label)

        for card in (self.header, self.fields_card, self.calibration_card, self.s_card, self.connection_card, self.usage_card):
            layout.addWidget(card)
        layout.addStretch(1)
        self.show_equipment(None)

    def _clear_chips(self) -> None:
        while self.chips.count():
            widget = self.chips.takeAt(0).widget()
            if widget:
                widget.deleteLater()

    def show_equipment(self, equipment: Equipment | None) -> None:
        self.equipment = equipment
        self._clear_chips()
        for card in (self.fields_card, self.calibration_card, self.s_card, self.connection_card, self.usage_card):
            card.setVisible(equipment is not None)
        if equipment is None:
            self.title.setText("Seleccione un equipo")
            self.subtitle.setText("La ficha muestra los campos que usan los hardware setups y los procedimientos.")
            return
        library = self.library
        model = library.model_of(equipment)
        category = library.category_of(equipment)
        self.title.setText(equipment.id)
        details = [model.display_name if model else equipment.model_id, category.name if category else "?"]
        if equipment.serial_number:
            details.append(f"Nº serie {equipment.serial_number}")
        if equipment.location:
            details.append(equipment.location)
        self.subtitle.setText(" · ".join(details) + (f"\n{equipment.description}" if equipment.description else ""))
        status_color = style.OK if equipment.status == "en_servicio" else style.ERROR
        self.chips.addWidget(style.chip(STATUS_NAMES.get(equipment.status, equipment.status), status_color))
        if equipment.simulated or (model and model.driver == "dummy"):
            self.chips.addWidget(style.chip("SIMULADO", style.SIMULATION))

        # fields grouped as in the category, with where each value comes from
        self.fields.clear()
        groups: dict[str, QTreeWidgetItem] = {}
        for key, resolved in library.fields_of(equipment).items():
            group = groups.get(resolved.spec.group)
            if group is None:
                group = QTreeWidgetItem([resolved.spec.group, "", ""])
                font = group.font(0)
                font.setBold(True)
                group.setFont(0, font)
                self.fields.addTopLevelItem(group)
                groups[resolved.spec.group] = group
            item = QTreeWidgetItem([resolved.spec.label, resolved.text, SOURCE_NAMES.get(resolved.source, "")])
            item.setToolTip(0, f"{key}\n{resolved.spec.description or ''}".strip())
            if resolved.value is None and resolved.spec.required:
                item.setForeground(1, style.ERROR)
                item.setText(1, "falta (obligatorio)")
            if resolved.source == "equipo":
                item.setForeground(2, style.ACCENT)
            group.addChild(item)
        info = library.frequency_range_of(equipment)
        usable = QTreeWidgetItem(["Rango útil (calculado)", str(info.range) if info.range else "sin definir", ""])
        usable.setToolTip(1, "\n".join(info.sources))
        font = usable.font(0)
        font.setBold(True)
        usable.setFont(0, font)
        self.fields.insertTopLevelItem(0, usable)
        for source in info.sources:
            usable.addChild(QTreeWidgetItem(["", source, ""]))
        ports = connectors_text(library, equipment)
        if ports:
            self.fields.insertTopLevelItem(1, QTreeWidgetItem(["Puertos", ports, ""]))
        self.fields.expandAll()
        self.fields.resizeColumnToContents(0)
        self.fields.setColumnWidth(0, min(max(self.fields.columnWidth(0), 200), 260))
        rows = sum(1 + self.fields.topLevelItem(i).childCount() for i in range(self.fields.topLevelItemCount()))
        self.fields.setMinimumHeight(min(40 + 21 * rows, 560))

        calibration = equipment.calibration
        today = date.today()
        if calibration and calibration.due:
            color = "#c0392b" if calibration.due < today else ("#d98a00" if calibration.due <= today + timedelta(days=30) else "#2e8b57")
            text = f"Certificado <b>{html.escape(calibration.certificate or '—')}</b>"
            if calibration.laboratory:
                text += f" · {html.escape(calibration.laboratory)}"
            text += "<br>"
            if calibration.issued:
                text += f"Calibrado el {calibration.issued:%d/%m/%Y} · "
            text += f"válido hasta <b style='color:{color}'>{calibration.due:%d/%m/%Y}</b>"
            self.calibration_label.setText(text)
        else:
            needs = category is not None and category.requires_calibration
            self.calibration_label.setText(
                "<span style='color:#d98a00'>Sin datos de calibración</span>" if needs else "La categoría no requiere calibración."
            )

        if equipment.s_parameters is not None:
            self.s_card.setVisible(True)
            data = equipment.s_parameters
            try:
                touchstone = library.touchstone_of(equipment)
                summary = f"{touchstone.n_ports} puertos · {len(touchstone.frequencies)} puntos · {touchstone.frequency_range}"
                self.plot_button.setEnabled(True)
            except TouchstoneError as exc:
                summary = f"<span style='color:#c0392b'>{html.escape(str(exc))}</span>"
                self.plot_button.setEnabled(False)
            date_text = f" · {data.measured_on:%d/%m/%Y}" if data.measured_on else ""
            self.s_label.setText(
                f"<b>{html.escape(Path(data.file).name)}</b>{date_text}<br>Referencia: {html.escape(data.reference or '—')}<br>{summary}"
            )
        elif category and category.requires_s_parameters:
            self.s_card.setVisible(True)
            self.s_label.setText("<span style='color:#c0392b'>Falta el fichero de parámetros S (obligatorio en esta categoría)</span>")
            self.plot_button.setEnabled(False)
        else:
            self.s_card.setVisible(False)

        if category and category.has_connection:
            connection = equipment.connection
            lines = [f"<b>{html.escape(driver_description(equipment, library))}</b>"]
            if connection and connection.resource:
                lines.append(f"Recurso VISA: <code>{html.escape(connection.resource)}</code>")
            self.connection_label.setText("<br>".join(lines))
            self.check_button.setVisible(category.id == "vna")
        else:
            self.connection_card.setVisible(False)

        users = library.references_to(equipment.id)
        lines = [f"<b>Setups:</b> {html.escape(', '.join(library.setups[u].name for u in users)) if users else 'ninguno'}"]
        templates = library.templates_using(equipment_id=equipment.id)
        if templates:
            lines.append(f"<b>EUT de las plantillas de ensayo:</b> {html.escape(', '.join(library.procedures[t].name for t in templates))}")
        specs = library.specifications_of(equipment)
        if specs:
            lines.append(
                "<b>Especificaciones S de su modelo (pasa / falla):</b> "
                + html.escape("; ".join(f"{spec.name} ({spec.describe()})" for spec, _ in specs))
            )
        self.usage_label.setText("<br>".join(lines))

    def _plot(self) -> None:
        if self.equipment is not None and self.equipment.s_parameters is not None:
            SParameterDialog(self.library, self.equipment, self).exec()

    def _functional_check(self) -> None:
        if self.equipment is not None:
            VnaCheckDialog(self.library, self.equipment, self).exec()


class InventoryView(QWidget):
    equipment_changed = Signal(str)
    title = "Equipos"

    def __init__(self, library: Library, parent: QWidget | None = None):
        super().__init__(parent)
        self.library = library
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 10)
        header = style.PageHeader("Equipos", "Inventario del laboratorio. Cada equipo tiene los campos de su categoría; los setups y los ensayos los usan.")
        layout.addWidget(header)
        filters = QHBoxLayout()
        self.category_filter = QComboBox()
        self.category_filter.setMinimumWidth(220)
        self.status_filter = QComboBox()
        self.status_filter.addItem("Todos los estados", None)
        for key, name in STATUS_NAMES.items():
            self.status_filter.addItem(name, key)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Buscar por ID, modelo, nº de serie…")
        self.search.setClearButtonEnabled(True)
        self.search.addAction(icon("buscar", style.NEUTRAL.name()), QLineEdit.ActionPosition.LeadingPosition)
        self.count_label = QLabel()
        self.count_label.setObjectName("pageSubtitle")
        filters.addWidget(self.category_filter)
        filters.addWidget(self.status_filter)
        filters.addWidget(self.search, 1)
        filters.addWidget(self.count_label)
        layout.addLayout(filters)

        self.table = style.make_table(COLUMNS)
        self.table.setSortingEnabled(True)
        self.table.sortByColumn(0, Qt.SortOrder.AscendingOrder)
        self.table.itemSelectionChanged.connect(self._selected)
        self.table.itemDoubleClicked.connect(lambda _item: self.edit_selected())
        self.detail = EquipmentDetail(library)
        self.detail.edit_requested.connect(lambda _id: self.edit_selected())
        detail_scroll = QScrollArea()
        detail_scroll.setWidgetResizable(True)
        detail_scroll.setWidget(self.detail)
        detail_scroll.setMinimumWidth(420)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self.table)
        splitter.addWidget(detail_scroll)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        layout.addWidget(splitter, 1)

        self.action_new = QAction(icon("nuevo", style.PRIMARY.name()), "Nuevo\nequipo", self)
        self.action_edit = QAction(icon("editar", style.PRIMARY.name()), "Editar", self)
        self.action_copy = QAction(icon("duplicar", style.PRIMARY.name()), "Duplicar", self)
        self.action_delete = QAction(icon("eliminar", style.ERROR.name()), "Eliminar", self)
        self.action_s = QAction(icon("grafica", style.PRIMARY.name()), "Datos S", self)
        self.action_check = QAction(icon("vna", style.PRIMARY.name()), "Comprobar\nVNA", self)
        self.action_new.triggered.connect(self.new_equipment)
        self.action_edit.triggered.connect(self.edit_selected)
        self.action_copy.triggered.connect(self.duplicate_selected)
        self.action_delete.triggered.connect(self.delete_selected)
        self.action_s.triggered.connect(self.detail._plot)
        self.action_check.triggered.connect(self.detail._functional_check)

        self.category_filter.currentIndexChanged.connect(lambda _i: self.refresh())
        self.status_filter.currentIndexChanged.connect(lambda _i: self.refresh())
        self.search.textChanged.connect(lambda _t: self.refresh())
        self.reload()

    def ribbon_actions(self) -> list[QAction | None]:
        return [self.action_new, self.action_edit, self.action_copy, self.action_delete, None, self.action_s, self.action_check]

    def set_library(self, library: Library) -> None:
        self.library = library
        self.detail.library = library
        self.detail.show_equipment(None)
        self.reload()

    def reload(self) -> None:
        current = self.category_filter.currentData()
        self.category_filter.blockSignals(True)
        self.category_filter.clear()
        self.category_filter.addItem("Todas las categorías", None)
        for category in sorted(self.library.categories.values(), key=lambda c: c.name):
            count = len(self.library.equipment_in_categories([category.id]))
            self.category_filter.addItem(f"{category.name} ({count})", category.id)
        self.category_filter.setCurrentIndex(max(self.category_filter.findData(current), 0))
        self.category_filter.blockSignals(False)
        self.refresh()

    def refresh(self, select_id: str | None = None) -> None:
        selected = select_id or (self.detail.equipment.id if self.detail.equipment else None)
        category_id = self.category_filter.currentData()
        status = self.status_filter.currentData()
        text = self.search.text().strip().lower()
        today = date.today()
        self.table.setSortingEnabled(False)
        self.table.setRowCount(0)
        for equipment in self.library.equipment.values():
            model = self.library.model_of(equipment)
            category = self.library.category_of(equipment)
            if category_id and (category is None or category.id != category_id):
                continue
            if status and equipment.status != status:
                continue
            haystack = " ".join(filter(None, [equipment.id, model.display_name if model else "", equipment.serial_number or "", equipment.description or ""])).lower()
            if text and text not in haystack:
                continue
            info = self.library.frequency_range_of(equipment)
            due = equipment.calibration.due if equipment.calibration else None
            values = [
                equipment.id,
                category.name if category else "?",
                model.display_name if model else equipment.model_id,
                equipment.serial_number or "",
                str(info.range) if info.range else "",
                connectors_text(self.library, equipment),
                f"{due:%Y-%m-%d}" if due else "",
                STATUS_NAMES.get(equipment.status, equipment.status),
            ]
            row = self.table.rowCount()
            self.table.insertRow(row)
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setData(Qt.ItemDataRole.UserRole, equipment.id)
                if column == 0:
                    kind = category.kind if category else "pasivo"
                    item.setIcon(icon("vna" if kind == "instrumento" else ("cable" if kind == "pasivo" else "setups"), style.KIND_COLORS.get(kind, style.ACCENT).name(), 16))
                    if equipment.simulated or (model and model.driver == "dummy"):
                        item.setToolTip("Instrumento simulado")
                if column == 6 and due:
                    if due < today:
                        item.setForeground(style.ERROR)
                        item.setToolTip("Calibración caducada")
                    elif due <= today + timedelta(days=30):
                        item.setForeground(style.WARNING)
                        item.setToolTip("Caduca en menos de 30 días")
                if column == 7 and equipment.status != "en_servicio":
                    item.setForeground(style.ERROR)
                if column == 4 and info.issues:
                    item.setForeground(style.ERROR)
                    item.setToolTip("\n".join(i.message for i in info.issues))
                self.table.setItem(row, column, item)
        self.table.setSortingEnabled(True)
        self.table.resizeColumnsToContents()
        for column in range(self.table.columnCount()):
            self.table.setColumnWidth(column, min(self.table.columnWidth(column) + 12, 260))
        self.count_label.setText(f"{self.table.rowCount()} de {len(self.library.equipment)} equipos")
        for row in range(self.table.rowCount()):
            if self.table.item(row, 0).data(Qt.ItemDataRole.UserRole) == selected:
                self.table.selectRow(row)
                break
        else:
            self.detail.show_equipment(None)
        self._update_actions()

    def _selected_id(self) -> str | None:
        rows = self.table.selectionModel().selectedRows()
        return self.table.item(rows[0].row(), 0).data(Qt.ItemDataRole.UserRole) if rows else None

    def _selected(self) -> None:
        equipment_id = self._selected_id()
        self.detail.show_equipment(self.library.equipment.get(equipment_id) if equipment_id else None)
        self._update_actions()

    def _update_actions(self) -> None:
        equipment = self.detail.equipment
        has = equipment is not None
        for action in (self.action_edit, self.action_copy, self.action_delete):
            action.setEnabled(has)
        self.action_s.setEnabled(has and equipment.s_parameters is not None)
        self.action_check.setEnabled(has and self.library.category_id_of(equipment) == "vna")

    def select(self, equipment_id: str) -> None:
        self.search.clear()
        self.category_filter.setCurrentIndex(0)
        self.status_filter.setCurrentIndex(0)
        self.refresh(select_id=equipment_id)

    # ── actions ────────────────────────────────────────────────────────────
    def new_equipment(self, model_id: str | None = None) -> None:
        dialog = EquipmentDialog(self.library, parent=self, model_id=model_id if isinstance(model_id, str) else None)
        if dialog.exec() and dialog.saved_id:
            self._after_change(dialog.saved_id)

    def edit_selected(self) -> None:
        equipment = self.detail.equipment
        if equipment is None:
            return
        dialog = EquipmentDialog(self.library, equipment, parent=self)
        if dialog.exec() and dialog.saved_id:
            self._after_change(dialog.saved_id)

    def duplicate_selected(self) -> None:
        equipment = self.detail.equipment
        if equipment is None:
            return
        dialog = EquipmentDialog(self.library, parent=self, copy_of=equipment)
        if dialog.exec() and dialog.saved_id:
            self._after_change(dialog.saved_id)

    def delete_selected(self) -> None:
        equipment = self.detail.equipment
        if equipment is not None and confirm_delete_equipment(self.library, equipment.id, self):
            self.detail.show_equipment(None)
            self.reload()
            self.equipment_changed.emit(equipment.id)

    def _after_change(self, equipment_id: str) -> None:
        self.library.reload()
        self.reload()
        self.refresh(select_id=equipment_id)
        self.equipment_changed.emit(equipment_id)
        problems = [i for i in self.library.load_issues if i.equipment_id == equipment_id or (i.source and equipment_id in i.source)]
        if problems:
            QMessageBox.warning(self, "Equipo guardado con avisos", "\n".join(i.message for i in problems))

