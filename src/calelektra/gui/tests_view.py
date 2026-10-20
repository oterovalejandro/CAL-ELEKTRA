"""Executed tests: history table and detail of each test (calibration, results, files, chart)."""

from __future__ import annotations

import html

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QAction, QDesktopServices
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QScrollArea,
    QSplitter,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from calelektra.catalog.library import Library, LibraryError
from calelektra.core.touchstone import TouchstoneError, read_touchstone
from calelektra.gui import style
from calelektra.gui.icons import icon
from calelektra.gui.plots import SParameterPlot
from calelektra.procedures.record import STATUS_NAMES, TestRecord

COLUMNS = ["Fecha", "DUT", "Procedimiento", "Setup", "Calibración", "Estado", "Operador"]


class TestDetail(QWidget):
    __test__ = False  # not a pytest test class

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.record: TestRecord | None = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 0, 4, 4)
        self.header = style.Card()
        top = QHBoxLayout()
        self.title = QLabel("Seleccione un ensayo")
        self.title.setObjectName("pageTitle")
        self.chips = QHBoxLayout()
        top.addWidget(self.title, 1)
        top.addLayout(self.chips)
        self.subtitle = QLabel()
        self.subtitle.setObjectName("pageSubtitle")
        self.subtitle.setWordWrap(True)
        self.header.body.addLayout(top)
        self.header.body.addWidget(self.subtitle)
        self.plot_card = style.Card("Resultado", icon_name="grafica")
        self.plot = SParameterPlot()
        self.plot.setMinimumHeight(300)
        self.plot_card.body.addWidget(self.plot)
        self.info_card = style.Card("Calibración, verificación y resultados", icon_name="ok")
        self.info = QLabel()
        self.info.setWordWrap(True)
        self.info.setTextFormat(Qt.TextFormat.RichText)
        self.info.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.info_card.body.addWidget(self.info)
        self.files_card = style.Card("Ficheros", icon_name="carpeta")
        self.files = QListWidget()
        self.files.setMaximumHeight(130)
        self.files.itemDoubleClicked.connect(self._open_file)
        self.files_card.body.addWidget(self.files)
        for card in (self.header, self.plot_card, self.info_card, self.files_card):
            layout.addWidget(card)
        layout.addStretch(1)
        self.show_record(None)

    def show_record(self, record: TestRecord | None) -> None:
        self.record = record
        while self.chips.count():
            widget = self.chips.takeAt(0).widget()
            if widget:
                widget.deleteLater()
        for card in (self.plot_card, self.info_card, self.files_card):
            card.setVisible(record is not None)
        if record is None:
            self.title.setText("Seleccione un ensayo")
            self.subtitle.setText("Cada ensayo guarda su registro (ensayo.yaml), los ficheros de resultados y el registro de operaciones.")
            return
        self.title.setText(record.dut.get("id", record.id) if record.dut else record.id)
        duration = f" · {(record.finished - record.started).total_seconds():.0f} s" if record.finished else ""
        self.subtitle.setText(
            f"{record.procedure.get('nombre', '—')} · setup {record.setup.get('nombre', '—')}<br>"
            f"{record.started:%d/%m/%Y %H:%M:%S}{duration} · operador: {record.operator or '—'} · {record.id}"
        )
        self.subtitle.setTextFormat(Qt.TextFormat.RichText)
        self.chips.addWidget(style.chip(record.status_name, style.status_color(record.status)))
        if record.simulated:
            self.chips.addWidget(style.chip("SIMULADO", style.SIMULATION))

        parts = []
        if record.message:
            parts.append(html.escape(record.message))
        calibration = record.calibration
        if calibration:
            text = f"<b>Calibración:</b> {calibration.get('metodo', '—')}"
            if calibration.get("kit"):
                text += f" · kit {calibration['kit']}" + (f" ({html.escape(str(calibration.get('kit_en_vna')))})" if calibration.get("kit_en_vna") else "")
            if calibration.get("planos"):
                text += " · " + ", ".join(f"{k}: {v}" for k, v in calibration["planos"].items())
            parts.append(text)
        checks = record.verification.get("comprobaciones", [])
        if checks:
            items = "".join(
                f"<li>{html.escape(c['comprobacion'])}: {c['valor_db']:g} dB (límite {c['limite_db']:g} dB) "
                f"<b style='color:{'#2e8b57' if c['correcto'] else '#c0392b'}'>{'OK' if c['correcto'] else 'FUERA'}</b></li>"
                for c in checks
            )
            parts.append(f"<b>Verificación</b><ul style='margin-top:2px'>{items}</ul>")
        results = record.results
        if results:
            rows = []

            def flatten(prefix: str, value) -> None:
                if isinstance(value, dict):
                    for key, item in value.items():
                        flatten(f"{prefix} · {key}" if prefix else str(key), item)
                else:
                    rows.append(f"<li>{html.escape(prefix.replace('_', ' '))}: <b>{html.escape(str(value))}</b></li>")

            flatten("", results)
            parts.append(f"<b>Resultados</b><ul style='margin-top:2px'>{''.join(rows)}</ul>")
        sweep = record.sweep
        if sweep:
            parts.append(
                f"<b>Barrido:</b> {sweep.get('inicio')} – {sweep.get('fin')}, {sweep.get('puntos')} puntos {sweep.get('tipo', '')}, "
                f"IFBW {sweep.get('ifbw')}, {sweep.get('potencia_dbm')} dBm"
            )
        instrument = record.instrument
        if instrument:
            parts.append(f"<b>VNA:</b> {html.escape(str(instrument.get('identificacion', '—')))}")
        if record.equipment:
            equipment = ", ".join(f"{e.get('id')} ({e.get('rol')})" for e in record.equipment)
            parts.append(f"<b>Equipos:</b> {html.escape(equipment)}")
        self.info.setText("<br>".join(parts) or "Sin datos")

        self.files.clear()
        self.plot.clear()
        folder = record.folder
        for name in record.files:
            kind = "grafica" if name.lower().endswith((".s1p", ".s2p")) else ("exportar" if name.lower().endswith(".csv") else "procedimientos")
            item = QListWidgetItem(icon(kind, style.PRIMARY.name()), name)
            item.setData(Qt.ItemDataRole.UserRole, str(folder / name) if folder else name)
            self.files.addItem(item)
            if folder and name.lower().endswith((".s1p", ".s2p")):
                try:
                    data = read_touchstone(folder / name)
                except TouchstoneError:
                    continue
                label = name.rsplit(".", 1)[0].rsplit("_", 1)[-1] if name.lower().endswith(".s1p") else "DUT"
                self.plot.set_data(label, data.frequencies, data.s, log_x=sweep.get("tipo") == "logaritmico")
        self.plot_card.setVisible(bool(self.plot.datasets))

    def _open_file(self, item: QListWidgetItem) -> None:
        QDesktopServices.openUrl(QUrl.fromLocalFile(item.data(Qt.ItemDataRole.UserRole)))


class TestsView(QWidget):
    __test__ = False  # not a pytest test class

    new_test = Signal()
    repeat_test = Signal(str, str)  # procedure id, setup id
    tests_changed = Signal()
    title = "Ensayos"

    def __init__(self, library: Library, parent: QWidget | None = None):
        super().__init__(parent)
        self.library = library
        self.records: list[TestRecord] = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 10)
        layout.addWidget(
            style.PageHeader(
                "Ensayos",
                "Calibraciones ejecutadas: cada una con su procedimiento, setup, equipos usados (y sus certificados), "
                "calibración del VNA, verificación y ficheros de resultados.",
            )
        )
        filters = QHBoxLayout()
        self.status_filter = QComboBox()
        self.status_filter.addItem("Todos los estados", None)
        for key, name in STATUS_NAMES.items():
            self.status_filter.addItem(name, key)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Buscar por DUT, procedimiento, setup u operador…")
        self.search.setClearButtonEnabled(True)
        self.search.addAction(icon("buscar", style.NEUTRAL.name()), QLineEdit.ActionPosition.LeadingPosition)
        self.count = QLabel()
        self.count.setObjectName("pageSubtitle")
        filters.addWidget(self.status_filter)
        filters.addWidget(self.search, 1)
        filters.addWidget(self.count)
        layout.addLayout(filters)
        self.table = style.make_table(COLUMNS)
        self.table.itemSelectionChanged.connect(self._selected)
        self.detail = TestDetail()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.detail)
        scroll.setMinimumWidth(460)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self.table)
        splitter.addWidget(scroll)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        layout.addWidget(splitter, 1)

        self.action_new = QAction(icon("ejecutar", style.OK.name()), "Nuevo\nensayo", self)
        self.action_repeat = QAction(icon("recargar", style.PRIMARY.name()), "Repetir", self)
        self.action_folder = QAction(icon("carpeta", style.PRIMARY.name()), "Abrir\ncarpeta", self)
        self.action_delete = QAction(icon("eliminar", style.ERROR.name()), "Eliminar", self)
        self.action_new.triggered.connect(self.new_test.emit)
        self.action_repeat.triggered.connect(self._repeat)
        self.action_folder.triggered.connect(self._open_folder)
        self.action_delete.triggered.connect(self._delete)
        self.status_filter.currentIndexChanged.connect(lambda _i: self.refresh())
        self.search.textChanged.connect(lambda _t: self.refresh())
        self.reload()

    def ribbon_actions(self) -> list[QAction | None]:
        return [self.action_new, self.action_repeat, None, self.action_folder, self.action_delete]

    def set_library(self, library: Library) -> None:
        self.library = library
        self.reload()

    def reload(self, select: str | None = None) -> None:
        self.records = self.library.tests()
        self.refresh(select)

    def refresh(self, select: str | None = None) -> None:
        current = select or (self.detail.record.id if self.detail.record else None)
        status = self.status_filter.currentData()
        text = self.search.text().strip().lower()
        self.table.setRowCount(0)
        shown = 0
        for record in self.records:
            if status and record.status != status:
                continue
            values = [
                f"{record.started:%Y-%m-%d %H:%M}",
                record.dut.get("id", "—") if record.dut else "—",
                record.procedure.get("nombre", "—"),
                record.setup.get("nombre", "—"),
                str(record.calibration.get("metodo", "—")) if record.calibration else "—",
                record.status_name + (" · simulado" if record.simulated else ""),
                record.operator or "",
            ]
            if text and text not in " ".join(values).lower():
                continue
            row = self.table.rowCount()
            self.table.insertRow(row)
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setData(Qt.ItemDataRole.UserRole, record.id)
                if column == 5:
                    item.setIcon(style.status_icon(record.status))
                    item.setForeground(style.status_color(record.status))
                self.table.setItem(row, column, item)
            shown += 1
        self.table.resizeColumnsToContents()
        self.count.setText(f"{shown} de {len(self.records)} ensayos")
        for row in range(self.table.rowCount()):
            if self.table.item(row, 0).data(Qt.ItemDataRole.UserRole) == current:
                self.table.selectRow(row)
                break
        else:
            if self.table.rowCount():
                self.table.selectRow(0)
            else:
                self.detail.show_record(None)
        self._update_actions()

    def _record(self) -> TestRecord | None:
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return None
        test_id = self.table.item(rows[0].row(), 0).data(Qt.ItemDataRole.UserRole)
        return next((r for r in self.records if r.id == test_id), None)

    def _selected(self) -> None:
        self.detail.show_record(self._record())
        self._update_actions()

    def _update_actions(self) -> None:
        record = self._record()
        has = record is not None
        self.action_folder.setEnabled(has)
        self.action_delete.setEnabled(has)
        self.action_repeat.setEnabled(
            has and record.procedure.get("id") in self.library.procedures and record.setup.get("id") in self.library.setups
        )

    def _repeat(self) -> None:
        record = self._record()
        if record is not None:
            self.repeat_test.emit(record.procedure.get("id"), record.setup.get("id"))

    def _open_folder(self) -> None:
        record = self._record()
        if record is not None and record.folder is not None:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(record.folder)))

    def _delete(self) -> None:
        record = self._record()
        if record is None:
            return
        answer = QMessageBox.question(self, "Eliminar ensayo", f"¿Eliminar el ensayo «{record.id}» y todos sus ficheros?")
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            self.library.delete_test(record.id)
        except (LibraryError, OSError) as exc:
            QMessageBox.warning(self, "Eliminar ensayo", str(exc))
            return
        self.detail.show_record(None)
        self.reload()
        self.tests_changed.emit()
