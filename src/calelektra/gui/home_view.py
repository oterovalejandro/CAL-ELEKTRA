"""Home dashboard: calibration alerts, setup status and library problems."""

from __future__ import annotations

from datetime import date, timedelta

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QGridLayout, QGroupBox, QLabel, QListWidget, QListWidgetItem, QVBoxLayout, QWidget

from calelektra.catalog.library import Library
from calelektra.core.issues import Severity
from calelektra.gui import style
from calelektra.gui.setups_view import setup_status


class HomeView(QWidget):
    open_setup = Signal(str)

    def __init__(self, library: Library, parent: QWidget | None = None):
        super().__init__(parent)
        self.library = library
        layout = QVBoxLayout(self)
        layout.addWidget(style.title_label("CAL-ELEKTRA — Laboratorio de calibración de equipos de MC"))
        self.summary = QLabel()
        layout.addWidget(self.summary)
        grid = QGridLayout()
        self.setups = QListWidget()
        self.setups.itemDoubleClicked.connect(lambda item: self.open_setup.emit(item.data(Qt.ItemDataRole.UserRole)))
        self.calibrations = QListWidget()
        self.problems = QListWidget()
        self.problems.setWordWrap(True)
        for column, (title, widget) in enumerate(
            (
                ("Hardware setups (doble clic para abrir)", self.setups),
                ("Calibraciones caducadas o próximas (30 días)", self.calibrations),
                ("Problemas al cargar la biblioteca", self.problems),
            )
        ):
            box = QGroupBox(title)
            QVBoxLayout(box).addWidget(widget)
            grid.addWidget(box, 0, column)
        layout.addLayout(grid)
        self.reload()

    def reload(self) -> None:
        library = self.library
        simulated = sum(1 for e in library.equipment.values() if e.connection and e.connection.simulated)
        self.summary.setText(
            f"Biblioteca: <b>{library.root}</b><br>"
            f"{len(library.categories)} categorías · {len(library.models)} modelos · {len(library.equipment)} equipos "
            f"({simulated} en simulación) · {len(library.templates)} plantillas · {len(library.setups)} setups"
        )
        self.setups.clear()
        for setup in sorted(library.setups.values(), key=lambda s: s.name):
            severity, text = setup_status(library, setup)
            item = QListWidgetItem(style.severity_icon(self, severity), f"{setup.name} — {text}")
            item.setData(Qt.ItemDataRole.UserRole, setup.id)
            self.setups.addItem(item)
        self.calibrations.clear()
        today = date.today()
        for equipment in sorted(library.equipment.values(), key=lambda e: e.id):
            due = equipment.calibration.due if equipment.calibration else None
            if due is None:
                continue
            if due < today:
                self.calibrations.addItem(QListWidgetItem(style.severity_icon(self, Severity.ERROR), f"{equipment.id}: caducada el {due:%d/%m/%Y}"))
            elif due <= today + timedelta(days=30):
                self.calibrations.addItem(QListWidgetItem(style.severity_icon(self, Severity.WARNING), f"{equipment.id}: caduca el {due:%d/%m/%Y}"))
        if not self.calibrations.count():
            self.calibrations.addItem(QListWidgetItem(style.severity_icon(self, None), "Ninguna"))
        self.problems.clear()
        for issue in library.load_issues:
            self.problems.addItem(QListWidgetItem(style.severity_icon(self, issue.severity), issue.message))
        if not self.problems.count():
            self.problems.addItem(QListWidgetItem(style.severity_icon(self, None), "Ninguno"))
