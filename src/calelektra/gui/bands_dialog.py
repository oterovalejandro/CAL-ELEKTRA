"""A value by frequency bands (``numero_bandas``, ``core/bands.py``) entered in a table of segments.

Each row is a band: up to which frequency it goes and its value (the VSWR of
a connector in its datasheet, for instance). The first band starts at the
lowest frequency; each other one where the previous one ends; the last one
may have no upper frequency (up to any frequency).
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from calelektra.catalog.fields import FieldError, FieldSpec, coerce
from calelektra.core.bands import BandValues
from calelektra.core.units import format_frequency
from calelektra.gui import style
from calelektra.gui.icons import icon

FROM, UP_TO, VALUE = range(3)


def _value_text(value: float) -> str:
    return f"{value:g}".replace(".", ",")


class BandsDialog(QDialog):
    """Table of the bands of a field: «Desde» (computed), «Hasta» and the value."""

    def __init__(self, spec: FieldSpec, bands: BandValues | None = None, parent: QWidget | None = None):
        super().__init__(parent)
        self.spec = spec
        self.result_bands: BandValues | None = bands
        self.setWindowTitle(f"{spec.label}: valores por bandas")
        self.resize(560, 420)
        layout = QVBoxLayout(self)
        layout.addWidget(
            style.PageHeader(
                spec.label,
                "Una fila por banda: hasta qué frecuencia llega (incluida) y su valor. Cada banda empieza donde acaba la "
                "anterior; la última puede quedar sin «Hasta» (vale para cualquier frecuencia más alta).",
            )
        )
        unit = f" ({spec.unit})" if spec.unit else ""
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Desde", "Hasta (frecuencia)", f"Valor{unit}"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.cellChanged.connect(self._cell_changed)
        layout.addWidget(self.table, 1)
        buttons = QHBoxLayout()
        add = QPushButton(icon("nuevo", style.PRIMARY.name()), "Añadir banda")
        add.clicked.connect(lambda: self.add_band())
        remove = QPushButton(icon("eliminar", style.ERROR.name()), "Quitar banda")
        remove.clicked.connect(self.remove_band)
        buttons.addWidget(add)
        buttons.addWidget(remove)
        buttons.addStretch(1)
        layout.addLayout(buttons)
        self.error = QLabel()
        self.error.setWordWrap(True)
        self.error.setStyleSheet(f"color: {style.ERROR.name()};")
        layout.addWidget(self.error)
        actions = QHBoxLayout()
        actions.addStretch(1)
        cancel = QPushButton("Cancelar")
        cancel.clicked.connect(self.reject)
        accept = QPushButton("Aceptar")
        accept.setObjectName("primary")
        accept.setDefault(True)
        accept.clicked.connect(self.accept)
        actions.addWidget(cancel)
        actions.addWidget(accept)
        layout.addLayout(actions)
        for upper, value in bands.bands if bands is not None else []:
            self.add_band(format_frequency(upper) if upper is not None else "", _value_text(value))
        if not self.table.rowCount():
            self.add_band()

    # ── rows ───────────────────────────────────────────────────────────────
    def add_band(self, upper: str = "", value: str = "") -> None:
        self.table.blockSignals(True)
        row = self.table.rowCount()
        self.table.insertRow(row)
        start = QTableWidgetItem()
        start.setFlags(start.flags() & ~Qt.ItemFlag.ItemIsEditable)
        start.setForeground(QColor(style.MUTED))
        self.table.setItem(row, FROM, start)
        self.table.setItem(row, UP_TO, QTableWidgetItem(upper))
        self.table.setItem(row, VALUE, QTableWidgetItem(value))
        self.table.blockSignals(False)
        self._update_starts()

    def remove_band(self) -> None:
        row = self.table.currentRow()
        self.table.removeRow(row if row >= 0 else self.table.rowCount() - 1)
        self._update_starts()

    def _cell_changed(self, _row: int, column: int) -> None:
        if column == UP_TO:
            self._update_starts()

    def _update_starts(self) -> None:
        self.table.blockSignals(True)
        for row in range(self.table.rowCount()):
            previous = self.table.item(row - 1, UP_TO) if row else None
            text = previous.text().strip() if previous is not None else ""
            self.table.item(row, FROM).setText(text if row else "inicio")
        self.table.blockSignals(False)

    # ── result ─────────────────────────────────────────────────────────────
    def rows(self) -> list[dict[str, str | None]]:
        """The rows with something written, as ``{hasta, valor}`` (YAML of the field)."""
        found = []
        for row in range(self.table.rowCount()):
            upper, value = (self.table.item(row, column).text().strip() for column in (UP_TO, VALUE))
            if upper or value:
                found.append({"hasta": upper or None, "valor": value})
        return found

    def bands(self) -> BandValues | None:
        """The bands written (``FieldError`` if they are not valid; ``None`` with an empty table)."""
        rows = self.rows()
        if not rows:
            return None
        if any(not row["valor"] for row in rows):
            raise FieldError("cada banda necesita su valor")
        return coerce(self.spec, rows)

    def accept(self) -> None:
        try:
            self.result_bands = self.bands()
        except FieldError as exc:
            self.error.setText(str(exc))
            return
        super().accept()
