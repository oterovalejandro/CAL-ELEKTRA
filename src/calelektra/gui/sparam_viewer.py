"""A file of S-parameters with uncertainty (``.sdatb``, ``.sdatcv``) or without it (Touchstone) in the view
of the standards of the kits: chart, table and correlation (Archivo → Ver parámetros S con incertidumbre…).

To look at a file of METAS VNA Tools without making it a standard of a kit, e.g. to compare its
correlation between frequencies with that of a standard of CAL-ELEKTRA.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from calelektra.gui.icons import icon
from calelektra.gui.kit_standards import StandardPreview
from calelektra.sdata import SDataError, SParamData, load_sparam_data

FILE_FILTER = "Parámetros S (*.sdatb *.sdatcv *.s1p *.s2p *.s3p *.s4p *.S1P *.S2P *.S3P *.S4P *.ts);;Todos los ficheros (*)"


class SParamViewer(QDialog):
    """The S-parameters of a file with their uncertainty."""

    def __init__(self, path: Path | None = None, folder: Path | None = None, parent: QWidget | None = None):
        super().__init__(parent)
        self.folder = folder or Path.home()
        self.path: Path | None = None
        self.data: SParamData | None = None
        self.setWindowTitle("Parámetros S con incertidumbre")
        self.resize(1000, 780)
        layout = QVBoxLayout(self)
        self.preview = StandardPreview()
        layout.addWidget(self.preview, 1)
        buttons = QHBoxLayout()
        other = QPushButton(icon("abrir"), "Abrir otro fichero…")
        other.clicked.connect(self.choose)
        close = QPushButton("Cerrar")
        close.clicked.connect(self.close)
        buttons.addWidget(other)
        buttons.addStretch(1)
        buttons.addWidget(close)
        layout.addLayout(buttons)
        if path is not None:
            self.open_file(path)

    def choose(self) -> None:
        filename, _ = QFileDialog.getOpenFileName(self, "Parámetros S con incertidumbre", str(self.path.parent if self.path else self.folder), FILE_FILTER)
        if filename:
            self.open_file(Path(filename))

    def open_file(self, path: Path) -> bool:
        path = Path(path)
        QGuiApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            data = load_sparam_data(path)
        except SDataError as exc:
            self.preview.show_message(f"✖ {exc}")
            return False
        finally:
            QGuiApplication.restoreOverrideCursor()
        self.path, self.data = path, data
        self.setWindowTitle(f"Parámetros S con incertidumbre · {path.name}")
        self.preview.show_data(data, path.name)
        return True
