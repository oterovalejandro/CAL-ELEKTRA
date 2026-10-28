"""The tables Fwd and Rev of the certificate of a cable in the test window (tab «Tabla»).

The rows are those of the certificate (``procedures/report.py``): one per
report frequency of the template (``informe.frecuencias``, 21 by default),
with the values of the nearest measured point, the VSWR and the group delay
smoothed over 1 % of the points, and the formats of the certificate.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QScrollArea,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from calelektra.catalog.library import Library
from calelektra.core.units import format_frequency
from calelektra.gui import style
from calelektra.gui.icons import icon
from calelektra.procedures.record import TestRecord
from calelektra.procedures.report import ReportError, report_tables

# the columns of the tables of the certificate: key of the row and header (on several lines, as in the certificate)
COLUMNS = (
    ("frecuencia", "Frecuencia"),
    ("vswr", "VSWR\npuerto {port}"),
    ("u_vswr", "Incertidumbre\nVSWR"),
    ("limite_vswr", "Límite\nVSWR"),
    ("perdida", "Pérdida de\ninserción\n{direction} (dB)"),
    ("u_perdida", "Incertidumbre\npérdida de\ninserción (dB)"),
    ("limite_perdida", "Límite\npérdida de\ninserción (dB)"),
    ("retardo", "Desfase de\ngrupo (ns)"),
    ("u_retardo", "Incertidumbre\ndesfase de\ngrupo (ns)"),
)
DIRECTIONS = {
    "fwd": ("Fwd", 1, "VSWR del puerto 1 (S11), pérdida de inserción y desfase de grupo de S21"),
    "rev": ("Rev", 2, "VSWR del puerto 2 (S22), pérdida de inserción y desfase de grupo de S12"),
}
ROW_HEIGHT = 24


class ReportTablesView(QWidget):
    """Tables Fwd and Rev of a 2-port test at the frequencies of its certificate, one under the other."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        top = QHBoxLayout()
        self.note = QLabel()
        self.note.setObjectName("pageSubtitle")
        self.note.setWordWrap(True)
        self.copy_button = QPushButton(icon("duplicar", style.PRIMARY.name()), "Copiar tablas")
        self.copy_button.setToolTip("Copia las dos tablas, separadas por tabuladores, para pegarlas en Excel")
        self.copy_button.clicked.connect(self.copy)
        top.addWidget(self.note, 1)
        top.addWidget(self.copy_button, 0, Qt.AlignmentFlag.AlignTop)
        layout.addLayout(top)
        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        self.titles: dict[str, QLabel] = {}
        self.tables: dict[str, QTableWidget] = {}
        for key, (name, port, text) in DIRECTIONS.items():
            title = QLabel(f"<b>{name}</b> · {text}")
            headers = [header.format(port=port, direction=name) for _key, header in COLUMNS]
            table = style.make_table(headers)
            table.setSelectionMode(QTableWidget.SelectionMode.ExtendedSelection)
            table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectItems)
            table.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            table.verticalHeader().setDefaultSectionSize(ROW_HEIGHT)
            header = table.horizontalHeader()
            header.setDefaultAlignment(Qt.AlignmentFlag.AlignCenter)
            header.setMinimumSectionSize(90)
            header.setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
            content_layout.addWidget(title)
            content_layout.addWidget(table)
            content_layout.addSpacing(6)
            self.titles[key] = title
            self.tables[key] = table
        content_layout.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(content)
        layout.addWidget(scroll, 1)
        self.clear("Las tablas Fwd y Rev aparecen al terminar la medida")

    def clear(self, message: str) -> None:
        self.note.setText(message)
        for table in self.tables.values():
            table.setRowCount(0)
            self._fit(table)
        self.copy_button.setEnabled(False)

    def show_record(self, library: Library, record: TestRecord) -> bool:
        """Fill the tables from the test; a message instead if it has no 2-port measurement."""
        try:
            tables = report_tables(library, record)
        except ReportError as exc:
            self.clear(str(exc))
            return False
        if isinstance(tables.settings.frequencies, list):
            source = f"{len(tables.nominal)} frecuencias de la lista del informe"
        else:
            spacing = "logarítmicamente" if tables.logarithmic else "igualmente"
            span = f"{format_frequency(tables.span.start)} a {format_frequency(tables.span.stop)}"
            source = f"{len(tables.nominal)} frecuencias {spacing} espaciadas de {span}"
        notes = [
            f"Las tablas del certificado: {source}, con los valores del punto medido más cercano.",
            f"VSWR y desfase de grupo suavizados al 1 %; la pérdida de inserción y las incertidumbres (k = {tables.k:g}) son las del punto.",
        ]
        if not tables.uncertainty:
            notes.append("El ensayo no tiene incertidumbre calculada.")
        elif record.uncertainty.get("estado") == "pendiente":
            notes.append("Incertidumbre pendiente: sin la componente de temperatura hasta cargar las condiciones ambientales.")
        if tables.spec is None:
            notes.append("Sin límites: el modelo del cable no tiene especificación por fórmula.")
        self.note.setText(" ".join(notes))
        for key, table in self.tables.items():
            self._fill(table, tables.rows.get(key, []))
        self.copy_button.setEnabled(True)
        return True

    def _fill(self, table: QTableWidget, rows: list[dict[str, str]]) -> None:
        table.setRowCount(0)
        for row in rows:
            index = table.rowCount()
            table.insertRow(index)
            failed = row.get("pasa") == "FALLA"
            tip = f"Punto medido: {row.get('frecuencia_medida', '')}"
            if failed:
                tip += ". Fuera de un límite (con el valor del punto, sin suavizar)"
            for column, (key, _header) in enumerate(COLUMNS):
                item = QTableWidgetItem(row.get(key, ""))
                item.setTextAlignment((Qt.AlignmentFlag.AlignRight if column else Qt.AlignmentFlag.AlignLeft) | Qt.AlignmentFlag.AlignVCenter)
                if failed:
                    item.setForeground(style.ERROR)
                item.setToolTip(tip)
                table.setItem(index, column, item)
        self._fit(table)

    @staticmethod
    def _fit(table: QTableWidget) -> None:
        """All the rows visible: the page scrolls, not the table."""
        height = table.horizontalHeader().sizeHint().height() + ROW_HEIGHT * max(table.rowCount(), 1)
        table.setFixedHeight(height + 2 * table.frameWidth() + table.horizontalScrollBar().sizeHint().height())

    def text(self) -> str:
        """Both tables as tab-separated text, each with its title."""
        blocks = []
        for key, table in self.tables.items():
            name = DIRECTIONS[key][0]
            headers = [table.horizontalHeaderItem(column).text().replace("\n", " ") for column in range(table.columnCount())]
            lines = [name, "\t".join(headers)]
            for row in range(table.rowCount()):
                lines.append("\t".join(table.item(row, column).text() for column in range(table.columnCount())))
            blocks.append("\n".join(lines))
        return "\n\n".join(blocks)

    def copy(self) -> None:
        QGuiApplication.clipboard().setText(self.text())
