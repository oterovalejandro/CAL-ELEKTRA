"""Editors shared by the hardware setups, the test templates and the catalogue models.

* :class:`FrequencyEditor`  frequency points of a hardware setup: linear (points
  or step), logarithmic (points) or a CSV list, inside its required range;
* :class:`TraceEditor`      IFBW, averaging and display smoothing of a test
  template, for all traces or per trace;
* :class:`SpecEditor`       S2P/S1P specification files of a catalogue model
  (pass / fail of its units); read only in the test template.

File references are stored as given by an *importer* (the file is copied into
the library) and resolved with a *resolver*.
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
from calelektra.catalog.model import SPEC_CRITERIA, SPEC_QUANTITIES, SpecFile
from calelektra.procedures.model import SweepSettings
from calelektra.setups.frequencies import FrequencyError, plan_frequencies, read_frequency_list
from calelektra.setups.model import LINEAR_DEFINITIONS, SWEEP_MODES, FrequencySettings

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
    """Frequency points of a hardware setup inside its required range."""

    changed = Signal()
    list_loaded = Signal(object)  # FrequencyRange of a CSV list just chosen

    def __init__(self, resolver: Resolver, importer: Importer, parent: QWidget | None = None):
        super().__init__(parent)
        self.resolver = resolver
        self.importer = importer
        self.required_range: FrequencyRange | None = None
        self._loading = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.form = QFormLayout()
        layout.addLayout(self.form)
        self.mode = QComboBox()
        for key, text in SWEEP_MODES.items():
            self.mode.addItem(text, key)
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
        self.preview = QLabel()
        self.preview.setObjectName("pageSubtitle")
        self.preview.setWordWrap(True)
        self.form.addRow("Tipo de barrido", self.mode)
        self.form.addRow("Definición", self.definition)
        self.form.addRow("Nº de puntos", self.points)
        self.form.addRow("Paso", self.step)
        self.form.addRow("Lista (CSV)", file_row)
        self._file_row = file_row
        note = QLabel("Las frecuencias se reparten en el rango requerido del setup; de una lista solo se miden las que caen dentro.")
        note.setObjectName("pageSubtitle")
        note.setWordWrap(True)
        layout.addWidget(self.preview)
        layout.addWidget(note)
        layout.addStretch(1)
        for combo in (self.mode, self.definition):
            combo.currentIndexChanged.connect(self._edited)
        self.step.textEdited.connect(self._edited)
        self.points.valueChanged.connect(self._edited)
        self._update_rows()

    # ── state ──────────────────────────────────────────────────────────────
    def load(self, settings: FrequencySettings, required_range: FrequencyRange | None = None) -> None:
        self._loading = True
        self.required_range = required_range
        self.mode.setCurrentIndex(max(self.mode.findData(settings.mode), 0))
        self.definition.setCurrentIndex(max(self.definition.findData(settings.linear_definition), 0))
        self.points.setValue(settings.points)
        self.step.setText(_frequency_text(settings.step))
        self.list_file.setText(settings.list_file or "")
        self._loading = False
        self._update_rows()

    def set_required_range(self, required_range: FrequencyRange | None) -> None:
        self.required_range = required_range
        self._update_preview()

    def values(self) -> dict[str, Any]:
        """``frecuencias`` of the setup; ``ValueError`` with the reason if something is wrong."""
        mode = self.mode.currentData()
        data: dict[str, Any] = {"modo": mode}
        if mode == "lista":
            if not self.list_file.text().strip():
                raise ValueError("Elija el fichero CSV con la lista de frecuencias")
            data["fichero_lista"] = self.list_file.text().strip()
            return data
        if mode == "lineal":
            data["definicion"] = self.definition.currentData()
            if data["definicion"] == "paso":
                step = _parse_optional_frequency(self.step.text(), "Paso")
                if not step or step <= 0:
                    raise ValueError("Indique el paso de frecuencia")
                data["paso"] = _frequency_text(step)
                return data
        data["puntos"] = self.points.value()
        return data

    def settings(self) -> FrequencySettings:
        try:
            return FrequencySettings.model_validate(self.values())
        except ValidationError as exc:
            raise ValueError(validation_message(exc)) from exc

    # ── behaviour ──────────────────────────────────────────────────────────
    def _edited(self, *_args) -> None:
        self._update_rows()
        if not self._loading:
            self.changed.emit()

    def _update_rows(self) -> None:
        mode = self.mode.currentData()
        linear, listed = mode == "lineal", mode == "lista"
        by_step = linear and self.definition.currentData() == "paso"
        self.form.setRowVisible(self.definition, linear)
        self.form.setRowVisible(self.points, not listed and not by_step)
        self.form.setRowVisible(self.step, by_step)
        self.form.setRowVisible(self._file_row, listed)
        self._update_preview()

    def _update_preview(self) -> None:
        def problem(text: str) -> None:
            self.preview.setText(f"⚠ {text}")
            self.preview.setStyleSheet(f"color: {style.WARNING.name()};")

        try:
            settings = self.settings()
        except ValueError as exc:
            problem(str(exc))
            return
        if self.required_range is None:
            problem("Indique el rango requerido del setup")
            return
        list_path = self.resolver(settings.list_file) if settings.list_file else None
        try:
            plan = plan_frequencies(settings, self.required_range, list_path)
        except FrequencyError as exc:
            problem(str(exc))
            return
        self.preview.setStyleSheet("")
        self.preview.setText(f"→ {plan.describe()}")

    def _choose_list(self) -> None:
        start = str(self.resolver(self.list_file.text()).parent) if self.list_file.text() else ""
        filename, _ = QFileDialog.getOpenFileName(self, "Lista de frecuencias", start, "CSV (*.csv *.txt);;Todos (*)")
        if filename:
            self.use_list(Path(filename))

    def use_list(self, path: Path) -> bool:
        """Take a CSV list (copied into the library); announces its range so the setup can adopt it."""
        try:
            frequencies = read_frequency_list(path)
            reference = self.importer(path)
        except (FrequencyError, OSError) as exc:
            QMessageBox.warning(self, "Lista de frecuencias", str(exc))
            return False
        self.list_file.setText(reference)
        self.list_loaded.emit(FrequencyRange(float(frequencies[0]), float(frequencies[-1])))
        self._edited()
        return True


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
        self.table.horizontalHeaderItem(2).setToolTip("Solo visualización: los datos, el pasa / falla y los ficheros no se suavizan")
        self.table.setVerticalHeaderLabels(["Todas las trazas", *TRACE_ROWS[1:]])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setDefaultSectionSize(26)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setFixedHeight(26 * len(TRACE_ROWS) + 42)
        self.table.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        layout.addWidget(self.table)
        hint = QLabel(
            "Las trazas con IFBW o promedios distintos se miden en barridos separados. El suavizado (media móvil de la "
            "magnitud en dB y de la fase sobre el % de puntos indicado) solo cambia la visualización: los datos, el "
            "pasa / falla y los ficheros exportados no se suavizan."
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
    """Specification files (S2P/S1P) and how the measurement is compared with them (``read_only``: only shown)."""

    changed = Signal()

    def __init__(self, resolver: Resolver, importer: Importer | None = None, read_only: bool = False, parent: QWidget | None = None):
        super().__init__(parent)
        self.resolver = resolver
        self.importer = importer
        self.read_only = read_only
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
        self.table.setColumnWidth(1, 200)
        self.table.setColumnWidth(2, 85)
        self.table.setColumnWidth(3, 115)
        self.table.setColumnWidth(4, 125)
        self.table.setColumnWidth(5, 75)
        if read_only:  # compact: the criterion column describes parameters, quantity and tolerance
            for column in (2, 4, 5):
                self.table.setColumnHidden(column, True)
            header.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
            header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
            self.table.setColumnWidth(0, 190)
            self.table.setColumnWidth(1, 170)
        self.table.setMinimumHeight(110)
        layout.addWidget(self.table, 1)
        buttons = QHBoxLayout()
        self.add_button = QPushButton(icon("nuevo", style.PRIMARY.name()), "Añadir fichero…")
        self.view_button = QPushButton(icon("grafica", style.PRIMARY.name()), "Ver")
        self.remove_button = QPushButton(icon("eliminar", style.ERROR.name()), "Quitar")
        self.add_button.clicked.connect(self._add)
        self.view_button.clicked.connect(self._view)
        self.remove_button.clicked.connect(self._remove)
        self.empty = QLabel("Sin especificaciones: no hay veredicto pasa / falla.")
        self.empty.setObjectName("pageSubtitle")
        self.add_button.setVisible(not read_only)
        self.remove_button.setVisible(not read_only)
        if read_only:
            self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
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
        if self.read_only:
            try:
                text = SpecFile(nombre=name, fichero=reference, parametros=parameters, criterio=criterion, magnitud=quantity, tolerancia=tolerance).describe()
            except ValueError:
                text = SPEC_CRITERIA.get(criterion, criterion)
            self.table.setItem(row, 3, QTableWidgetItem(text))
            self.table.setItem(row, 4, QTableWidgetItem(SPEC_QUANTITIES.get(quantity, quantity)))
            self.table.setItem(row, 5, QTableWidgetItem("" if tolerance is None else f"{tolerance:g}"))
            for column in (0, 2):
                self.table.item(row, column).setToolTip(self.table.item(row, column).text())
            return
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
        if self.read_only:
            criteria.setEnabled(False)
            quantities.setEnabled(False)

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
            reference = self.importer(path) if self.importer is not None else str(path)
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
        self.remove_button.setEnabled(selected and not self.read_only)
        self.empty.setVisible(self.table.rowCount() == 0)
