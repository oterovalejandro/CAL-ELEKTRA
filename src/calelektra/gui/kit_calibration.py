"""The calibration of a unit of a kit: the real values of its standards, their deviations from their
definition and the uncertainty that follows from them (``kits/calibration.py``).

«Calibración del kit…» in the tab «Patrones» of the unit:

* the date, certificate and laboratory of the calibration, and the width of the bands of the own
  specifications of the standards;
* a file of real values per standard (``.sdatb`` or Touchstone), assigned by its name: «OPENM
  resultados.s1p» is the male open; «Elegir fichero…» for the others;
* for each standard, its verdict against the specification of the manufacturer, its largest deviations
  and its own specifications (k = 2); for the chosen one, the chart of each quantity: its deviation (with
  its uncertainty, if the file gives it), the specification of the manufacturer (±) and its own one.

On «Aceptar» the files are copied into the library (``catalogo/patrones/<unit>/``) and each standard of
the unit gets its real values (``valores_reales``): its uncertainty comes from them from then on.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

import numpy as np
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
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
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from calelektra.catalog.library import Library
from calelektra.catalog.model import Calibration, EquipmentModel
from calelektra.core.units import UnitError, format_frequency, parse_frequency
from calelektra.gui.frequency_input import install_frequency_completion
from calelektra.gui.icons import icon
from calelektra.gui.kit_standards import specifications_text
from calelektra.gui.plots import ChartLine, ChartTrace, TraceChart
from calelektra.kits import (
    VERDICT_NAMES,
    KitError,
    ResolvedStandard,
    StandardDeviations,
    UnitStandard,
    match_standard,
)
from calelektra.kits.calibration import FLOOR, QuantityDeviation
from calelektra.kits.specs import spec_values
from calelektra.sdata import SDataError

VERDICT_COLORS = {"cumple": "#2e8b57", "no_cumple": "#c0392b", "indeterminado": "#d98a00", "sin_especificacion": "#5b6b80"}
FILE_FILTER = "Valores reales (*.sdatb *.s1p *.s2p *.S1P *.S2P *.ts);;Todos los ficheros (*)"
DEFAULT_BAND = "1 GHz"
AXES = {
    "modulo_db": "Desviación de módulo (dB)",
    "fase_grados": "Desviación de fase (°)",
    "perdidas_retorno_db": "|S − S₀| (dB)",
    "transmision_modulo_db": "Desviación de módulo (dB)",
    "transmision_fase_grados": "Desviación de fase (°)",
}
SHORT = {"modulo_db": "mód.", "fase_grados": "fase", "perdidas_retorno_db": "|S − S₀|", "transmision_modulo_db": "mód.", "transmision_fase_grados": "fase"}
DEVIATION_COLOR, MANUFACTURER_COLOR, OWN_COLOR = "#2b7bb9", "#34495e", "#d98a00"


@dataclass
class CalibrationEntry:
    """A standard of the kit in the dialog: the file of its real values and what follows from it."""

    resolved: ResolvedStandard
    path: Path | None = None
    stored: bool = False  # the file is the one already in the unit
    deviations: StandardDeviations | None = None
    error: str | None = None

    @property
    def comparable(self) -> bool:
        """Its definition is the model of Keysight: its real values can be compared with it."""
        return self.resolved.definition.kind == "modelo" or self.resolved.standard.definition.kind == "modelo"


def _date(text: str) -> date | None:
    text = text.strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"«Fecha de calibración»: fecha no válida «{text}» (use AAAA-MM-DD)") from exc


class DeviationChart(QWidget):
    """A quantity of a standard: its deviation (± U), the specification of the manufacturer and its own one."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.deviations: StandardDeviations | None = None
        self.own: list = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        controls = QHBoxLayout()
        self.quantity = QComboBox()
        self.quantity.currentIndexChanged.connect(lambda _i: self.redraw())
        self.log_x = QCheckBox("Frecuencia logarítmica")
        self.log_x.toggled.connect(lambda _on: self.redraw())
        controls.addWidget(self.quantity)
        controls.addWidget(self.log_x)
        controls.addStretch(1)
        layout.addLayout(controls)
        self.chart = TraceChart()
        layout.addWidget(self.chart, 1)
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        self.summary.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.summary)
        self.show_deviations(None)

    def show_deviations(self, deviations: StandardDeviations | None, own: list | None = None, message: str = "") -> None:
        self.deviations, self.own = deviations, list(own or [])
        current = self.quantity.currentIndex()
        self.quantity.blockSignals(True)
        self.quantity.clear()
        for q in deviations.quantities if deviations else []:
            self.quantity.addItem(q.name[0].upper() + q.name[1:])
        self.quantity.setCurrentIndex(min(max(current, 0), self.quantity.count() - 1))
        self.quantity.blockSignals(False)
        if deviations is None:
            self.chart.draw("", "", [], [], False)
            self.summary.setText(message)
            return
        standard = deviations.standard
        verdict = deviations.verdict
        lines = [f"<b>{standard.label or standard.id}</b>: <b style='color:{VERDICT_COLORS[verdict]}'>{VERDICT_NAMES[verdict]}</b>"]
        lines += [f"• {line}" for line in deviations.summary()]
        if not deviations.uncertain:
            lines.append("Los valores reales no traen incertidumbre (U = 0): la especificación propia es su desviación.")
        self.summary.setText("<br>".join(lines))
        self.redraw()

    def current(self) -> QuantityDeviation | None:
        if self.deviations is None or not self.deviations.quantities:
            return None
        return self.deviations.quantities[max(self.quantity.currentIndex(), 0)]

    def redraw(self) -> None:
        q = self.current()
        if q is None:
            return
        f = self.deviations.frequencies
        if q.distance:
            values = q.shown(q.deviation)
            band = (q.shown(np.maximum(q.deviation - q.uncertainty, FLOOR)), q.shown(q.deviation + q.uncertainty))
        else:
            values = q.deviation
            band = (q.deviation - q.uncertainty, q.deviation + q.uncertainty)
        traces = [ChartTrace("Desviación", f, values, band if np.any(q.uncertainty > 0) else None, DEVIATION_COLOR, "Valores reales − definición")]
        lines = []
        if q.limit is not None:
            if q.distance:
                lines.append(ChartLine("Especificación del fabricante", f, q.shown(q.limit), MANUFACTURER_COLOR))
            else:
                lines += [ChartLine("Especificación del fabricante", f, q.limit, MANUFACTURER_COLOR), ChartLine(None, f, -q.limit, MANUFACTURER_COLOR)]
        own = spec_values(self.own, q.column, f) if self.own else None
        if own is not None:
            if q.distance:
                lines.append(ChartLine("Especificación propia (k = 2)", f, -np.abs(own), OWN_COLOR))
            else:
                lines += [ChartLine("Especificación propia (k = 2)", f, own, OWN_COLOR), ChartLine(None, f, -own, OWN_COLOR)]
        standard = self.deviations.standard
        self.chart.draw(f"{standard.label or standard.id}: {q.name}", AXES[q.column], traces, lines, self.log_x.isChecked())


class KitCalibrationDialog(QDialog):
    """The calibration of a unit of a kit: the real values of its standards and what follows from them."""

    HEADERS = ["Patrón", "Fichero", "Resultado", "Desviación máxima", "Especificación propia (k = 2)"]

    def __init__(
        self,
        library: Library,
        model: EquipmentModel,
        unit_id: str,
        unit_folder: Path,
        units: dict[str, UnitStandard],
        calibration: Calibration | None = None,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.library, self.model, self.unit_id, self.unit_folder = library, model, unit_id, unit_folder
        self.units = {key: value.model_copy(deep=True) for key, value in units.items()}
        self.settings = library.kit_settings(model)
        self.result_units: dict[str, UnitStandard] | None = None
        self.result_calibration: Calibration | None = None
        self.setWindowTitle(f"Calibración del kit {unit_id}")
        self.resize(1240, 860)
        layout = QVBoxLayout(self)
        intro = QLabel(
            "Los valores reales de cada patrón (su certificado) se comparan con su definición: sus desviaciones dicen si "
            "cumple la especificación del fabricante y dan su especificación propia, por bandas, de la que sale su incertidumbre."
        )
        intro.setWordWrap(True)
        intro.setObjectName("pageSubtitle")
        layout.addWidget(intro)
        box = QGroupBox("Calibración")
        form = QFormLayout(box)
        self.date_edit = QLineEdit()
        self.date_edit.setPlaceholderText("AAAA-MM-DD")
        self.certificate = QLineEdit()
        self.laboratory = QLineEdit()
        self.band = QLineEdit(DEFAULT_BAND)
        self.band.setPlaceholderText("vacío: una sola banda")
        self.band.setToolTip("Las especificaciones propias toman, en cada banda, la mayor |desviación| + U")
        install_frequency_completion(self.band)
        self.band.editingFinished.connect(self._band_changed)
        row = QHBoxLayout()
        for label, widget in (("Fecha de calibración *", self.date_edit), ("Certificado", self.certificate), ("Laboratorio", self.laboratory)):
            row.addWidget(QLabel(label))
            row.addWidget(widget, 1)
        form.addRow(row)
        form.addRow("Ancho de las bandas", self.band)
        layout.addWidget(box)
        buttons = QHBoxLayout()
        add = QPushButton(icon("importar"), "Añadir ficheros…")
        add.setToolTip("Ficheros de valores reales: cada uno se asigna a su patrón por su nombre (OPENM… → abierto macho)")
        add.clicked.connect(self.add_files)
        choose = QPushButton(icon("abrir"), "Elegir fichero…")
        choose.setToolTip("El fichero de valores reales del patrón elegido")
        choose.clicked.connect(self.choose_file)
        remove = QPushButton(icon("eliminar"), "Quitar")
        remove.setToolTip("Quitar los valores reales del patrón elegido")
        remove.clicked.connect(self.remove_file)
        for button in (add, choose, remove):
            buttons.addWidget(button)
        buttons.addStretch(1)
        layout.addLayout(buttons)
        splitter = QSplitter(Qt.Orientation.Vertical)
        self.table = QTableWidget(0, len(self.HEADERS))
        self.table.setHorizontalHeaderLabels(self.HEADERS)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.itemSelectionChanged.connect(self._show_selected)
        splitter.addWidget(self.table)
        self.detail = DeviationChart()
        splitter.addWidget(self.detail)
        splitter.setSizes([300, 480])
        layout.addWidget(splitter, 1)
        bottom = QHBoxLayout()
        bottom.addStretch(1)
        cancel = QPushButton("Cancelar")
        cancel.clicked.connect(self.reject)
        accept = QPushButton("Aceptar")
        accept.setObjectName("primary")
        accept.clicked.connect(self._accept)
        bottom.addWidget(cancel)
        bottom.addWidget(accept)
        layout.addLayout(bottom)

        model_folder = model.source_path.parent if model.source_path else library.root / "catalogo" / "modelos"
        self.entries = [CalibrationEntry(ResolvedStandard(s, self.units.get(s.id), model_folder, unit_folder)) for s in model.standards]
        stored_dates = []
        for entry in self.entries:
            real = entry.resolved.real_values
            if real is not None:
                entry.path, entry.stored = entry.resolved.real_values_path(), True
                stored_dates.append(real.calibration_date)
                if real.band_width:
                    self.band.setText(format_frequency(real.band_width))
                self._compute(entry)
        when = calibration.issued if calibration and calibration.issued else next((d for d in stored_dates if d), None)
        self.date_edit.setText(when.isoformat() if when else "")
        if calibration is not None:
            self.certificate.setText(calibration.certificate or "")
            self.laboratory.setText(calibration.laboratory or "")
        self._fill(0)

    # ── files ──────────────────────────────────────────────────────────────
    def band_width(self) -> float | None:
        text = self.band.text().strip()
        if not text:
            return None
        try:
            width = parse_frequency(text)
        except UnitError as exc:
            raise ValueError(f"«Ancho de las bandas»: {exc}") from exc
        if width <= 0:
            raise ValueError("«Ancho de las bandas» debe ser mayor que 0 Hz")
        return width

    def _compute(self, entry: CalibrationEntry) -> None:
        entry.deviations, entry.error = None, None
        if entry.path is None:
            return
        try:
            entry.deviations = self.library.standard_deviations(entry.resolved, self.settings, entry.path)
        except (KitError, SDataError, OSError) as exc:
            entry.error = str(exc)

    def entry(self, standard_id: str) -> CalibrationEntry:
        return next(e for e in self.entries if e.resolved.id == standard_id)

    def set_file(self, standard_id: str, path: Path | None) -> None:
        entry = self.entry(standard_id)
        entry.path, entry.stored = (Path(path) if path else None), False
        self._compute(entry)

    def assign_files(self, paths: list[Path]) -> list[Path]:
        """Each file to the standard of its name; returns those that are not for one."""
        unassigned = []
        standards = [e.resolved.standard for e in self.entries if e.comparable]
        for path in paths:
            standard = match_standard(Path(path).name, standards)
            if standard is None:
                unassigned.append(Path(path))
            else:
                self.set_file(standard.id, path)
        self._fill(self._current())
        return unassigned

    def add_files(self) -> None:
        names, _ = QFileDialog.getOpenFileNames(self, "Valores reales de los patrones", str(self.library.root), FILE_FILTER)
        unassigned = self.assign_files([Path(n) for n in names])
        if unassigned:
            QMessageBox.information(
                self,
                "Valores reales",
                "No se sabe de qué patrón son (asígnelos con «Elegir fichero…»):\n" + "\n".join(p.name for p in unassigned),
            )

    def choose_file(self) -> None:
        index = self._current()
        if index is None:
            return
        name, _ = QFileDialog.getOpenFileName(self, "Valores reales del patrón", str(self.library.root), FILE_FILTER)
        if name:
            self.set_file(self.entries[index].resolved.id, Path(name))
            self._fill(index)

    def remove_file(self) -> None:
        index = self._current()
        if index is not None:
            self.set_file(self.entries[index].resolved.id, None)
            self._fill(index)

    def _band_changed(self) -> None:
        self._fill(self._current())

    # ── table and detail ───────────────────────────────────────────────────
    def _current(self) -> int | None:
        rows = {index.row() for index in self.table.selectedIndexes()}
        return rows.pop() if rows else None

    def _own(self, entry: CalibrationEntry) -> list:
        try:
            return entry.deviations.specifications(self.band_width()) if entry.deviations else []
        except ValueError:
            return []

    def row_texts(self, entry: CalibrationEntry) -> list[str]:
        standard = entry.resolved.standard
        name = standard.label or f"{standard.id} ({standard.gender_text})"
        if not entry.comparable:
            return [name, "—", "Sin definición por el modelo: no se compara", "", ""]
        if entry.path is None:
            return [name, "—", "", "", ""]
        file = entry.path.name + ("" if entry.stored else " (nuevo)")
        if entry.error:
            return [name, file, f"✖ {entry.error}", "", ""]
        deviations = entry.deviations
        worst = []
        for q in deviations.quantities:
            k = q.worst
            worst.append(f"{q.parameter} {SHORT[q.column]} {q.value_text(q.deviation[k])} ({format_frequency(deviations.frequencies[k])})")
        return [name, file, VERDICT_NAMES[deviations.verdict], " · ".join(worst), specifications_text(standard.type, self._own(entry))]

    def _fill(self, select: int | None = None) -> None:
        self.table.blockSignals(True)
        self.table.setRowCount(0)
        for entry in self.entries:
            row = self.table.rowCount()
            self.table.insertRow(row)
            for column, text in enumerate(self.row_texts(entry)):
                item = QTableWidgetItem(text)
                if column == 2 and entry.deviations is not None:
                    item.setForeground(QColor(VERDICT_COLORS[entry.deviations.verdict]))
                elif column == 2 and entry.error:
                    item.setForeground(QColor(VERDICT_COLORS["no_cumple"]))
                self.table.setItem(row, column, item)
        self.table.blockSignals(False)
        if select is not None and 0 <= select < len(self.entries):
            self.table.selectRow(select)
        self._show_selected()

    def _show_selected(self) -> None:
        index = self._current()
        if index is None:
            self.detail.show_deviations(None, message="Elija un patrón para ver sus desviaciones.")
            return
        entry = self.entries[index]
        if entry.deviations is None:
            message = entry.error or ("Sin valores reales: añada su fichero." if entry.comparable else "Sin definición por el modelo: no se compara.")
            self.detail.show_deviations(None, message=message)
            return
        self.detail.show_deviations(entry.deviations, self._own(entry))

    # ── result ─────────────────────────────────────────────────────────────
    def build(self) -> tuple[dict[str, UnitStandard], Calibration]:
        """The standards of the unit with their real values (the files are copied into the library) and the calibration."""
        when = _date(self.date_edit.text())
        if when is None:
            raise ValueError("Indique la fecha de calibración del kit")
        band = self.band_width()
        failed = [e.resolved.standard.label or e.resolved.id for e in self.entries if e.path is not None and e.error]
        if failed:
            raise ValueError(f"Los valores reales de {', '.join(failed)} no se pueden usar: quítelos o elija otro fichero")
        units = {key: value.model_copy(deep=True) for key, value in self.units.items()}
        for entry in self.entries:
            standard_id = entry.resolved.id
            unit = units.get(standard_id) or UnitStandard()
            if entry.path is None or entry.deviations is None:
                unit.real_values = None
            else:
                unit.real_values = self.library.kit_real_values(
                    entry.resolved, self.settings, entry.path, self.unit_id, when, band, self.certificate.text().strip() or None, entry.deviations
                )
            if unit.empty:
                units.pop(standard_id, None)
            else:
                units[standard_id] = unit
        calibration = Calibration(certificate=self.certificate.text().strip() or None, laboratory=self.laboratory.text().strip() or None, issued=when)
        return units, calibration

    def _accept(self) -> None:
        try:
            self.result_units, self.result_calibration = self.build()
        except (ValueError, KitError, OSError) as exc:
            QMessageBox.warning(self, self.windowTitle(), str(exc))
            return
        self.accept()
