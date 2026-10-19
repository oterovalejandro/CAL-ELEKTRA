"""Equipment inventory: list, details, S-parameter reference and functional check."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from calelektra.catalog.library import Library
from calelektra.catalog.model import Equipment, SParameterData
from calelektra.core.touchstone import TouchstoneError, read_touchstone
from calelektra.gui import style
from calelektra.gui.plots import SParameterDialog, VnaCheckDialog

COLUMNS = ["ID", "Categoría", "Modelo", "Nº serie", "Rango útil", "Calibración hasta", "Estado", "Datos S"]


class EquipmentDetail(QWidget):
    changed = Signal(str)

    def __init__(self, library: Library, parent: QWidget | None = None):
        super().__init__(parent)
        self.library = library
        self.equipment: Equipment | None = None
        layout = QVBoxLayout(self)
        self.title = style.title_label("Seleccione un equipo")
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        self.summary.setTextFormat(Qt.TextFormat.RichText)
        self.summary.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

        self.s_box = QGroupBox("Datos S (parámetros S calibrados)")
        s_form = QFormLayout(self.s_box)
        self.s_file = QLineEdit()
        self.s_file.setReadOnly(True)
        change_file = QPushButton("Cambiar fichero…")
        change_file.clicked.connect(self._change_file)
        file_row = QHBoxLayout()
        file_row.addWidget(self.s_file)
        file_row.addWidget(change_file)
        self.s_reference = QLineEdit()
        self.s_reference.setPlaceholderText("Certificado o medida de la que proceden los datos")
        self.s_range = QLabel()
        save = QPushButton("Guardar referencia")
        save.clicked.connect(self._save_s_data)
        plot = QPushButton("Ver parámetros S")
        plot.clicked.connect(self._plot)
        buttons = QHBoxLayout()
        buttons.addWidget(save)
        buttons.addWidget(plot)
        buttons.addStretch(1)
        s_form.addRow("Fichero", file_row)
        s_form.addRow("Referencia", self.s_reference)
        s_form.addRow("Rango de los datos", self.s_range)
        s_form.addRow(buttons)

        self.vna_box = QGroupBox("Instrumento")
        vna_layout = QVBoxLayout(self.vna_box)
        self.vna_label = QLabel()
        self.vna_label.setWordWrap(True)
        check = QPushButton("Comprobación funcional del VNA…")
        check.clicked.connect(self._functional_check)
        vna_layout.addWidget(self.vna_label)
        vna_layout.addWidget(check)

        for widget in (self.title, self.summary, self.s_box, self.vna_box):
            layout.addWidget(widget)
        layout.addStretch(1)
        self.show_equipment(None)

    def show_equipment(self, equipment: Equipment | None) -> None:
        self.equipment = equipment
        self.s_box.setVisible(False)
        self.vna_box.setVisible(False)
        if equipment is None:
            self.title.setText("Seleccione un equipo")
            self.summary.clear()
            return
        model = self.library.model_of(equipment)
        category = self.library.category_of(equipment)
        info = self.library.frequency_range_of(equipment)
        self.title.setText(equipment.id)
        lines = [
            f"<b>Modelo:</b> {model.display_name if model else equipment.model_id}",
            f"<b>Categoría:</b> {category.name if category else '?'}",
        ]
        if equipment.serial_number:
            lines.append(f"<b>Nº de serie:</b> {equipment.serial_number}")
        if equipment.description:
            lines.append(f"<b>Descripción:</b> {equipment.description}")
        lines.append(f"<b>Estado:</b> {equipment.status.replace('_', ' ')}")
        lines.append(f"<b>Rango útil:</b> {info.range if info.range else 'sin definir'}")
        lines.extend(f"&nbsp;&nbsp;· {source}" for source in info.sources)
        for issue in info.issues:
            lines.append(f"<span style='color:#c0392b'>{issue.message}</span>")
        calibration = equipment.calibration
        if calibration and calibration.due:
            lines.append(
                f"<b>Calibración:</b> {calibration.certificate or ''} válida hasta {calibration.due:%d/%m/%Y}"
            )
        elif category and category.requires_calibration:
            lines.append("<b>Calibración:</b> <span style='color:#d98a00'>sin datos</span>")
        if equipment.location:
            lines.append(f"<b>Ubicación:</b> {equipment.location}")
        if model and model.status:
            lines.append(f"<b>Estado del modelo:</b> {model.status}")
        if equipment.source_path:
            lines.append(f"<small>{equipment.source_path}</small>")
        self.summary.setText("<br>".join(lines))

        if equipment.s_parameters is not None or (category and category.requires_s_parameters):
            self.s_box.setVisible(True)
            data = equipment.s_parameters
            self.s_file.setText(data.file if data else "")
            self.s_reference.setText(data.reference if data else "")
            try:
                rng = self.library.touchstone_of(equipment).frequency_range if data else None
                self.s_range.setText(str(rng) if rng else "sin datos S")
            except TouchstoneError as exc:
                self.s_range.setText(f"<span style='color:#c0392b'>{exc}</span>")
        if model and model.category == "vna":
            self.vna_box.setVisible(True)
            simulated = equipment.connection is not None and equipment.connection.simulated
            driver = (model.communication or {}).get("driver", "—")
            self.vna_label.setText(
                f"Driver: <b>{driver}</b>" + (" — en <b>simulación</b> (VNA dummy)" if simulated else " — real, pendiente de integrar")
            )

    def _change_file(self) -> None:
        if self.equipment is None:
            return
        filename, _ = QFileDialog.getOpenFileName(self, "Fichero de parámetros S", str(self.library.root), "Touchstone (*.s1p *.s2p *.s3p *.s4p *.S1P *.S2P)")
        if not filename:
            return
        try:
            read_touchstone(filename)
        except TouchstoneError as exc:
            QMessageBox.warning(self, "Datos S", f"El fichero no es válido: {exc}")
            return
        relative = self.library.copy_s_parameter_file(Path(filename))
        reference = self.s_reference.text().strip()
        self.equipment.s_parameters = SParameterData(file=relative, reference=reference, measured_on=date.today())
        self._save()

    def _save_s_data(self) -> None:
        if self.equipment is None or self.equipment.s_parameters is None:
            QMessageBox.information(self, "Datos S", "Primero asigne un fichero de parámetros S.")
            return
        self.equipment.s_parameters.reference = self.s_reference.text().strip()
        self._save()

    def _save(self) -> None:
        assert self.equipment is not None
        try:
            self.library.save_equipment(self.equipment)
        except OSError as exc:
            QMessageBox.critical(self, "Guardar equipo", str(exc))
            return
        self.changed.emit(self.equipment.id)

    def _plot(self) -> None:
        if self.equipment is not None and self.equipment.s_parameters is not None:
            SParameterDialog(self.library, self.equipment, self).exec()

    def _functional_check(self) -> None:
        if self.equipment is not None:
            VnaCheckDialog(self.library, self.equipment, self).exec()


class InventoryView(QWidget):
    equipment_changed = Signal(str)

    def __init__(self, library: Library, parent: QWidget | None = None):
        super().__init__(parent)
        self.library = library
        layout = QVBoxLayout(self)
        header = QHBoxLayout()
        header.addWidget(style.title_label("Inventario de equipos"))
        header.addStretch(1)
        self.category_filter = QComboBox()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Buscar por ID, modelo, nº de serie…")
        self.search.setClearButtonEnabled(True)
        header.addWidget(QLabel("Categoría:"))
        header.addWidget(self.category_filter)
        header.addWidget(self.search)
        layout.addLayout(header)

        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSortingEnabled(True)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.sortByColumn(0, Qt.SortOrder.AscendingOrder)
        self.table.itemSelectionChanged.connect(self._selected)
        self.detail = EquipmentDetail(library)
        self.detail.changed.connect(self._detail_changed)
        detail_scroll = QScrollArea()
        detail_scroll.setWidgetResizable(True)
        detail_scroll.setWidget(self.detail)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self.table)
        splitter.addWidget(detail_scroll)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        layout.addWidget(splitter)
        self.category_filter.currentIndexChanged.connect(lambda _i: self.refresh())
        self.search.textChanged.connect(lambda _t: self.refresh())
        self.reload()

    def reload(self) -> None:
        current = self.category_filter.currentData()
        self.category_filter.blockSignals(True)
        self.category_filter.clear()
        self.category_filter.addItem("Todas", None)
        for category in sorted(self.library.categories.values(), key=lambda c: c.name):
            self.category_filter.addItem(category.name, category.id)
        index = self.category_filter.findData(current)
        self.category_filter.setCurrentIndex(max(index, 0))
        self.category_filter.blockSignals(False)
        self.refresh()

    def refresh(self, select_id: str | None = None) -> None:
        selected = select_id or (self.detail.equipment.id if self.detail.equipment else None)
        category_id = self.category_filter.currentData()
        text = self.search.text().strip().lower()
        today = date.today()
        self.table.setSortingEnabled(False)
        self.table.setRowCount(0)
        for equipment in self.library.equipment.values():
            model = self.library.model_of(equipment)
            category = self.library.category_of(equipment)
            if category_id and (category is None or category.id != category_id):
                continue
            haystack = " ".join(filter(None, [equipment.id, model.display_name if model else "", equipment.serial_number or ""])).lower()
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
                f"{due:%Y-%m-%d}" if due else "",
                equipment.status.replace("_", " "),
                Path(equipment.s_parameters.file).name if equipment.s_parameters else "",
            ]
            row = self.table.rowCount()
            self.table.insertRow(row)
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setData(Qt.ItemDataRole.UserRole, equipment.id)
                if column == 5 and due:
                    if due < today:
                        item.setForeground(style.ERROR)
                        item.setToolTip("Calibración caducada")
                    elif due <= today + timedelta(days=30):
                        item.setForeground(style.WARNING)
                        item.setToolTip("Caduca en menos de 30 días")
                if column == 6 and equipment.status != "en_servicio":
                    item.setForeground(style.ERROR)
                if info.issues and column == 4:
                    item.setForeground(style.ERROR)
                    item.setToolTip("\n".join(i.message for i in info.issues))
                if equipment.connection and equipment.connection.simulated and column == 0:
                    item.setToolTip("Equipo en simulación")
                    item.setForeground(QColor("#6c3483"))
                self.table.setItem(row, column, item)
        self.table.setSortingEnabled(True)
        self.table.resizeColumnsToContents()
        for column in range(self.table.columnCount()):
            self.table.setColumnWidth(column, min(self.table.columnWidth(column), 240))
        for row in range(self.table.rowCount()):
            if self.table.item(row, 0).text() == selected:
                self.table.selectRow(row)
                break

    def _selected(self) -> None:
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return
        equipment_id = self.table.item(rows[0].row(), 0).data(Qt.ItemDataRole.UserRole)
        self.detail.show_equipment(self.library.equipment.get(equipment_id))

    def _detail_changed(self, equipment_id: str) -> None:
        self.refresh(select_id=equipment_id)
        self.detail.show_equipment(self.library.equipment.get(equipment_id))
        self.equipment_changed.emit(equipment_id)
