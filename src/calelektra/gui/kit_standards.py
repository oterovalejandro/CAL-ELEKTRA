"""The standards of the calibration kits in the GUI.

* ``KitStandardsEditor``: the standards of a kit model (in its dialog): import of a ``.ckt``, new,
  edit, duplicate, delete and export as ``.sdatb``.
* ``UnitStandardsEditor``: what a unit of the kit says of each standard (in the dialog of the
  equipment): serial number, state and its own data or specifications.
* ``StandardDialog`` / ``UnitStandardDialog``: a standard, defined by the model of Keysight (Agilent
  model: offset line, C0–C3, L0–L3) or by data (``.sdatb`` or ``.sNp``), with its electrical
  specifications (k = 2), and the view of its S-parameters with their uncertainty.
* ``StandardPreview``: that view, a chart and a table (U with k = 2).
"""

from __future__ import annotations

import math
from datetime import date
from pathlib import Path
from typing import Callable

import numpy as np
from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt, QTimer, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
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
    QStackedWidget,
    QTableView,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from calelektra.catalog.library import Library
from calelektra.core.connectors import GENDERS
from calelektra.core.frequency import FrequencyRange
from calelektra.core.units import UnitError, format_frequency
from calelektra.gui.frequency_input import FrequencyDelegate, install_frequency_completion
from calelektra.gui.icons import icon
from calelektra.gui.plots import TRACE_COLORS, ChartTrace, TraceChart
from calelektra.kits import (
    DEFINITION_NAMES,
    SPEC_COLUMNS,
    SPEC_NAMES,
    STATE_NAMES,
    TYPE_NAMES,
    CktKit,
    KitError,
    KitSettings,
    KitStandard,
    ResolvedStandard,
    SpecRow,
    StandardDefinition,
    UnitStandard,
    kit_standards,
    read_ckt,
    table_rows,
)
from calelektra.kits.model import TWO_PORT_TYPES, default_id, unique_id
from calelektra.sdata import SDataError, SParamData, load_sparam_data

COVERAGE = 2.0
CHART_MODES = {"lineal": "Magnitud (lineal)", "db": "Magnitud (dB)", "fase": "Fase (°)", "real": "Parte real", "imag": "Parte imaginaria"}
DATA_FILTER = "Parámetros S (*.sdatb *.s1p *.s2p *.S1P *.S2P *.ts)"


def _number(text: str, label: str, default: float | None = None) -> float | None:
    text = text.strip().replace(",", ".")
    if not text:
        return default
    try:
        return float(text)
    except ValueError as exc:
        raise ValueError(f"«{label}»: «{text}» no es un número") from exc


def _text(value: float | None) -> str:
    return "" if value is None else f"{value:.12g}"


def _date(text: str, label: str) -> date | None:
    text = text.strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"«{label}»: fecha no válida «{text}» (use AAAA-MM-DD)") from exc


def _g(value: float) -> str:
    return f"{value:g}".replace(".", ",")


def definition_text(definition: StandardDefinition) -> str:
    if definition.kind == "modelo":
        return f"Modelo: {_g(definition.offset_delay_ps)} ps, {_g(definition.offset_loss)} GΩ/s, {_g(definition.offset_z0)} Ω"
    if definition.kind == "datos":
        return f"Datos: {Path(definition.file or '').name}" + (f" (puertos {', '.join(map(str, definition.ports))})" if definition.ports else "")
    return DEFINITION_NAMES["ninguna"]


def specifications_text(kind: str, rows: list[SpecRow]) -> str:
    if not rows:
        return "—"
    parts = []
    for column in SPEC_COLUMNS[kind]:
        values = [row.value(column) for row in rows if row.value(column) is not None]
        if values:
            unit = "°" if column.endswith("grados") else " dB"
            low, high = min(values), max(values)
            short = {"modulo_db": "mód.", "fase_grados": "fase", "perdidas_retorno_db": "RL", "transmision_modulo_db": "trans. mód.", "transmision_fase_grados": "trans. fase"}[column]
            parts.append(f"{short} {_g(low)}{unit}" if low == high else f"{short} {_g(low)}…{_g(high)}{unit}")
    return ", ".join(parts) + f" ({len(rows)} fila{'s' if len(rows) > 1 else ''})" if parts else "—"


# ── the view of the S-parameters of a standard ─────────────────────────────────────────────────────────


class _TableModel(QAbstractTableModel):
    HEADERS = ["Frecuencia", "Parámetro", "Re", "Im", "|S|", "∠S (°)", "U Re", "U Im", "U |S|", "U ∠ (°)"]

    def __init__(self) -> None:
        super().__init__()
        self.rows: list = []

    def set_rows(self, rows: list) -> None:
        self.beginResetModel()
        self.rows = rows
        self.endResetModel()

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802 (Qt API)
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802 (Qt API)
        return len(self.HEADERS)

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole):  # noqa: N802 (Qt API)
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return self.HEADERS[section]
        return None

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.TextAlignmentRole and index.column() > 1:
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        return self.cell(self.rows[index.row()], index.column())

    @staticmethod
    def cell(row, column: int, decimal: str = ",") -> str:
        value = row.value
        texts = [
            format_frequency(row.frequency, decimal=decimal),
            row.parameter,
            f"{value.real:.6f}",
            f"{value.imag:.6f}",
            f"{abs(value):.6f}",
            f"{math.degrees(math.atan2(value.imag, value.real)):.3f}" if abs(value) > 0 else "—",
            f"{row.u_real:.3g}",
            f"{row.u_imag:.3g}",
            f"{row.u_magnitude:.3g}",
            "—" if math.isnan(row.u_phase_deg) else f"{row.u_phase_deg:.3g}",
        ]
        text = texts[column]
        return text.replace(".", decimal) if column > 1 else text


class StandardPreview(QWidget):
    """S-parameters of a standard with their uncertainty: a chart and a table (U with k = 2)."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.data: SParamData | None = None
        self.rows: list = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.info = QLabel()
        self.info.setWordWrap(True)
        self.info.setObjectName("pageSubtitle")
        layout.addWidget(self.info)
        self.tabs = QTabWidget()
        chart_page = QWidget()
        chart_layout = QVBoxLayout(chart_page)
        chart_layout.setContentsMargins(0, 4, 0, 0)
        controls = QHBoxLayout()
        self.mode = QComboBox()
        for key, text in CHART_MODES.items():
            self.mode.addItem(text, key)
        self.mode.currentIndexChanged.connect(lambda _i: self.redraw())
        self.bands = QCheckBox(f"Incertidumbre (k={COVERAGE:g})")
        self.bands.setChecked(True)
        self.bands.toggled.connect(lambda _on: self.redraw())
        self.log_x = QCheckBox("Frecuencia logarítmica")
        self.log_x.toggled.connect(lambda _on: self.redraw())
        controls.addWidget(self.mode)
        controls.addWidget(self.bands)
        controls.addWidget(self.log_x)
        controls.addStretch(1)
        chart_layout.addLayout(controls)
        self.chart = TraceChart()
        chart_layout.addWidget(self.chart, 1)
        self.tabs.addTab(chart_page, icon("grafica"), "Gráfica")
        table_page = QWidget()
        table_layout = QVBoxLayout(table_page)
        table_layout.setContentsMargins(0, 4, 0, 0)
        self.table_model = _TableModel()
        self.table = QTableView()
        self.table.setModel(self.table_model)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        table_layout.addWidget(self.table, 1)
        copy = QPushButton("Copiar tabla")
        copy.setToolTip("Copia la tabla al portapapeles (separada por tabuladores, con punto decimal)")
        copy.clicked.connect(self.copy_table)
        row = QHBoxLayout()
        row.addWidget(QLabel(f"U: incertidumbre expandida, k = {COVERAGE:g}"))
        row.addStretch(1)
        row.addWidget(copy)
        table_layout.addLayout(row)
        self.tabs.addTab(table_page, icon("tabla"), "Tabla")
        layout.addWidget(self.tabs, 1)
        self.show_message("Sin datos")

    def show_message(self, text: str) -> None:
        self.data, self.rows = None, []
        self.info.setText(text)
        self.table_model.set_rows([])
        self.chart.draw("", "", [], [], False)

    def show_data(self, data: SParamData, title: str = "") -> None:
        self.data = data
        self.rows = table_rows(data, COVERAGE)
        self.table_model.set_rows(self.rows)
        f = data.frequencies
        zr = ", ".join(f"{z.real:g}" + (f"{z.imag:+g}j" if z.imag else "") + " Ω" for z in data.zr)
        inputs = len(data.vector.inputs)
        budget = f"{inputs} entrada{'s' if inputs != 1 else ''} en el presupuesto" if inputs else "sin incertidumbre"
        self.info.setText(f"{title + ' · ' if title else ''}{data.n_points} puntos, {format_frequency(f[0])} – {format_frequency(f[-1])} · Zr {zr} · {budget}")
        self.redraw()

    def _arrays(self) -> dict[str, dict[str, np.ndarray]]:
        result: dict[str, dict[str, list]] = {}
        for row in self.rows:
            item = result.setdefault(row.parameter, {"f": [], "v": [], "ur": [], "ui": [], "um": [], "up": []})
            item["f"].append(row.frequency)
            item["v"].append(row.value)
            item["ur"].append(row.u_real)
            item["ui"].append(row.u_imag)
            item["um"].append(row.u_magnitude)
            item["up"].append(row.u_phase_deg)
        return {key: {name: np.asarray(values) for name, values in item.items()} for key, item in result.items()}

    def redraw(self) -> None:
        if self.data is None:
            return
        mode = self.mode.currentData()
        axis = CHART_MODES[mode]
        traces = []
        for parameter, a in self._arrays().items():
            v, magnitude = a["v"], np.abs(a["v"])
            if mode == "lineal":
                y, low, high = magnitude, np.maximum(magnitude - a["um"], 0), magnitude + a["um"]
            elif mode == "db":
                with np.errstate(divide="ignore", invalid="ignore"):
                    y = np.where(magnitude > 0, 20 * np.log10(magnitude), np.nan)
                    low = 20 * np.log10(np.maximum(magnitude - a["um"], 1e-12))
                    high = 20 * np.log10(magnitude + a["um"])
            elif mode == "fase":
                y = np.where(magnitude > 0, np.degrees(np.angle(v)), np.nan)
                low, high = y - a["up"], y + a["up"]
            elif mode == "real":
                y, low, high = v.real, v.real - a["ur"], v.real + a["ur"]
            else:
                y, low, high = v.imag, v.imag - a["ui"], v.imag + a["ui"]
            band = (low, high) if self.bands.isChecked() and np.any(np.nan_to_num(high - low) > 0) else None
            traces.append(ChartTrace(parameter, a["f"], y, band, TRACE_COLORS.get(parameter, "#555"), f"{parameter}: {axis}"))
        log_x = self.log_x.isChecked() and bool(len(self.data.frequencies)) and self.data.frequencies[0] > 0
        self.chart.draw("", axis, traces, [], log_x, wrap=mode == "fase")

    def table_text(self) -> str:
        lines = ["\t".join(_TableModel.HEADERS)]
        lines += ["\t".join(_TableModel.cell(row, column, decimal=".") for column in range(len(_TableModel.HEADERS))) for row in self.rows]
        return "\n".join(lines)

    def copy_table(self) -> None:
        QGuiApplication.clipboard().setText(self.table_text())


# ── definition and specifications of a standard ────────────────────────────────────────────────────────


class DefinitionEditor(QWidget):
    """How a standard is defined: model of Keysight, data of a file or nothing (or, in a unit, that of the model)."""

    changed = Signal()

    def __init__(self, library: Library, kit_id: Callable[[], str], folder: Callable[[], Path], inherit: bool = False, parent: QWidget | None = None):
        super().__init__(parent)
        self.library, self.kit_id, self.folder = library, kit_id, folder
        self.inherit = inherit
        self.kind_type = "abierto"
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.kind = QComboBox()
        if inherit:
            self.kind.addItem("La del modelo del kit", "")
        for key, text in (("modelo", "Modelo (Agilent): línea offset y C0–C3 / L0–L3"), ("datos", "Datos: fichero .sdatb o .sNp"), ("ninguna", "Sin definición")):
            self.kind.addItem(text, key)
        self.kind.currentIndexChanged.connect(self._kind_changed)
        layout.addWidget(self.kind)
        self.stack = QStackedWidget()
        layout.addWidget(self.stack)
        self.stack.addWidget(QLabel("Se usa la definición del modelo del kit."))
        # model
        model_page = QWidget()
        self.model_form = QFormLayout(model_page)
        self.z0, self.delay, self.loss = QLineEdit("50"), QLineEdit("0"), QLineEdit("0")
        self.model_form.addRow("Offset Z0 (Ω)", self.z0)
        self.model_form.addRow("Offset Delay (ps)", self.delay)
        self.model_form.addRow("Offset Loss (GΩ/s)", self.loss)
        units_c = ("E-15 F", "E-27 F/Hz", "E-36 F/Hz²", "E-45 F/Hz³")
        units_l = ("E-12 H", "E-24 H/Hz", "E-33 H/Hz²", "E-42 H/Hz³")
        self.c = [QLineEdit("0") for _ in range(4)]
        self.l = [QLineEdit("0") for _ in range(4)]
        for k, (edit, unit) in enumerate(zip(self.c, units_c)):
            self.model_form.addRow(f"C{k} ({unit})", edit)
        for k, (edit, unit) in enumerate(zip(self.l, units_l)):
            self.model_form.addRow(f"L{k} ({unit})", edit)
        self.load_r, self.load_x = QLineEdit(), QLineEdit()
        self.load_r.setPlaceholderText("la del offset (Z0)")
        self.load_x.setPlaceholderText("0")
        self.model_form.addRow("Carga: R (Ω)", self.load_r)
        self.model_form.addRow("Carga: X (Ω)", self.load_x)
        self.stack.addWidget(model_page)
        # data
        data_page = QWidget()
        data_form = QFormLayout(data_page)
        self.file = QLineEdit()
        self.file.setReadOnly(True)
        self.file.setPlaceholderText("Fichero .sdatb (con su incertidumbre) o Touchstone .sNp")
        browse = QPushButton("Examinar…")
        browse.clicked.connect(self._browse)
        row = QHBoxLayout()
        row.addWidget(self.file, 1)
        row.addWidget(browse)
        self.ports = QLineEdit()
        self.ports.setPlaceholderText("todos (p. ej. 1, o 1, 2)")
        self.reference = QLineEdit()
        self.reference.setPlaceholderText("Certificado o laboratorio del que proceden los datos")
        self.data_date = QLineEdit()
        self.data_date.setPlaceholderText("AAAA-MM-DD")
        self.data_info = QLabel()
        self.data_info.setWordWrap(True)
        self.data_info.setStyleSheet("color: #5b6b80;")
        self.sha256: str | None = None
        data_form.addRow("Fichero", row)
        data_form.addRow("Puertos que se usan", self.ports)
        data_form.addRow("Referencia", self.reference)
        data_form.addRow("Fecha de los datos", self.data_date)
        data_form.addRow(self.data_info)
        self.stack.addWidget(data_page)
        self.stack.addWidget(QLabel("Sin definición: un adaptador que hace de through desconocido, o un patrón pendiente de definir."))
        for edit in [self.z0, self.delay, self.loss, *self.c, *self.l, self.load_r, self.load_x, self.ports]:
            edit.textEdited.connect(lambda _t: self.changed.emit())
        self._kind_changed()

    def _pages(self) -> dict[str, int]:
        return {"": 0, "modelo": 1, "datos": 2, "ninguna": 3}

    def _kind_changed(self) -> None:
        self.stack.setCurrentIndex(self._pages()[self.kind.currentData()])
        self._update_rows()
        self.changed.emit()

    def _update_rows(self) -> None:
        kind = self.kind_type
        for edit in self.c:
            self.model_form.setRowVisible(edit, kind == "abierto")
        for edit in self.l:
            self.model_form.setRowVisible(edit, kind == "cortocircuito")
        for edit in (self.load_r, self.load_x):
            self.model_form.setRowVisible(edit, kind == "carga")
        self.changed.emit()

    def set_type(self, kind: str) -> None:
        self.kind_type = kind
        index = self.kind.findData("modelo")
        model_item = self.kind.model().item(index)
        model_item.setEnabled(kind != "adaptador")
        if kind == "adaptador" and self.kind.currentData() == "modelo":
            self.kind.setCurrentIndex(self.kind.findData("ninguna"))
        self._update_rows()

    def _browse(self) -> None:
        filename, _ = QFileDialog.getOpenFileName(self, "Datos del patrón", str(self.library.root), DATA_FILTER)
        if filename:
            self.use_file(Path(filename))

    def use_file(self, source: Path) -> None:
        """Copy a data file into the library (``catalogo/patrones/<kit>/``) and use it."""
        try:
            data = load_sparam_data(source)
        except SDataError as exc:
            QMessageBox.warning(self, "Datos del patrón", f"El fichero no es válido: {exc}")
            return
        relative, digest = self.library.copy_standard_file(source, self.kit_id() or "kit")
        self.file.setText(relative)
        self.sha256 = digest
        self._describe(data)
        if not self.data_date.text():
            self.data_date.setText(date.today().isoformat())
        self.changed.emit()

    def _describe(self, data: SParamData) -> None:
        f = data.frequencies
        own = f"{len(data.vector.inputs)} entradas de incertidumbre" if data.vector.inputs else "sin incertidumbre: se añaden las especificaciones eléctricas"
        ports = f"{data.n_ports} puerto{'s' if data.n_ports > 1 else ''} ({', '.join(p.label for p in data.ports)})"
        self.data_info.setText(f"{ports} · {data.n_points} puntos, {format_frequency(f[0])} – {format_frequency(f[-1])} · {own}")

    def load(self, definition: StandardDefinition | None) -> None:
        if definition is None:
            self.kind.setCurrentIndex(max(self.kind.findData(""), 0))
            return
        self.kind.setCurrentIndex(max(self.kind.findData(definition.kind), 0))
        self.z0.setText(_text(definition.offset_z0))
        self.delay.setText(_text(definition.offset_delay_ps))
        self.loss.setText(_text(definition.offset_loss))
        for edit, value in zip(self.c, definition.capacitance):
            edit.setText(_text(value))
        for edit, value in zip(self.l, definition.inductance):
            edit.setText(_text(value))
        r, x = definition.load_impedance or (None, None)
        self.load_r.setText(_text(r))
        self.load_x.setText(_text(x))
        self.file.setText(definition.file or "")
        self.ports.setText(", ".join(map(str, definition.ports)))
        self.reference.setText(definition.reference or "")
        self.data_date.setText(definition.data_date.isoformat() if definition.data_date else "")
        self.sha256 = definition.sha256
        self.data_info.clear()
        if definition.kind == "datos" and definition.file:
            path = Path(definition.file) if Path(definition.file).is_absolute() else (self.folder() / definition.file).resolve()
            try:
                self._describe(load_sparam_data(path))
            except SDataError as exc:
                self.data_info.setText(f"✖ {exc}")

    def definition(self) -> StandardDefinition | None:
        kind = self.kind.currentData()
        if kind == "":
            return None
        if kind == "ninguna":
            return StandardDefinition(kind="ninguna")
        if kind == "datos":
            if not self.file.text().strip():
                raise ValueError("Elija el fichero de datos del patrón (.sdatb o .sNp)")
            try:
                ports = [int(p) for p in self.ports.text().replace(";", ",").split(",") if p.strip()]
            except ValueError as exc:
                raise ValueError(f"«Puertos que se usan»: «{self.ports.text()}» no es una lista de números") from exc
            return StandardDefinition(
                kind="datos",
                file=self.file.text().strip(),
                ports=ports,
                reference=self.reference.text().strip() or None,
                data_date=_date(self.data_date.text(), "Fecha de los datos"),
                sha256=self.sha256,
            )
        load = None
        r = _number(self.load_r.text(), "Carga: R")
        x = _number(self.load_x.text(), "Carga: X", 0.0)
        if r is not None or x:
            load = [r if r is not None else _number(self.z0.text(), "Offset Z0", 50.0), x]
        return StandardDefinition(
            kind="modelo",
            offset_z0=_number(self.z0.text(), "Offset Z0", 50.0),
            offset_delay_ps=_number(self.delay.text(), "Offset Delay", 0.0),
            offset_loss=_number(self.loss.text(), "Offset Loss", 0.0),
            capacitance=[_number(e.text(), f"C{k}", 0.0) for k, e in enumerate(self.c)],
            inductance=[_number(e.text(), f"L{k}", 0.0) for k, e in enumerate(self.l)],
            load_impedance=load,
        )


class SpecsEditor(QWidget):
    """Electrical specifications of a standard: a row per frequency, k = 2 (columns of its type)."""

    changed = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.kind = "abierto"
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.note = QLabel()
        self.note.setWordWrap(True)
        self.note.setStyleSheet("color: #5b6b80;")
        layout.addWidget(self.note)
        self.table = QTableWidget(0, 1)
        self.table.setItemDelegateForColumn(0, FrequencyDelegate(self.table))
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.itemChanged.connect(lambda _item: self.changed.emit())
        self.table.setMinimumHeight(110)
        layout.addWidget(self.table)
        buttons = QHBoxLayout()
        add = QPushButton("Añadir fila")
        add.clicked.connect(self.add_row)
        remove = QPushButton("Quitar fila")
        remove.clicked.connect(self.remove_row)
        buttons.addWidget(add)
        buttons.addWidget(remove)
        buttons.addStretch(1)
        layout.addLayout(buttons)
        self.set_type("abierto")

    def _columns(self) -> tuple[str, ...]:
        return SPEC_COLUMNS[self.kind]

    def set_type(self, kind: str) -> None:
        try:
            rows = self.rows()
        except ValueError:
            rows = []
        self.kind = kind
        self.table.blockSignals(True)
        self.table.setColumnCount(1 + len(self._columns()))
        self.table.setHorizontalHeaderLabels(["Frecuencia"] + [SPEC_NAMES[c] for c in self._columns()])
        self.table.blockSignals(False)
        self.load(rows)
        notes = {
            "abierto": "Desviaciones de módulo (dB) y de fase (°) respecto a la definición, con k = 2.",
            "cortocircuito": "Desviaciones de módulo (dB) y de fase (°) respecto a la definición, con k = 2.",
            "carga": "Pérdidas de retorno con k = 2: −40 dB es una incertidumbre de 0,01 en la parte real y en la imaginaria de Γ.",
            "through": "Pérdidas de retorno de las reflexiones y desviaciones de módulo (dB) y fase (°) de la transmisión, con k = 2.",
            "adaptador": "Un adaptador (through desconocido) no tiene especificaciones eléctricas.",
        }
        self.note.setText(notes[kind] + (" Entre filas se interpola linealmente; fuera de la tabla vale la fila más cercana." if kind != "adaptador" else ""))
        self.setEnabled(kind != "adaptador")

    def add_row(self) -> None:
        self.table.insertRow(self.table.rowCount())
        self.changed.emit()

    def remove_row(self) -> None:
        rows = sorted({index.row() for index in self.table.selectedIndexes()}, reverse=True) or ([self.table.rowCount() - 1] if self.table.rowCount() else [])
        for row in rows:
            self.table.removeRow(row)
        self.changed.emit()

    def load(self, rows: list[SpecRow]) -> None:
        self.table.blockSignals(True)
        self.table.setRowCount(0)
        for spec in rows:
            row = self.table.rowCount()
            self.table.insertRow(row)
            self.table.setItem(row, 0, QTableWidgetItem(format_frequency(spec.frequency)))
            for column, key in enumerate(self._columns(), start=1):
                value = spec.value(key)
                self.table.setItem(row, column, QTableWidgetItem("" if value is None else f"{value:g}".replace(".", ",")))
        self.table.blockSignals(False)

    def rows(self) -> list[SpecRow]:
        result = []
        for row in range(self.table.rowCount()):
            texts = [(self.table.item(row, column).text().strip() if self.table.item(row, column) else "") for column in range(self.table.columnCount())]
            if not any(texts):
                continue
            values = {}
            for key, text in zip(self._columns(), texts[1:]):
                if text:
                    values[key] = _number(text, SPEC_NAMES[key])
            if not values:
                continue
            try:
                result.append(SpecRow.model_validate({"frecuencia": texts[0] or "0 Hz", **values}))
            except ValueError as exc:
                raise ValueError(f"Especificaciones, fila {row + 1}: {exc}") from exc
        return result


# ── the dialogs of a standard ──────────────────────────────────────────────────────────────────────────


class _PreviewHolder:
    """Recompute the view of the standard shortly after a change."""

    def _setup_preview(self, preview: StandardPreview) -> None:
        self.preview = preview
        self.timer = QTimer(self)  # type: ignore[arg-type]
        self.timer.setSingleShot(True)
        self.timer.setInterval(350)
        self.timer.timeout.connect(self.refresh)

    def schedule(self) -> None:
        self.timer.start()

    def refresh(self) -> None:
        raise NotImplementedError


class StandardDialog(QDialog, _PreviewHolder):
    """A standard of a kit model: identity, definition, electrical specifications and its view."""

    def __init__(
        self,
        library: Library,
        standard: KitStandard | None,
        taken: set[str],
        settings: Callable[[], KitSettings],
        kit_id: Callable[[], str],
        folder: Callable[[], Path],
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.library, self.taken, self.settings, self.folder = library, taken, settings, folder
        self.result_standard: KitStandard | None = None
        self.setWindowTitle(f"Patrón «{standard.label or standard.id}»" if standard else "Nuevo patrón")
        self.resize(1180, 760)
        layout = QVBoxLayout(self)
        splitter = QSplitter()
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        identity = QGroupBox("Identificación del patrón")
        form = QFormLayout(identity)
        self.id_edit = QLineEdit()
        self.id_edit.setPlaceholderText("p. ej. abierto_m (se propone al elegir el tipo)")
        self.kind = QComboBox()
        for key, text in TYPE_NAMES.items():
            self.kind.addItem(text, key)
        self.label = QLineEdit()
        self.label.setPlaceholderText("Como lo nombra el VNA o el .ckt, p. ej. OPEN -M-")
        self.description = QLineEdit()
        self.gender1, self.gender2 = QComboBox(), QComboBox()
        for combo in (self.gender1, self.gender2):
            for key, text in GENDERS.items():
                combo.addItem(text, key)
        genders = QHBoxLayout()
        genders.addWidget(self.gender1)
        self.gender_dash = QLabel("–")
        genders.addWidget(self.gender_dash)
        genders.addWidget(self.gender2)
        self.range_low, self.range_high = QLineEdit(), QLineEdit()
        for edit, text in ((self.range_low, "inicio (el del kit)"), (self.range_high, "fin (el del kit)")):
            edit.setPlaceholderText(text)
            install_frequency_completion(edit)
            edit.textEdited.connect(lambda _t: self.schedule())
        span = QHBoxLayout()
        span.addWidget(self.range_low)
        span.addWidget(QLabel("–"))
        span.addWidget(self.range_high)
        form.addRow("Identificador *", self.id_edit)
        form.addRow("Tipo", self.kind)
        form.addRow("Etiqueta", self.label)
        form.addRow("Descripción", self.description)
        form.addRow("Género", genders)
        form.addRow("Rango propio", span)
        left_layout.addWidget(identity)
        definition_box = QGroupBox("Definición")
        definition_layout = QVBoxLayout(definition_box)
        self.definition = DefinitionEditor(library, kit_id, folder)
        definition_layout.addWidget(self.definition)
        left_layout.addWidget(definition_box)
        specs_box = QGroupBox("Especificaciones eléctricas (k = 2)")
        specs_layout = QVBoxLayout(specs_box)
        self.specs = SpecsEditor()
        specs_layout.addWidget(self.specs)
        left_layout.addWidget(specs_box, 1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(left)
        splitter.addWidget(scroll)
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.addWidget(QLabel("<b>Parámetros S del patrón con su incertidumbre</b>"))
        self._setup_preview(StandardPreview())
        right_layout.addWidget(self.preview, 1)
        splitter.addWidget(right)
        splitter.setSizes([600, 580])
        layout.addWidget(splitter, 1)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("Cancelar")
        cancel.clicked.connect(self.reject)
        accept = QPushButton("Aceptar")
        accept.setObjectName("primary")
        accept.clicked.connect(self._accept)
        buttons.addWidget(cancel)
        buttons.addWidget(accept)
        layout.addLayout(buttons)
        self.kind.currentIndexChanged.connect(self._kind_changed)
        for combo in (self.gender1, self.gender2):
            combo.currentIndexChanged.connect(lambda _i: self._suggest_id())
        self.definition.changed.connect(self.schedule)
        self.specs.changed.connect(self.schedule)
        self.original_id = standard.id if standard else None
        if standard is not None:
            self.load(standard)
        else:
            self._kind_changed()
        self.refresh()

    def _kind_changed(self) -> None:
        kind = self.kind.currentData()
        two = kind in TWO_PORT_TYPES
        self.gender2.setVisible(two)
        self.gender_dash.setVisible(two)
        self.definition.set_type(kind)
        self.specs.set_type(kind)
        self._suggest_id()
        self.schedule()

    def _genders(self) -> list[str]:
        genders = [self.gender1.currentData()]
        if self.kind.currentData() in TWO_PORT_TYPES:
            genders.append(self.gender2.currentData())
        return genders

    def _suggest_id(self) -> None:
        if self.original_id is None and (not self.id_edit.text().strip() or self.id_edit.property("suggested")):
            self.id_edit.setText(default_id(self.kind.currentData(), self._genders(), self.taken))
            self.id_edit.setProperty("suggested", True)

    def load(self, standard: KitStandard) -> None:
        self.id_edit.setText(standard.id)
        self.kind.blockSignals(True)
        self.kind.setCurrentIndex(max(self.kind.findData(standard.type), 0))
        self.kind.blockSignals(False)
        self.gender1.setCurrentIndex(max(self.gender1.findData(standard.genders[0]), 0))
        if len(standard.genders) > 1:
            self.gender2.setCurrentIndex(max(self.gender2.findData(standard.genders[1]), 0))
        self._kind_changed()
        self.label.setText(standard.label)
        self.description.setText(standard.description or "")
        if standard.frequency_range is not None:
            self.range_low.setText(format_frequency(standard.frequency_range.start) if standard.frequency_range.start else "DC")
            self.range_high.setText(format_frequency(standard.frequency_range.stop))
        self.definition.load(standard.definition)
        self.specs.load(standard.specifications)

    def build(self) -> KitStandard:
        identifier = self.id_edit.text().strip()
        if not identifier:
            raise ValueError("Indique el identificador del patrón")
        if identifier != self.original_id and identifier in self.taken:
            raise ValueError(f"Ya hay un patrón «{identifier}» en el kit")
        frequency_range = None
        if self.range_low.text().strip() or self.range_high.text().strip():
            try:
                frequency_range = FrequencyRange.parse([self.range_low.text() or "DC", self.range_high.text()])
            except (UnitError, ValueError) as exc:
                raise ValueError(f"«Rango propio»: {exc}") from exc
        return KitStandard(
            id=identifier,
            type=self.kind.currentData(),
            label=self.label.text().strip(),
            description=self.description.text().strip() or None,
            genders=self._genders(),
            frequency_range=frequency_range,
            definition=self.definition.definition() or StandardDefinition(),
            specifications=self.specs.rows(),
        )

    def refresh(self) -> None:
        try:
            standard = self.build()
        except ValueError as exc:
            self.preview.show_message(f"✖ {exc}")
            return
        if standard.definition.kind == "ninguna":
            self.preview.show_message("Sin definición: no hay parámetros S que mostrar")
            return
        settings = self.settings()
        try:
            data = self.library.standard_data(ResolvedStandard(standard, None, self.folder()), settings)
        except (KitError, SDataError, ValueError) as exc:
            self.preview.show_message(f"✖ {exc}")
            return
        self.preview.show_data(data, ResolvedStandard(standard, None, self.folder()).name(settings.name))

    def _accept(self) -> None:
        try:
            self.result_standard = self.build()
        except ValueError as exc:
            QMessageBox.warning(self, self.windowTitle(), str(exc))
            return
        self.accept()


class UnitStandardDialog(QDialog, _PreviewHolder):
    """A standard of a unit of the kit: serial number, state and, if its certificate gives them, its own data."""

    def __init__(
        self,
        library: Library,
        standard: KitStandard,
        unit: UnitStandard | None,
        settings: KitSettings,
        kit_id: str,
        model_folder: Path,
        unit_folder: Path,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.library, self.standard, self.settings = library, standard, settings
        self.model_folder, self.unit_folder = model_folder, unit_folder
        self.result_unit: UnitStandard | None = None
        self.setWindowTitle(f"Patrón «{standard.label or standard.id}» de la unidad")
        self.resize(1160, 720)
        layout = QVBoxLayout(self)
        splitter = QSplitter()
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        summary = QLabel(
            f"<b>{TYPE_NAMES[standard.type]}</b> {standard.gender_text} · {standard.label or standard.id}"
            + (f"<br>{standard.description}" if standard.description else "")
            + f"<br>Del modelo: {definition_text(standard.definition)}; especificaciones: {specifications_text(standard.type, standard.specifications)}"
        )
        summary.setWordWrap(True)
        left_layout.addWidget(summary)
        identity = QGroupBox("Este patrón")
        form = QFormLayout(identity)
        self.serial = QLineEdit()
        self.state = QComboBox()
        for key, text in STATE_NAMES.items():
            self.state.addItem(text, key)
        self.state_date = QLineEdit()
        self.state_date.setPlaceholderText("AAAA-MM-DD (cuándo se dañó o se sustituyó)")
        self.notes = QLineEdit()
        form.addRow("Nº de serie", self.serial)
        form.addRow("Estado", self.state)
        form.addRow("Fecha del estado", self.state_date)
        form.addRow("Notas", self.notes)
        left_layout.addWidget(identity)
        definition_box = QGroupBox("Definición propia (p. ej. los datos de su certificado)")
        definition_layout = QVBoxLayout(definition_box)
        self.definition = DefinitionEditor(library, lambda: kit_id, lambda: unit_folder, inherit=True)
        self.definition.set_type(standard.type)
        definition_layout.addWidget(self.definition)
        left_layout.addWidget(definition_box)
        specs_box = QGroupBox("Especificaciones eléctricas (k = 2)")
        specs_layout = QVBoxLayout(specs_box)
        self.own_specs = QCheckBox("Especificaciones propias (si no, las del modelo)")
        specs_layout.addWidget(self.own_specs)
        self.specs = SpecsEditor()
        self.specs.set_type(standard.type)
        specs_layout.addWidget(self.specs)
        left_layout.addWidget(specs_box, 1)
        splitter.addWidget(left)
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.addWidget(QLabel("<b>Parámetros S del patrón con su incertidumbre</b>"))
        self._setup_preview(StandardPreview())
        right_layout.addWidget(self.preview, 1)
        splitter.addWidget(right)
        splitter.setSizes([500, 660])
        layout.addWidget(splitter, 1)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("Cancelar")
        cancel.clicked.connect(self.reject)
        accept = QPushButton("Aceptar")
        accept.setObjectName("primary")
        accept.clicked.connect(self._accept)
        buttons.addWidget(cancel)
        buttons.addWidget(accept)
        layout.addLayout(buttons)
        if unit is not None:
            self.serial.setText(unit.serial_number or "")
            self.state.setCurrentIndex(max(self.state.findData(unit.state), 0))
            self.state_date.setText(unit.state_date.isoformat() if unit.state_date else "")
            self.notes.setText(unit.notes or "")
            self.definition.load(unit.definition)
            if unit.specifications is not None:
                self.own_specs.setChecked(True)
                self.specs.load(unit.specifications)
        if not self.own_specs.isChecked():
            self.specs.load(standard.specifications)
        self.specs.setEnabled(self.own_specs.isChecked() and standard.type != "adaptador")
        self.own_specs.toggled.connect(lambda on: (self.specs.setEnabled(on and standard.type != "adaptador"), self.schedule()))
        self.definition.changed.connect(self.schedule)
        self.specs.changed.connect(self.schedule)
        self.refresh()

    def build(self) -> UnitStandard:
        return UnitStandard(
            numero_serie=self.serial.text().strip() or None,
            estado=self.state.currentData(),
            fecha_estado=_date(self.state_date.text(), "Fecha del estado"),
            definicion=self.definition.definition(),
            especificaciones=self.specs.rows() if self.own_specs.isChecked() else None,
            notas=self.notes.text().strip() or None,
        )

    def refresh(self) -> None:
        try:
            unit = self.build()
        except ValueError as exc:
            self.preview.show_message(f"✖ {exc}")
            return
        resolved = ResolvedStandard(self.standard, unit, self.model_folder, self.unit_folder)
        if resolved.definition.kind == "ninguna":
            self.preview.show_message("Sin definición: no hay parámetros S que mostrar")
            return
        try:
            data = self.library.standard_data(resolved, self.settings)
        except (KitError, SDataError, ValueError) as exc:
            self.preview.show_message(f"✖ {exc}")
            return
        origin = "datos propios" if resolved.own_definition else "definición del modelo"
        self.preview.show_data(data, f"{resolved.name(self.settings.name)} ({origin})")

    def _accept(self) -> None:
        try:
            self.result_unit = self.build()
        except ValueError as exc:
            QMessageBox.warning(self, self.windowTitle(), str(exc))
            return
        self.accept()


# ── the tables of the standards ────────────────────────────────────────────────────────────────────────


def _table(headers: list[str]) -> QTableWidget:
    table = QTableWidget(0, len(headers))
    table.setHorizontalHeaderLabels(headers)
    table.verticalHeader().setVisible(False)
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
    table.horizontalHeader().setStretchLastSection(True)
    table.setMinimumHeight(220)
    return table


def _button(text: str, icon_name: str, tip: str) -> QPushButton:
    button = QPushButton(icon(icon_name), text)
    button.setToolTip(tip)
    return button


class KitStandardsEditor(QWidget):
    """The standards of a kit model."""

    changed = Signal()
    ckt_imported = Signal(object, object)  # CktKit, path of the .ckt

    HEADERS = ["Patrón", "Tipo", "Género", "Definición", "Especificaciones (k = 2)", "Rango propio"]

    def __init__(self, library: Library, kit_id: Callable[[], str], settings: Callable[[], KitSettings], folder: Callable[[], Path], parent: QWidget | None = None):
        super().__init__(parent)
        self.library, self.kit_id, self.settings, self.folder = library, kit_id, settings, folder
        self.items: list[KitStandard] = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        buttons = QHBoxLayout()
        self.import_button = _button("Importar .ckt…", "importar", "Patrones de un fichero de kit de Keysight (.ckt)")
        self.import_button.clicked.connect(self.import_ckt)
        new = _button("Nuevo…", "nuevo", "Definir un patrón a mano: por el modelo, por datos o sin definición")
        new.clicked.connect(self.new_standard)
        edit = _button("Editar…", "editar", "Definición, especificaciones y vista del patrón (también con doble clic)")
        edit.clicked.connect(self.edit_standard)
        duplicate = _button("Duplicar", "duplicar", "Copia del patrón elegido")
        duplicate.clicked.connect(self.duplicate_standard)
        remove = _button("Eliminar", "eliminar", "Quitar el patrón del kit")
        remove.clicked.connect(self.remove_standard)
        export = _button("Exportar .sdatb…", "exportar", "Un .sdatb por patrón, con su incertidumbre, para METAS VNA Tools")
        export.clicked.connect(self.export)
        for button in (self.import_button, new, edit, duplicate, remove):
            buttons.addWidget(button)
        buttons.addStretch(1)
        buttons.addWidget(export)
        layout.addLayout(buttons)
        self.table = _table(self.HEADERS)
        self.table.doubleClicked.connect(lambda _i: self.edit_standard())
        layout.addWidget(self.table)

    def load(self, standards: list[KitStandard]) -> None:
        self.items = [standard.model_copy(deep=True) for standard in standards]
        self._fill()

    def standards(self) -> list[KitStandard]:
        return [standard.model_copy(deep=True) for standard in self.items]

    def _fill(self, select: int | None = None) -> None:
        self.table.setRowCount(0)
        for standard in self.items:
            row = self.table.rowCount()
            self.table.insertRow(row)
            values = [
                standard.label or standard.id,
                TYPE_NAMES[standard.type],
                standard.gender_text,
                definition_text(standard.definition),
                specifications_text(standard.type, standard.specifications),
                str(standard.frequency_range) if standard.frequency_range else "el del kit",
            ]
            for column, text in enumerate(values):
                item = QTableWidgetItem(text)
                item.setToolTip(f"{standard.id}: {standard.description}" if standard.description else standard.id)
                self.table.setItem(row, column, item)
        if select is not None and 0 <= select < len(self.items):
            self.table.selectRow(select)

    def _current(self) -> int | None:
        rows = {index.row() for index in self.table.selectedIndexes()}
        return rows.pop() if rows else None

    def _dialog(self, standard: KitStandard | None, taken: set[str]) -> StandardDialog:
        return StandardDialog(self.library, standard, taken, self.settings, self.kit_id, self.folder, self)

    def new_standard(self) -> None:
        dialog = self._dialog(None, {s.id for s in self.items})
        if dialog.exec() and dialog.result_standard:
            self.items.append(dialog.result_standard)
            self._fill(len(self.items) - 1)
            self.changed.emit()

    def edit_standard(self) -> None:
        index = self._current()
        if index is None:
            return
        dialog = self._dialog(self.items[index], {s.id for s in self.items})
        if dialog.exec() and dialog.result_standard:
            self.items[index] = dialog.result_standard
            self._fill(index)
            self.changed.emit()

    def duplicate_standard(self) -> None:
        index = self._current()
        if index is None:
            return
        copy = self.items[index].model_copy(deep=True)
        copy.id = unique_id(copy.id, {s.id for s in self.items})
        self.items.insert(index + 1, copy)
        self._fill(index + 1)
        self.changed.emit()

    def remove_standard(self) -> None:
        index = self._current()
        if index is None:
            return
        standard = self.items[index]
        if QMessageBox.question(self, "Eliminar patrón", f"¿Quitar «{standard.label or standard.id}» del kit?") != QMessageBox.StandardButton.Yes:
            return
        del self.items[index]
        self._fill(min(index, len(self.items) - 1))
        self.changed.emit()

    def import_ckt(self) -> None:
        filename, _ = QFileDialog.getOpenFileName(self, "Fichero de kit de Keysight", str(self.library.root), "Kit de calibración (*.ckt *.CKT)")
        if filename:
            self.import_file(Path(filename))

    def import_file(self, path: Path, replace: bool | None = None) -> CktKit | None:
        """The standards of a .ckt; ``replace`` None asks whether to replace the current ones or add them."""
        try:
            kit = read_ckt(path)
            standards = kit_standards(kit)
        except (KitError, OSError) as exc:
            QMessageBox.warning(self, "Importar .ckt", str(exc))
            return None
        if self.items and replace is None:
            answer = QMessageBox.question(
                self,
                "Importar .ckt",
                f"El kit ya tiene {len(self.items)} patrones. ¿Sustituirlos por los {len(standards)} de {path.name}?\n"
                "Sí: se sustituyen (se conservan las especificaciones de los que tengan el mismo identificador). "
                "No: se añaden.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No | QMessageBox.StandardButton.Cancel,
            )
            if answer == QMessageBox.StandardButton.Cancel:
                return None
            replace = answer == QMessageBox.StandardButton.Yes
        if replace or not self.items:
            previous = {s.id: s for s in self.items}
            for standard in standards:
                if standard.id in previous and not standard.specifications:
                    standard.specifications = previous[standard.id].specifications
            self.items = standards
        else:
            taken = {s.id for s in self.items}
            for standard in standards:
                standard.id = unique_id(standard.id, taken)
                taken.add(standard.id)
            self.items += standards
        self._fill()
        self.changed.emit()
        self.ckt_imported.emit(kit, path)
        return kit

    def export(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Carpeta para los .sdatb de los patrones", str(self.library.root))
        if not folder:
            return
        settings = self.settings()
        resolved = [ResolvedStandard(s, None, self.folder()) for s in self.items]
        try:
            files = self.library.export_standards(resolved, settings, Path(folder))
        except (KitError, SDataError, OSError) as exc:
            QMessageBox.warning(self, "Exportar .sdatb", str(exc))
            return
        QMessageBox.information(self, "Exportar .sdatb", f"{len(files)} ficheros en {folder}:\n" + "\n".join(p.name for p in files))


class UnitStandardsEditor(QWidget):
    """The standards of a unit of the kit: serial number, state and own data of each standard of its model."""

    changed = Signal()

    HEADERS = ["Patrón", "Tipo", "Género", "Nº de serie", "Estado", "Definición", "Nombre (VNA Tools)"]

    def __init__(self, library: Library, kit_id: Callable[[], str], unit_folder: Callable[[], Path], parent: QWidget | None = None):
        super().__init__(parent)
        self.library, self.kit_id, self.unit_folder = library, kit_id, unit_folder
        self.model = None
        self.units: dict[str, UnitStandard] = {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        self.status = QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        buttons = QHBoxLayout()
        edit = _button("Editar…", "editar", "Nº de serie, estado y datos propios del patrón, con su vista (también con doble clic)")
        edit.clicked.connect(self.edit_standard)
        export = _button("Exportar .sdatb…", "exportar", "Un .sdatb por patrón de esta unidad, con su nº de serie en el nombre")
        export.clicked.connect(self.export)
        buttons.addWidget(edit)
        buttons.addStretch(1)
        buttons.addWidget(export)
        layout.addLayout(buttons)
        self.table = _table(self.HEADERS)
        self.table.doubleClicked.connect(lambda _i: self.edit_standard())
        layout.addWidget(self.table, 1)
        self.note = QLabel("Los patrones y su definición nominal son los del modelo del kit (Catálogo). Aquí: lo de esta unidad.")
        self.note.setObjectName("pageSubtitle")
        self.note.setWordWrap(True)
        layout.addWidget(self.note)

    def load(self, model, units: dict[str, UnitStandard]) -> None:
        self.model = model
        self.units = {key: value.model_copy(deep=True) for key, value in units.items()}
        self._fill()

    def set_status(self, html_text: str) -> None:
        self.status.setText(html_text)

    def _standards(self) -> list[ResolvedStandard]:
        if self.model is None:
            return []
        folder = self.model.source_path.parent if self.model.source_path else self.library.root / "catalogo" / "modelos"
        return [ResolvedStandard(s, self.units.get(s.id), folder, self.unit_folder()) for s in self.model.standards]

    def _fill(self, select: int | None = None) -> None:
        self.table.setRowCount(0)
        name = self.model.designation if self.model else ""
        for resolved in self._standards():
            row = self.table.rowCount()
            self.table.insertRow(row)
            standard = resolved.standard
            definition = definition_text(resolved.definition) + (" (propia)" if resolved.own_definition else "")
            values = [standard.label or standard.id, TYPE_NAMES[standard.type], standard.gender_text, resolved.serial_number or "—",
                      STATE_NAMES[resolved.state], definition, resolved.name(name)]
            for column, text in enumerate(values):
                item = QTableWidgetItem(text)
                if resolved.state == "danado":
                    item.setForeground(Qt.GlobalColor.red)
                self.table.setItem(row, column, item)
        if self.model is None or not self.model.standards:
            self.note.setText("El modelo de este kit no define sus patrones: defínalos en su modelo (Catálogo → Editar modelo → Patrones del kit).")
        if select is not None:
            self.table.selectRow(select)

    def edit_standard(self) -> None:
        rows = {index.row() for index in self.table.selectedIndexes()}
        if not rows or self.model is None:
            return
        index = rows.pop()
        resolved = self._standards()[index]
        settings = self.library.kit_settings(self.model)
        dialog = UnitStandardDialog(self.library, resolved.standard, resolved.unit, settings, self.kit_id() or "kit", resolved.model_folder, self.unit_folder(), self)
        if dialog.exec() and dialog.result_unit is not None:
            if dialog.result_unit.empty:
                self.units.pop(resolved.id, None)
            else:
                self.units[resolved.id] = dialog.result_unit
            self._fill(index)
            self.changed.emit()

    def export(self) -> None:
        if self.model is None:
            return
        folder = QFileDialog.getExistingDirectory(self, "Carpeta para los .sdatb de los patrones", str(self.library.root))
        if not folder:
            return
        try:
            files = self.library.export_standards(self._standards(), self.library.kit_settings(self.model), Path(folder))
        except (KitError, SDataError, OSError) as exc:
            QMessageBox.warning(self, "Exportar .sdatb", str(exc))
            return
        QMessageBox.information(self, "Exportar .sdatb", f"{len(files)} ficheros en {folder}:\n" + "\n".join(p.name for p in files))
