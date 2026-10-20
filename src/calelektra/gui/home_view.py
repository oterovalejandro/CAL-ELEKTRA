"""Home dashboard: indicators, latest tests, setup status, calibration alerts and instruments."""

from __future__ import annotations

from datetime import date, datetime, timedelta

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QGridLayout,
    QHBoxLayout,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QScrollArea,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from calelektra.catalog.library import Library
from calelektra.core.issues import Severity
from calelektra.gui import style
from calelektra.gui.icons import icon
from calelektra.gui.setups_view import setup_status
from calelektra.instruments.factory import driver_description
from calelektra.procedures.specs import VERDICTS


class HomeView(QWidget):
    open_setup = Signal(str)
    open_test = Signal(str)  # folder of the test
    open_equipment = Signal(str)
    new_test = Signal()
    new_equipment = Signal()
    new_setup = Signal()
    title = "Inicio"

    def __init__(self, library: Library, parent: QWidget | None = None):
        super().__init__(parent)
        self.library = library
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        outer.addWidget(scroll)
        content = QWidget()
        scroll.setWidget(content)
        layout = QVBoxLayout(content)
        layout.setContentsMargins(18, 16, 18, 12)
        layout.setSpacing(12)

        self.header = style.PageHeader("CAL-ELEKTRA", "Laboratorio de calibración de equipos de MC")
        for text, slot, primary, icon_name in (
            ("Nuevo ensayo", self.new_test.emit, True, "ejecutar"),
            ("Nuevo equipo", self.new_equipment.emit, False, "equipos"),
            ("Nuevo setup", self.new_setup.emit, False, "setups"),
        ):
            button = QPushButton(icon(icon_name, "#ffffff" if primary else style.PRIMARY.name()), text)
            if primary:
                button.setObjectName("primary")
            button.clicked.connect(slot)
            self.header.right.addWidget(button)
        layout.addWidget(self.header)

        tiles = QHBoxLayout()
        tiles.setSpacing(12)
        self.tile_equipment = style.KpiTile("Equipos", "equipos", style.ACCENT)
        self.tile_calibration = style.KpiTile("Calibraciones", "ok", style.OK)
        self.tile_setups = style.KpiTile("Hardware setups", "setups", style.PRIMARY)
        self.tile_tests = style.KpiTile("Ensayos", "ensayos", style.INDIGO)
        self.tile_procedures = style.KpiTile("Plantillas de ensayo", "procedimientos", style.DUT)
        for tile in (self.tile_equipment, self.tile_calibration, self.tile_setups, self.tile_tests, self.tile_procedures):
            tiles.addWidget(tile, 1)
        layout.addLayout(tiles)

        grid = QGridLayout()
        grid.setSpacing(12)
        tests_card = style.Card("Últimos ensayos", icon_name="ensayos")
        self.tests = style.make_table(["Fecha", "Ensayo", "Equipo", "Estado"], stretch_column=1)
        self.tests.setMinimumHeight(220)
        self.tests.itemDoubleClicked.connect(lambda item: item.data(Qt.ItemDataRole.UserRole) and self.open_test.emit(item.data(Qt.ItemDataRole.UserRole)))
        tests_card.body.addWidget(self.tests)
        setups_card = style.Card("Estado de los hardware setups", icon_name="setups")
        self.setups = QListWidget()
        self.setups.setMinimumHeight(220)
        self.setups.itemDoubleClicked.connect(lambda item: self.open_setup.emit(item.data(Qt.ItemDataRole.UserRole)))
        setups_card.body.addWidget(self.setups)
        calibrations_card = style.Card("Calibraciones caducadas o próximas (30 días)", icon_name="aviso")
        self.calibrations = QListWidget()
        self.calibrations.itemDoubleClicked.connect(lambda item: item.data(Qt.ItemDataRole.UserRole) and self.open_equipment.emit(item.data(Qt.ItemDataRole.UserRole)))
        calibrations_card.body.addWidget(self.calibrations)
        instruments_card = style.Card("Instrumentos y biblioteca", icon_name="vna")
        self.instruments = QListWidget()
        self.instruments.setWordWrap(True)
        self.instruments.itemDoubleClicked.connect(lambda item: item.data(Qt.ItemDataRole.UserRole) and self.open_equipment.emit(item.data(Qt.ItemDataRole.UserRole)))
        instruments_card.body.addWidget(self.instruments)
        grid.addWidget(tests_card, 0, 0)
        grid.addWidget(setups_card, 0, 1)
        grid.addWidget(calibrations_card, 1, 0)
        grid.addWidget(instruments_card, 1, 1)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        layout.addLayout(grid, 1)

        self.action_test = QAction(icon("ejecutar", style.OK.name()), "Nuevo\nensayo", self)
        self.action_equipment = QAction(icon("equipos", style.PRIMARY.name()), "Nuevo\nequipo", self)
        self.action_setup = QAction(icon("setups", style.PRIMARY.name()), "Nuevo\nsetup", self)
        self.action_test.triggered.connect(self.new_test.emit)
        self.action_equipment.triggered.connect(self.new_equipment.emit)
        self.action_setup.triggered.connect(self.new_setup.emit)
        self.reload()

    def ribbon_actions(self) -> list[QAction | None]:
        return [self.action_test, None, self.action_equipment, self.action_setup]

    def set_library(self, library: Library) -> None:
        self.library = library
        self.reload()

    def reload(self) -> None:
        library = self.library
        today = date.today()
        self.header.subtitle.setText(f"Laboratorio de calibración de equipos de MC · biblioteca: {library.root}")

        in_service = sum(1 for e in library.equipment.values() if e.status == "en_servicio")
        self.tile_equipment.set_values(str(len(library.equipment)), f"{in_service} en servicio · {len(library.models)} modelos")

        expired, soon = [], []
        for equipment in sorted(library.equipment.values(), key=lambda e: e.id):
            due = equipment.calibration.due if equipment.calibration else None
            if due is None:
                continue
            if due < today:
                expired.append((equipment, due))
            elif due <= today + timedelta(days=30):
                soon.append((equipment, due))
        if expired:
            self.tile_calibration.set_values(str(len(expired)), f"caducadas · {len(soon)} próximas", style.ERROR)
        else:
            self.tile_calibration.set_values(str(len(soon)), "próximas a caducar (30 días)", style.WARNING if soon else style.OK)

        statuses = {setup.id: setup_status(library, setup) for setup in library.setups.values()}
        valid = sum(1 for severity, _ in statuses.values() if severity is not Severity.ERROR)
        alarms = len(statuses) - valid
        self.tile_setups.set_values(str(len(statuses)), f"{valid} válidos · {alarms} con alarma", style.ERROR if alarms else style.OK)

        tests = library.tests()
        recent = sum(1 for t in tests if t.started >= datetime.now() - timedelta(days=30))
        failed = sum(1 for t in tests if t.status in ("error", "abortado") or t.verdict == "FALLA")
        prepared = sum(1 for t in tests if t.status == "preparado")
        self.tile_tests.set_values(
            str(len(tests)), f"{recent} en 30 días · {prepared} preparados · {failed} FALLA/error", style.ERROR if failed else None
        )
        self.tile_procedures.set_values(str(len(library.procedures)), f"{len(library.templates)} plantillas de setup")

        self.tests.setRowCount(0)
        for record in tests[:12]:
            row = self.tests.rowCount()
            self.tests.insertRow(row)
            verdict = f" · {VERDICTS[record.verdict].upper()}" if record.verdict in ("PASA", "FALLA") else ""
            values = [
                f"{record.started:%d/%m %H:%M}",
                record.title,
                record.dut_id or "—",
                record.status_name + verdict + (" · sim." if record.simulated else ""),
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setData(Qt.ItemDataRole.UserRole, str(record.folder))
                item.setToolTip(f"{record.procedure.get('nombre', '—')}\n{record.folder}")
                if column == 3:
                    item.setIcon(style.status_icon(record.status))
                self.tests.setItem(row, column, item)
        if not tests:
            self.tests.insertRow(0)
            self.tests.setItem(0, 0, QTableWidgetItem("Aún no hay ensayos"))
        self.tests.resizeColumnsToContents()

        self.setups.clear()
        for setup in sorted(library.setups.values(), key=lambda s: s.name):
            severity, text = statuses[setup.id]
            item = QListWidgetItem(style.severity_icon(self, severity), f"{setup.name} — {text}")
            item.setData(Qt.ItemDataRole.UserRole, setup.id)
            item.setToolTip(f"{setup.required_range} · plantilla {setup.template}\nDoble clic para abrir")
            self.setups.addItem(item)

        self.calibrations.clear()
        for equipment, due in expired:
            item = QListWidgetItem(style.severity_icon(self, Severity.ERROR), f"{equipment.id}: caducada el {due:%d/%m/%Y}")
            item.setData(Qt.ItemDataRole.UserRole, equipment.id)
            self.calibrations.addItem(item)
        for equipment, due in soon:
            item = QListWidgetItem(style.severity_icon(self, Severity.WARNING), f"{equipment.id}: caduca el {due:%d/%m/%Y}")
            item.setData(Qt.ItemDataRole.UserRole, equipment.id)
            self.calibrations.addItem(item)
        if not self.calibrations.count():
            self.calibrations.addItem(QListWidgetItem(style.severity_icon(self, None), "Ninguna"))

        self.instruments.clear()
        for equipment in sorted(library.equipment_in_categories(["vna"]), key=lambda e: e.id):
            model = library.model_of(equipment)
            item = QListWidgetItem(icon("vna", style.ACCENT.name()), f"{equipment.id} — {model.display_name if model else ''}\n{driver_description(equipment, library)}")
            item.setData(Qt.ItemDataRole.UserRole, equipment.id)
            self.instruments.addItem(item)
        for issue in library.load_issues:
            self.instruments.addItem(QListWidgetItem(style.severity_icon(self, issue.severity), issue.message))
        if not library.load_issues:
            self.instruments.addItem(QListWidgetItem(style.severity_icon(self, None), "Biblioteca cargada sin problemas"))

