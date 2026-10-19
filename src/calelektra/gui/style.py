"""Colours, icons and small widget helpers shared by the GUI."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QIcon, QPalette
from PySide6.QtWidgets import QApplication, QFrame, QLabel, QStyle, QWidget

from calelektra.core.issues import Severity

PRIMARY = QColor("#1f3a5f")
ACCENT = QColor("#2b7bb9")
OK = QColor("#2e8b57")
WARNING = QColor("#d98a00")
ERROR = QColor("#c0392b")
NEUTRAL = QColor("#8a94a3")
DUT = QColor("#7b5ea7")

SEVERITY_COLORS = {Severity.INFO: ACCENT, Severity.WARNING: WARNING, Severity.ERROR: ERROR}

KIND_COLORS = {
    "instrumento": QColor("#2b7bb9"),
    "pasivo": QColor("#4f8a8b"),
    "posicionador": QColor("#8d6e63"),
    "recinto": QColor("#7b5ea7"),
}


def apply_theme(app: QApplication) -> None:
    app.setStyle("Fusion")
    palette = app.palette()
    palette.setColor(QPalette.ColorRole.Window, QColor("#f3f5f8"))
    palette.setColor(QPalette.ColorRole.Base, QColor("#ffffff"))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor("#eef2f7"))
    palette.setColor(QPalette.ColorRole.Highlight, ACCENT)
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#ffffff"))
    app.setPalette(palette)
    app.setStyleSheet(
        """
        QListWidget#navegacion { background: #1f3a5f; color: #e8eef6; border: none; font-size: 13px; }
        QListWidget#navegacion::item { padding: 10px 14px; }
        QListWidget#navegacion::item:selected { background: #2b7bb9; color: white; }
        QListWidget#navegacion::item:disabled { color: #7d8ea6; }
        QLabel.titulo { font-size: 18px; font-weight: 600; color: #1f3a5f; }
        QGroupBox { font-weight: 600; margin-top: 10px; }
        QGroupBox::title { subcontrol-origin: margin; left: 8px; padding: 0 4px; color: #1f3a5f; }
        """
    )


def severity_icon(widget: QWidget, severity: Severity | None) -> QIcon:
    pixmap = {
        Severity.ERROR: QStyle.StandardPixmap.SP_MessageBoxCritical,
        Severity.WARNING: QStyle.StandardPixmap.SP_MessageBoxWarning,
        Severity.INFO: QStyle.StandardPixmap.SP_MessageBoxInformation,
        None: QStyle.StandardPixmap.SP_DialogApplyButton,
    }[severity]
    return widget.style().standardIcon(pixmap)


def title_label(text: str) -> QLabel:
    label = QLabel(text)
    label.setProperty("class", "titulo")
    return label


class StatusBadge(QLabel):
    """Coloured pill: «Setup válido» / «ALARMA»…"""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setMinimumWidth(190)
        self.set_state("—", NEUTRAL)

    def set_state(self, text: str, color: QColor) -> None:
        self.setText(text)
        self.setStyleSheet(
            f"QLabel {{ background: {color.name()}; color: white; border-radius: 10px;"
            " padding: 4px 12px; font-weight: 600; }"
        )


def separator() -> QFrame:
    line = QFrame()
    line.setFrameShape(QFrame.Shape.HLine)
    line.setFrameShadow(QFrame.Shadow.Sunken)
    return line
