"""Editors of the parts of a test template, shared by the template editor and the new-test dialog.

* :class:`FrequencyEditor`  frequency definition: linear (points or step),
  logarithmic (points) or a CSV list, and the source power;
* :class:`TraceEditor`      IFBW, averaging and smoothing for all traces or per trace;
* :class:`SpecEditor`       S2P/S1P specification files that give pass / fail.

File references are stored as given by an *importer* (the template editor
copies the file into ``procedimientos/``; a new test keeps the chosen path and
copies the file into its own folder) and resolved with a *resolver*.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from pydantic import ValidationError
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from calelektra.catalog.library import validation_message
from calelektra.core.frequency import FrequencyRange
from calelektra.core.touchstone import TouchstoneError, read_touchstone
from calelektra.core.units import UnitError, format_frequency, parse_frequency
from calelektra.gui import style
from calelektra.gui.icons import icon
from calelektra.procedures.frequencies import FrequencyError, plan_frequencies
from calelektra.procedures.model import LINEAR_DEFINITIONS, SPEC_CRITERIA, SPEC_QUANTITIES, SWEEP_MODES, SpecFile, SweepSettings

Resolver = Callable[[str], Path]  # file reference of the template → path on disk
Importer = Callable[[Path], str]  # file chosen by the user → reference stored in the template


def _frequency_text(value: float | None) -> str:
    return "" if value is None else format_frequency(value, decimal=".")


def _parse_optional_frequency(text: str, label: str) -> float | None:
    text = text.strip()
    if not text:
        return None
    try:
        return parse_frequency(text)
    except UnitError as exc:
        raise ValueError(f"{label}: {exc}") from exc


class FrequencyEditor(QWidget):
    """Frequency points of the test: linear by points or step, logarithmic by points, or a CSV list."""

    changed = Signal()

    def __init__(self, resolver: Resolver, importer: Importer, parent: QWidget | None = None):
        super().__init__(parent)
        self.resolver = resolver
        self.importer = importer
        self.default_range: FrequencyRange | None = None
        self._loading = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.form = QFormLayout()
        layout.addLayout(self.form)
        self.mode = QComboBox()
        for key, text in SWEEP_MODES.items():
            self.mode.addItem(text, key)
        self.setup_range = QCheckBox("Rango requerido del hardware setup")
        self.setup_range.setToolTip("Sin inicio ni fin propios: el ensayo usa el rango requerido del setup elegido")
        self.start = QLineEdit()
        self.start.setPlaceholderText("p. ej. 50 MHz")
        self.stop = QLineEdit()
        self.stop.setPlaceholderText("p. ej. 40 GHz")
        self.definition = QComboBox()
        for key, text in LINEAR_DEFINITIONS.items():
            self.definition.addItem(text, key)
        self.points = QSpinBox()
        self.points.setRange(2, 100001)
        self.points.setSingleStep(100)
        self.step = QLineEdit()
        self.step.setPlaceholderText("p. ej. 50 MHz")
        file_row = QWidget()
        file_layout = QHBoxLayout(file_row)
        file_layout.setContentsMargins(0, 0, 0, 0)
        self.list_file = QLineEdit()
        self.list_file.setReadOnly(True)
        self.list_file.setPlaceholderText("Fichero CSV con las frecuencias")
        self.browse = QPushButton(icon("abrir", style.PRIMARY.name()), "Examinar…")
        self.browse.clicked.connect(self._choose_list)
        file_layout.addWidget(self.list_file, 1)
        file_layout.addWidget(self.browse)
        self.power = QDoubleSpinBox()
        self.power.setRange(-100, 30)
        self.power.setDecimals(1)
        self.power.setSuffix(" dBm")
        self.preview = QLabel()
        self.preview.setObjectName("pageSubtitle")
        self.preview.setWordWrap(True)
        self.form.addRow("Tipo de barrido", self.mode)
        self.form.addRow("", self.setup_range)
        self.form.addRow("Inicio", self.start)
        self.form.addRow("Fin", self.stop)
        self.form.addRow("Definición", self.definition)
        self.form.addRow("Nº de puntos", self.points)
        self.form.addRow("Paso", self.step)
        self.form.addRow("Lista (CSV)", file_row)
        self.form.addRow("Potencia", self.power)
        self._file_row = file_row
        layout.addWidget(self.preview)
        for combo in (self.mode, self.definition):
            combo.currentIndexChanged.connect(self._edited)
        for edit in (self.start, self.stop, self.step):
            edit.textEdited.connect(self._edited)
        self.points.valueChanged.connect(self._edited)
        self.power.valueChanged.connect(self._edited)
        self.setup_range.toggled.connect(self._edited)
        self._update_rows()

    # ── state ──────────────────────────────────────────────────────────────
    def load(self, sweep: SweepSettings) -> None:
        self._loading = True
        self.mode.setCurrentIndex(max(self.mode.findData(sweep.mode), 0))
        self.setup_range.setChecked(sweep.start is None and sweep.stop is None)
        self.start.setText(_frequency_text(sweep.start))
        self.stop.setText(_frequency_text(sweep.stop))
        self.definition.setCurrentIndex(max(self.definition.findData(sweep.linear_definition), 0))
        self.points.setValue(sweep.points)
        self.step.setText(_frequency_text(sweep.step))
        self.list_file.setText(sweep.list_file or "")
        self.power.setValue(sweep.power_dbm)
        self._loading = False
        self._update_rows()

    def set_default_range(self, frequency_range: FrequencyRange | None) -> None:
        """Required range of the setup, used when the template has no range of its own (preview only)."""
        self.default_range = frequency_range
        self._update_preview()

    def values(self) -> dict[str, Any]:
        """``barrido`` keys of the frequency definition; ``ValueError`` with the reason if something is wrong."""
        mode = self.mode.currentData()
        data: dict[str, Any] = {"modo": mode, "potencia_dbm": self.power.value()}
        if mode == "lista":
            if not self.list_file.text().strip():
                raise ValueError("Elija el fichero CSV con la lista de frecuencias")
            data["fichero_lista"] = self.list_file.text().strip()
            return data
        if not self.setup_range.isChecked():
            start = _parse_optional_frequency(self.start.text(), "Inicio")
            stop = _parse_optional_frequency(self.stop.text(), "Fin")
            if start is None or stop is None:
                raise ValueError("Indique el inicio y el fin del rango de frecuencia (o use el rango del hardware setup)")
            if stop <= start:
                raise ValueError("La frecuencia final debe ser mayor que la inicial")
            data["inicio"], data["fin"] = _frequency_text(start), _frequency_text(stop)
        if mode == "lineal":
            data["definicion"] = self.definition.currentData()
            if data["definicion"] == "paso":
                step = _parse_optional_frequency(self.step.text(), "Paso")
                if not step or step <= 0:
                    raise ValueError("Indique el paso de frecuencia")
                data["paso"] = _frequency_text(step)
        data["puntos"] = self.points.value()
        return data

    # ── behaviour ──────────────────────────────────────────────────────────
    def _edited(self, *_args) -> None:
        self._update_rows()
        if not self._loading:
            self.changed.emit()

    def _update_rows(self) -> None:
        mode = self.mode.currentData()
        linear, listed = mode == "lineal", mode == "lista"
        by_step = linear and self.definition.currentData() == "paso"
        own_range = not self.setup_range.isChecked()
        self.form.setRowVisible(self.setup_range, not listed)
        self.form.setRowVisible(self.start, not listed)
        self.form.setRowVisible(self.stop, not listed)
        self.start.setEnabled(own_range)
        self.stop.setEnabled(own_range)
        self.form.setRowVisible(self.definition, linear)
        self.form.setRowVisible(self.points, not listed and not by_step)
        self.form.setRowVisible(self.step, by_step)
        self.form.setRowVisible(self._file_row, listed)
        self._update_preview()

    def _update_preview(self) -> None:
        try:
            data = self.values()
        except ValueError as exc:
            self.preview.setText(f"⚠ {exc}")
            self.preview.setStyleSheet(f"color: {style.WARNING.name()};")
            return
        self.preview.setStyleSheet("")
        try:
            sweep = SweepSettings.model_validate(data)
        except ValidationError as exc:
            self.preview.setText(f"⚠ {validation_message(exc)}")
            return
        if sweep.mode != "lista" and sweep.start is None and self.default_range is None:
            points = "puntos según el paso" if sweep.linear_definition == "paso" and sweep.mode == "lineal" else f"{sweep.points} puntos"
            self.preview.setText(f"→ {SWEEP_MODES[sweep.mode]} en el rango requerido del hardware setup, {points}")
            return
        list_path = self.resolver(sweep.list_file) if sweep.list_file else None
        try:
            plan = plan_frequencies(sweep, self.default_range, list_path)
        except FrequencyError as exc:
            self.preview.setText(f"⚠ {exc}")
            self.preview.setStyleSheet(f"color: {style.WARNING.name()};")
            return
        self.preview.setText(f"→ {plan.describe()}")

    def _choose_list(self) -> None:
        start = str(self.resolver(self.list_file.text()).parent) if self.list_file.text() else ""
        filename, _ = QFileDialog.getOpenFileName(self, "Lista de frecuencias", start, "CSV (*.csv *.txt);;Todos (*)")
        if not filename:
            return
        try:
            reference = self.importer(Path(filename))
        except OSError as exc:
            QMessageBox.warning(self, "Lista de frecuencias", str(exc))
            return
        self.list_file.setText(reference)
        self._edited()


TRACE_ROWS = ("Todas", "S11", "S21", "S12", "S22")


class TraceEditor(QWidget):
    """IFBW, averaging and smoothing of all traces, or of each trace (empty cell = value of «Todas»)."""

    changed = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._loading = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.per_trace = QCheckBox("Ajustes por traza")
        self.per_trace.setToolTip("Una celda vacía toma el valor de «Todas las trazas»")
        layout.addWidget(self.per_trace)
        self.table = QTableWidget(len(TRACE_ROWS), 3)
        self.table.setHorizontalHeaderLabels(["IFBW", "Promedios", "Suavizado (%)"])
        self.table.setVerticalHeaderLabels(["Todas las trazas", *TRACE_ROWS[1:]])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setDefaultSectionSize(26)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setFixedHeight(26 * len(TRACE_ROWS) + 42)
        self.table.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        layout.addWidget(self.table)
        hint = QLabel(
            "Las trazas con IFBW o promedios distintos se miden en barridos separados. "
            "El suavizado es una media móvil de la magnitud (dB) y de la fase sobre el % de puntos indicado."
        )
        hint.setObjectName("pageSubtitle")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        for row in range(len(TRACE_ROWS)):
            for column in range(3):
                self.table.setItem(row, column, QTableWidgetItem(""))
        self.per_trace.toggled.connect(self._edited)
        self.table.itemChanged.connect(self._edited)
        self.set_traces(TRACE_ROWS[1:])
        self._update_rows()

    def set_traces(self, traces: tuple[str, ...] | list[str]) -> None:
        """Show only the traces measured by the algorithm (S11 for 1 port)."""
        for row, name in enumerate(TRACE_ROWS[1:], start=1):
            self.table.setRowHidden(row, name not in traces)
        visible = 1 + sum(1 for name in TRACE_ROWS[1:] if name in traces)
        self.table.setFixedHeight(26 * visible + 42)

    def load(self, sweep: SweepSettings) -> None:
        self._loading = True
        self.table.blockSignals(True)
        self._set_row(0, sweep.if_bandwidth, sweep.averages, sweep.smoothing_pct)
        for row, name in enumerate(TRACE_ROWS[1:], start=1):
            own = sweep.traces.get(name)
            self._set_row(row, own.if_bandwidth if own else None, own.averages if own else None, own.smoothing_pct if own else None)
        self.table.blockSignals(False)
        self.per_trace.setChecked(sweep.per_trace)
        self._loading = False
        self._update_rows()

    def _set_row(self, row: int, ifbw: float | None, averages: int | None, smoothing: float | None) -> None:
        self.table.item(row, 0).setText(_frequency_text(ifbw))
        self.table.item(row, 1).setText("" if averages is None else str(averages))
        self.table.item(row, 2).setText("" if smoothing is None else f"{smoothing:g}")

    def _cell(self, row: int, column: int) -> str:
        item = self.table.item(row, column)
        return item.text().strip() if item else ""

    def _row_values(self, row: int, required: bool) -> dict[str, Any]:
        label = "todas las trazas" if row == 0 else TRACE_ROWS[row]
        values: dict[str, Any] = {}
        ifbw, averages, smoothing = (self._cell(row, c) for c in range(3))
        if ifbw or required:
            try:
                values["ifbw"] = _frequency_text(parse_frequency(ifbw or "?"))
            except UnitError as exc:
                raise ValueError(f"IFBW de {label}: {exc}") from exc
        if averages or required:
            try:
                count = int(averages or "x")
            except ValueError:
                raise ValueError(f"Promedios de {label}: «{averages}» no es un número entero") from None
            if count < 1:
                raise ValueError(f"Promedios de {label}: mínimo 1")
            values["promedios"] = count
        if smoothing or required:
            try:
                pct = float((smoothing or "0").replace(",", "."))
            except ValueError:
                raise ValueError(f"Suavizado de {label}: «{smoothing}» no es un número") from None
            if not 0 <= pct <= 100:
                raise ValueError(f"Suavizado de {label}: entre 0 y 100 %")
            values["suavizado_pct"] = pct
        return values

    def values(self) -> dict[str, Any]:
        """``barrido`` keys of the trace settings; ``ValueError`` with the reason if a cell is wrong."""
        data = self._row_values(0, required=True)
        data["por_traza"] = self.per_trace.isChecked()
        traces = {}
        for row, name in enumerate(TRACE_ROWS[1:], start=1):
            if self.table.isRowHidden(row):
                continue
            own = self._row_values(row, required=False)
            if own:
                traces[name] = own
        if traces:
            data["trazas"] = traces  # kept even when «por traza» is off, so they are not lost
        return data

    def _edited(self, *_args) -> None:
        self._update_rows()
        if not self._loading:
            self.changed.emit()

    def _update_rows(self) -> None:
        enabled = self.per_trace.isChecked()
        self.table.blockSignals(True)
        for row in range(1, len(TRACE_ROWS)):
            for column in range(3):
                item = self.table.item(row, column)
                flags = Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled
                if enabled:
                    flags |= Qt.ItemFlag.ItemIsEditable
                else:
                    flags &= ~Qt.ItemFlag.ItemIsEnabled
                item.setFlags(flags)
                item.setToolTip("Vacío: valor de «Todas las trazas»" if enabled else "Active «Ajustes por traza»")
        self.table.blockSignals(False)


SPEC_COLUMNS = ["Nombre", "Fichero", "Parámetros", "Criterio", "Magnitud", "Tolerancia"]


class SpecEditor(QWidget):
    """Specification files (S2P/S1P) and how the measurement is compared with them."""

    changed = Signal()

    def __init__(self, resolver: Resolver, importer: Importer, parent: QWidget | None = None):
        super().__init__(parent)
        self.resolver = resolver
        self.importer = importer
        self.traces: tuple[str, ...] = ("S11", "S21", "S12", "S22")
        self._loading = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.table = QTableWidget(0, len(SPEC_COLUMNS))
        self.table.setHorizontalHeaderLabels(SPEC_COLUMNS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(30)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.setColumnWidth(1, 230)
        self.table.setColumnWidth(2, 90)
        self.table.setColumnWidth(3, 130)
        self.table.setColumnWidth(4, 150)
        self.table.setColumnWidth(5, 80)
        self.table.setMinimumHeight(110)
        layout.addWidget(self.table, 1)
        buttons = QHBoxLayout()
        self.add_button = QPushButton(icon("nuevo", style.PRIMARY.name()), "Añadir fichero…")
        self.view_button = QPushButton(icon("grafica", style.PRIMARY.name()), "Ver")
        self.remove_button = QPushButton(icon("eliminar", style.ERROR.name()), "Quitar")
        self.add_button.clicked.connect(self._add)
        self.view_button.clicked.connect(self._view)
        self.remove_button.clicked.connect(self._remove)
        self.empty = QLabel("Sin especificaciones: el ensayo no da veredicto pasa / falla.")
        self.empty.setObjectName("pageSubtitle")
        buttons.addWidget(self.add_button)
        buttons.addWidget(self.view_button)
        buttons.addWidget(self.remove_button)
        buttons.addSpacing(8)
        buttons.addWidget(self.empty, 1)
        layout.addLayout(buttons)
        self.table.itemChanged.connect(self._edited)
        self.table.itemSelectionChanged.connect(self._update_buttons)
        self._update_buttons()

    # ── state ──────────────────────────────────────────────────────────────
    def set_traces(self, traces: tuple[str, ...] | list[str]) -> None:
        self.traces = tuple(traces)

    def load(self, specs: list[SpecFile]) -> None:
        self._loading = True
        self.table.blockSignals(True)
        self.table.setRowCount(0)
        for spec in specs:
            self._add_row(spec.name, spec.file, spec.parameters, spec.criterion, spec.quantity, spec.tolerance)
        self.table.blockSignals(False)
        self._loading = False
        self._update_buttons()

    def values(self) -> list[dict[str, Any]]:
        """``especificaciones`` of the template; ``ValueError`` with the reason if a row is wrong."""
        specs = []
        for row in range(self.table.rowCount()):
            name = self.table.item(row, 0).text().strip() or f"Especificación {row + 1}"
            parameters = [p.strip().upper() for p in self.table.item(row, 2).text().replace(";", ",").split(",") if p.strip()]
            if not parameters:
                raise ValueError(f"«{name}»: indique los parámetros S a comparar (p. ej. S21, S12)")
            unknown = [p for p in parameters if p not in self.traces]
            if unknown:
                raise ValueError(f"«{name}»: el procedimiento no mide {', '.join(unknown)} (mide {', '.join(self.traces)})")
            criterion = self.table.cellWidget(row, 3).currentData()
            data: dict[str, Any] = {
                "nombre": name,
                "fichero": self.table.item(row, 1).data(Qt.ItemDataRole.UserRole),
                "parametros": parameters,
                "criterio": criterion,
                "magnitud": self.table.cellWidget(row, 4).currentData(),
            }
            if criterion == "tolerancia":
                text = self.table.item(row, 5).text().strip().replace(",", ".")
                try:
                    tolerance = float(text)
                except ValueError:
                    raise ValueError(f"«{name}»: indique la tolerancia (número positivo)") from None
                if tolerance <= 0:
                    raise ValueError(f"«{name}»: la tolerancia debe ser positiva")
                data["tolerancia"] = tolerance
            specs.append(data)
        return specs

    def specs(self) -> list[SpecFile]:
        return [SpecFile.model_validate(item) for item in self.values()]

    # ── rows ───────────────────────────────────────────────────────────────
    def _add_row(self, name: str, reference: str, parameters: list[str], criterion: str, quantity: str, tolerance: float | None) -> None:
        row = self.table.rowCount()
        self.table.insertRow(row)
        self.table.setItem(row, 0, QTableWidgetItem(name))
        file_item = QTableWidgetItem(Path(reference).name)
        file_item.setData(Qt.ItemDataRole.UserRole, reference)
        file_item.setFlags(file_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        path = self.resolver(reference)
        if path.is_file():
            file_item.setIcon(icon("grafica", style.PRIMARY.name()))
            file_item.setToolTip(str(path))
        else:
            file_item.setIcon(icon("error", style.ERROR.name()))
            file_item.setToolTip(f"No se encuentra {path}")
        self.table.setItem(row, 1, file_item)
        self.table.setItem(row, 2, QTableWidgetItem(", ".join(parameters)))
        criteria = QComboBox()
        for key, text in SPEC_CRITERIA.items():
            criteria.addItem(text.split(":")[0], key)
            criteria.setItemData(criteria.count() - 1, text, Qt.ItemDataRole.ToolTipRole)
        criteria.setCurrentIndex(max(criteria.findData(criterion), 0))
        quantities = QComboBox()
        for key, text in SPEC_QUANTITIES.items():
            quantities.addItem(text, key)
        quantities.setCurrentIndex(max(quantities.findData(quantity), 0))
        self.table.setCellWidget(row, 3, criteria)
        self.table.setCellWidget(row, 4, quantities)
        self.table.setItem(row, 5, QTableWidgetItem("" if tolerance is None else f"{tolerance:g}"))
        criteria.currentIndexChanged.connect(lambda _i, r=row: self._criterion_changed(r))
        quantities.currentIndexChanged.connect(self._edited)
        self._criterion_changed(row, emit=False)

    def _criterion_changed(self, row: int, emit: bool = True) -> None:
        combo = self.table.cellWidget(row, 3)
        item = self.table.item(row, 5)
        if combo is None or item is None:
            return
        tolerance = combo.currentData() == "tolerancia"
        flags = item.flags()
        item.setFlags(flags | Qt.ItemFlag.ItemIsEditable if tolerance else flags & ~Qt.ItemFlag.ItemIsEditable)
        item.setToolTip("± tolerancia en dB o grados" if tolerance else "Solo para el criterio de tolerancia")
        if emit:
            self._edited()

    def _add(self) -> None:
        filename, _ = QFileDialog.getOpenFileName(self, "Fichero de especificación", "", "Touchstone (*.s1p *.s2p);;Todos (*)")
        if filename:
            self.add_file(Path(filename))

    def add_file(self, path: Path) -> bool:
        """Add a specification file with defaults taken from its number of ports."""
        try:
            data = read_touchstone(path)
        except (TouchstoneError, OSError) as exc:
            QMessageBox.warning(self, "Especificación", f"No se puede leer {path.name}: {exc}")
            return False
        try:
            reference = self.importer(path)
        except OSError as exc:
            QMessageBox.warning(self, "Especificación", str(exc))
            return False
        if data.n_ports >= 2 and "S21" in self.traces:
            parameters, criterion = ["S21", "S12"], "minimo"
        else:
            parameters, criterion = ["S11"], "maximo"
        self._add_row(path.stem, reference, parameters, criterion, "magnitud_db", None)
        self.table.selectRow(self.table.rowCount() - 1)
        self._edited()
        return True

    def _selected_row(self) -> int | None:
        rows = self.table.selectionModel().selectedRows()
        return rows[0].row() if rows else None

    def _view(self) -> None:
        row = self._selected_row()
        if row is None:
            return
        from calelektra.gui.plots import SParameterDialog  # plots imports pyqtgraph: only when needed

        path = self.resolver(self.table.item(row, 1).data(Qt.ItemDataRole.UserRole))
        SParameterDialog(None, None, self, path=path).exec()

    def _remove(self) -> None:
        row = self._selected_row()
        if row is not None:
            self.table.removeRow(row)
            # rows below moved up: reconnect the criterion combos with their new row numbers
            specs = []
            for index in range(self.table.rowCount()):
                specs.append(
                    (
                        self.table.item(index, 0).text(),
                        self.table.item(index, 1).data(Qt.ItemDataRole.UserRole),
                        [p.strip() for p in self.table.item(index, 2).text().split(",") if p.strip()],
                        self.table.cellWidget(index, 3).currentData(),
                        self.table.cellWidget(index, 4).currentData(),
                        self.table.item(index, 5).text().strip() or None,
                    )
                )
            self.table.blockSignals(True)
            self.table.setRowCount(0)
            for name, reference, parameters, criterion, quantity, tolerance in specs:
                value = None
                if tolerance is not None:
                    try:
                        value = float(tolerance.replace(",", "."))
                    except ValueError:
                        value = None
                self._add_row(name, reference, parameters, criterion, quantity, value)
            self.table.blockSignals(False)
            self._edited()

    def _edited(self, *_args) -> None:
        self._update_buttons()
        if not self._loading:
            self.changed.emit()

    def _update_buttons(self) -> None:
        selected = self._selected_row() is not None
        self.view_button.setEnabled(selected)
        self.remove_button.setEnabled(selected)
        self.empty.setVisible(self.table.rowCount() == 0)
