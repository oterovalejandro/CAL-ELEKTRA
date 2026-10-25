"""Theme (palette + style sheet), status colours and small widgets shared by the GUI."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QIcon, QPalette
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QTableWidget,
    QVBoxLayout,
    QWidget,
)

from calelektra.core.issues import Severity
from calelektra.gui.icons import icon

NAVY = QColor("#16263d")
PRIMARY = QColor("#1f3a5f")
ACCENT = QColor("#2b7bb9")
OK = QColor("#2e8b57")
WARNING = QColor("#d98a00")
ERROR = QColor("#c0392b")
NEUTRAL = QColor("#8a94a3")
DUT = QColor("#7b5ea7")
SIMULATION = QColor("#6c3483")
INDIGO = QColor("#5d6dbe")
TEXT = "#22303f"
MUTED = "#5b6b80"

SEVERITY_COLORS = {Severity.INFO: ACCENT, Severity.WARNING: WARNING, Severity.ERROR: ERROR}

KIND_COLORS = {
    "instrumento": QColor("#2b7bb9"),
    "pasivo": QColor("#4f8a8b"),
    "posicionador": QColor("#8d6e63"),
    "recinto": QColor("#7b5ea7"),
}

# statuses of test steps, checks and test records → (icon, colour, text)
STATUS = {
    "ok": ("ok", OK, "Correcto"),
    "completado": ("ok", OK, "Completado"),
    "aviso": ("aviso", WARNING, "Aviso"),
    "verificacion_fallida": ("aviso", WARNING, "Verificación fallida"),
    "error": ("error", ERROR, "Error"),
    "abortado": ("detener", ERROR, "Abortado"),
    "info": ("info", ACCENT, "Información"),
    "sin_datos": ("aviso", WARNING, "Sin datos"),
    "en_curso": ("en_curso", ACCENT, "En curso"),
    "preparado": ("pendiente", INDIGO, "Preparado"),
    "pendiente": ("pendiente", NEUTRAL, "Pendiente"),
    "omitido": ("omitido", NEUTRAL, "Omitido"),
}

# verdict of a test against its specification files
VERDICT_COLORS = {"PASA": OK, "FALLA": ERROR, "NO_EVALUADO": WARNING, "SIN_ESPECIFICACIONES": NEUTRAL}

STYLE_SHEET = """
QMainWindow, QDialog { background: #eef1f5; }
QWidget { color: #22303f; }
QToolTip { background: #16263d; color: #eef3f9; border: none; padding: 6px; }

/* navigation rail */
QWidget#rail { background: #16263d; }
QToolButton#railButton {
    color: #aebbd0; background: transparent; border: none; border-left: 3px solid transparent;
    padding: 9px 2px 8px 2px; font-size: 11px;
}
QToolButton#railButton:hover { background: #1d3150; color: #ffffff; }
QToolButton#railButton:checked { background: #22385a; color: #ffffff; border-left: 3px solid #3d9be9; }
QLabel#railLogo { color: #ffffff; font-weight: 700; font-size: 12px; padding: 12px 4px 10px 4px; }

/* ribbon */
QToolBar#ribbon { background: #ffffff; border: none; border-bottom: 1px solid #d9dee6; padding: 4px 8px; spacing: 2px; }
QToolBar#ribbon QToolButton { padding: 4px 8px; border-radius: 6px; color: #22303f; min-width: 64px; }
QToolBar#ribbon QToolButton:hover { background: #e8f1fb; }
QToolBar#ribbon QToolButton:pressed { background: #d4e6f7; }
QToolBar#ribbon QToolButton:disabled { color: #a3adba; }
QLabel#ribbonSection { color: #16263d; font-size: 15px; font-weight: 600; padding: 0 14px 0 6px; }

/* headers and cards */
QLabel#pageTitle { font-size: 19px; font-weight: 600; color: #16263d; }
QLabel#pageSubtitle { color: #5b6b80; }
QFrame#card { background: #ffffff; border: 1px solid #dde3ea; border-radius: 8px; }
QLabel#cardTitle { font-weight: 600; color: #1f3a5f; font-size: 13px; }
QFrame#kpi { background: #ffffff; border: 1px solid #dde3ea; border-radius: 8px; }
QLabel#kpiValue { font-size: 26px; font-weight: 600; color: #16263d; }
QLabel#kpiTitle { color: #5b6b80; font-weight: 600; }
QLabel#kpiDetail { color: #5b6b80; font-size: 11px; }
QLabel.titulo { font-size: 18px; font-weight: 600; color: #1f3a5f; }

/* tables and lists */
QTableView, QTableWidget, QTreeWidget, QTreeView, QListWidget {
    background: #ffffff; border: 1px solid #dde3ea; border-radius: 6px;
    gridline-color: #eef1f5; selection-background-color: #d6e8f8; selection-color: #10233a;
    alternate-background-color: #f7f9fb;
}
QHeaderView::section {
    background: #f5f7fa; color: #3b4b5f; padding: 6px 8px; border: none;
    border-bottom: 1px solid #dde3ea; border-right: 1px solid #eef1f5; font-weight: 600;
}
QTableView::item, QTableWidget::item { padding: 3px 6px; }

/* tabs */
QTabWidget::pane { border: none; background: #eef1f5; }
QTabBar::tab {
    background: transparent; color: #5b6b80; padding: 8px 16px; border: none;
    border-bottom: 2px solid transparent; margin-right: 2px;
}
QTabBar::tab:selected { color: #16263d; border-bottom: 2px solid #2b7bb9; background: #ffffff; }
QTabBar::tab:hover { color: #16263d; background: #f5f8fb; }
QTabWidget#workspace > QTabBar::tab { padding: 9px 18px; }

/* buttons and inputs */
QPushButton {
    background: #ffffff; border: 1px solid #c8d1dc; border-radius: 6px; padding: 6px 14px; color: #22303f;
}
QPushButton:hover { border-color: #2b7bb9; color: #1a5f94; }
QPushButton:disabled { color: #a3adba; border-color: #dde3ea; }
QPushButton#primary { background: #2b7bb9; color: #ffffff; border: 1px solid #2b7bb9; font-weight: 600; }
QPushButton#primary:hover { background: #236a9f; }
QPushButton#primary:disabled { background: #9fc3e0; border-color: #9fc3e0; }
QPushButton#danger { background: #ffffff; color: #c0392b; border: 1px solid #e5b3ad; }
QPushButton#danger:hover { background: #fbeeec; }
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QDateEdit, QPlainTextEdit, QTextEdit {
    background: #ffffff; border: 1px solid #c8d1dc; border-radius: 5px; padding: 4px 6px;
    selection-background-color: #2b7bb9;
}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus, QPlainTextEdit:focus { border-color: #2b7bb9; }
QLineEdit:read-only { background: #f5f7fa; }
QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled { color: #a3adba; background: #f5f7fa; border-color: #dde3ea; }
QCheckBox:disabled { color: #a3adba; }
QGroupBox { font-weight: 600; margin-top: 12px; border: 1px solid #dde3ea; border-radius: 6px; padding-top: 6px; background: #ffffff; }
QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 4px; color: #1f3a5f; }
QProgressBar { border: 1px solid #c8d1dc; border-radius: 5px; background: #ffffff; text-align: center; height: 14px; }
QProgressBar::chunk { background: #2b7bb9; border-radius: 4px; }
QSplitter::handle { background: #e2e7ee; }
QStatusBar { background: #16263d; color: #dfe7f1; }
QStatusBar QLabel { color: #dfe7f1; padding: 0 8px; }
QScrollArea { border: none; background: transparent; }
QScrollArea > QWidget > QWidget { background: transparent; }
QMenu { background: #ffffff; border: 1px solid #dde3ea; }
QMenu::item { padding: 6px 22px; }
QMenu::item:selected { background: #e8f1fb; color: #10233a; }
"""


def apply_theme(app: QApplication) -> None:
    app.setStyle("Fusion")
    font = QFont(app.font())
    font.setPointSizeF(max(font.pointSizeF(), 9.5))
    app.setFont(font)
    palette = app.palette()
    palette.setColor(QPalette.ColorRole.Window, QColor("#eef1f5"))
    palette.setColor(QPalette.ColorRole.Base, QColor("#ffffff"))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor("#f7f9fb"))
    palette.setColor(QPalette.ColorRole.Highlight, ACCENT)
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#ffffff"))
    palette.setColor(QPalette.ColorRole.Text, QColor(TEXT))
    palette.setColor(QPalette.ColorRole.WindowText, QColor(TEXT))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor(TEXT))
    app.setPalette(palette)
    app.setStyleSheet(STYLE_SHEET)


def severity_icon(widget: QWidget | None, severity: Severity | None) -> QIcon:
    name, color = {
        Severity.ERROR: ("error", ERROR),
        Severity.WARNING: ("aviso", WARNING),
        Severity.INFO: ("info", ACCENT),
        None: ("ok", OK),
    }[severity]
    return icon(name, color.name())


def status_icon(status: str) -> QIcon:
    name, color, _ = STATUS.get(status, STATUS["info"])
    return icon(name, color.name())


def status_color(status: str) -> QColor:
    return STATUS.get(status, STATUS["info"])[1]


def status_text(status: str) -> str:
    return STATUS.get(status, (None, None, status))[2]


def title_label(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("pageTitle")
    return label


class StatusBadge(QLabel):
    """Coloured pill: «Setup válido» / «ALARMA»…"""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setMinimumWidth(170)
        self.set_state("—", NEUTRAL)

    def set_state(self, text: str, color: QColor) -> None:
        self.setText(text)
        self.setStyleSheet(
            f"QLabel {{ background: {color.name()}; color: white; border-radius: 11px;"
            " padding: 4px 14px; font-weight: 600; }"
        )


def chip(text: str, color: QColor, parent: QWidget | None = None) -> QLabel:
    """Small rounded label, e.g. «SIMULACIÓN»."""
    label = QLabel(text, parent)
    label.setStyleSheet(
        f"QLabel {{ background: {color.name()}; color: white; border-radius: 8px; padding: 2px 9px;"
        " font-size: 11px; font-weight: 600; }"
    )
    return label


def separator() -> QFrame:
    line = QFrame()
    line.setFrameShape(QFrame.Shape.HLine)
    line.setStyleSheet("color: #dde3ea;")
    return line


class Card(QFrame):
    """White rounded panel with an optional title row (and widgets at its right)."""

    def __init__(self, title: str | None = None, parent: QWidget | None = None, icon_name: str | None = None):
        super().__init__(parent)
        self.setObjectName("card")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 12, 14, 12)
        outer.setSpacing(8)
        self.header = QHBoxLayout()
        self.header.setSpacing(8)
        if icon_name:
            mark = QLabel()
            mark.setPixmap(icon(icon_name, PRIMARY.name(), 18).pixmap(18, 18))
            self.header.addWidget(mark)
        self.title_label = QLabel(title or "")
        self.title_label.setObjectName("cardTitle")
        self.header.addWidget(self.title_label)
        self.header.addStretch(1)
        if title:
            outer.addLayout(self.header)
        self.body = QVBoxLayout()
        self.body.setSpacing(6)
        outer.addLayout(self.body)

    def set_title(self, text: str) -> None:
        self.title_label.setText(text)


class PageHeader(QWidget):
    def __init__(self, title: str, subtitle: str = "", parent: QWidget | None = None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 4)
        texts = QVBoxLayout()
        texts.setSpacing(2)
        self.title = QLabel(title)
        self.title.setObjectName("pageTitle")
        self.subtitle = QLabel(subtitle)
        self.subtitle.setObjectName("pageSubtitle")
        self.subtitle.setWordWrap(True)
        texts.addWidget(self.title)
        if subtitle:
            texts.addWidget(self.subtitle)
        layout.addLayout(texts, 1)
        self.right = QHBoxLayout()
        layout.addLayout(self.right)


class KpiTile(QFrame):
    def __init__(self, title: str, icon_name: str, color: QColor = ACCENT, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("kpi")
        self.setMinimumWidth(170)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 10)
        badge = QLabel()
        badge.setFixedSize(42, 42)
        badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        badge.setStyleSheet(f"background: {color.name()}; border-radius: 21px;")
        badge.setPixmap(icon(icon_name, "#ffffff", 22).pixmap(22, 22))
        texts = QVBoxLayout()
        texts.setSpacing(0)
        self.title = QLabel(title)
        self.title.setObjectName("kpiTitle")
        self.value = QLabel("—")
        self.value.setObjectName("kpiValue")
        self.detail = QLabel("")
        self.detail.setObjectName("kpiDetail")
        texts.addWidget(self.title)
        texts.addWidget(self.value)
        texts.addWidget(self.detail)
        layout.addWidget(badge, 0, Qt.AlignmentFlag.AlignTop)
        layout.addSpacing(6)
        layout.addLayout(texts, 1)

    def set_values(self, value: str, detail: str = "", detail_color: QColor | None = None) -> None:
        self.value.setText(value)
        self.detail.setText(detail)
        color = detail_color.name() if detail_color else MUTED
        self.detail.setStyleSheet(f"color: {color}; font-size: 11px;")


def make_table(headers: list[str], stretch_column: int | None = None) -> QTableWidget:
    table = QTableWidget(0, len(headers))
    table.setHorizontalHeaderLabels(headers)
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    table.setAlternatingRowColors(True)
    table.setShowGrid(False)
    table.verticalHeader().setVisible(False)
    table.verticalHeader().setDefaultSectionSize(28)
    header = table.horizontalHeader()
    header.setHighlightSections(False)
    header.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
    header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
    if stretch_column is not None:
        header.setSectionResizeMode(stretch_column, QHeaderView.ResizeMode.Stretch)
    else:
        header.setStretchLastSection(True)
    return table
