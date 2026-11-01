"""The covariance and the correlation of S-parameter data as a colour map, like the «Covariance» view of
the Data Explorer of METAS VNA Tools (``sdata/correlation.py``).

* «Entre frecuencias»: a quantity of an S-parameter (real or imaginary part, magnitude or phase) at the
  frequencies of a range: a matrix of ±1 is a deviation shared by all of them (one input of the budget),
  a diagonal one, frequencies with their own inputs.
* «En una frecuencia»: the real and imaginary parts (or magnitude and phase) of all the S-parameters of a
  frequency, in the order of a ``.sdatcv``.

The cells without a correlation (a quantity without uncertainty, or the magnitude and phase where S = 0)
are grey. Under the pointer: the two quantities, their value and their standard uncertainties.
"""

from __future__ import annotations

import math

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QGuiApplication, QPen
from PySide6.QtWidgets import (
    QComboBox,
    QGraphicsRectItem,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from calelektra.core.units import UnitError, format_frequency, parse_frequency
from calelektra.gui.frequency_input import install_frequency_completion
from calelektra.sdata import SDataError, SParamData
from calelektra.sdata.correlation import (
    COMPONENTS,
    POINT_COMPONENTS,
    CovarianceMatrix,
    frequency_matrix,
    nearest_point,
    parameters,
    point_matrix,
)

SCOPES = {"frecuencias": "Entre frecuencias", "punto": "En una frecuencia"}
KINDS = {"correlacion": "Correlación", "covarianza": "Covarianza"}
DIVERGING = pg.ColorMap([0.0, 0.5, 1.0], [(33, 102, 172), (247, 247, 247), (178, 24, 43)])  # −1 blue, 0 white, +1 red
UNDEFINED_COLOR = "#c9ced6"  # quantities without a correlation
MAX_VALUES = 16  # matrices up to this size show their values in the cells
MAX_COPY = 250_000  # values of a matrix that can be copied
FULL = 1 - 1e-9  # |ρ| from which a correlation is total


def _number(value: float, digits: int = 3) -> str:
    return f"{value:.{digits}f}".replace(".", ",")


def _scientific(value: float) -> str:
    return f"{value:.3g}".replace(".", ",")


def frequency_ticks(frequencies: np.ndarray, count: int = 6) -> list[tuple[float, str]]:
    """Ticks of an axis that has a cell per frequency: round frequencies at their (fractional) cell."""
    f = np.asarray(frequencies, float)
    n = len(f)
    if n == 0:
        return []
    if n == 1 or np.any(np.diff(f) <= 0):
        step = max(1, n // count)
        return [(i + 0.5, format_frequency(f[i], 4)) for i in range(0, n, step)]
    low, high = f[0], f[-1]
    ratios = f[1:] / f[:-1] if low > 0 else None
    logarithmic = ratios is not None and n > 2 and np.ptp(ratios) < 1e-3 * ratios.mean() and np.ptp(np.diff(f)) > 1e-3 * np.diff(f).mean()
    if logarithmic:
        decades = range(math.floor(math.log10(low)), math.ceil(math.log10(high)) + 1)
        values = [m * 10.0**e for e in decades for m in (1, 2, 5)]
        values = [v for v in values if low <= v <= high]
        if len(values) > 2 * count:
            values = [v for v in values if round(v / 10 ** math.floor(math.log10(v))) == 1]
    else:
        raw = (high - low) / count
        magnitude = 10 ** math.floor(math.log10(raw))
        step = next(m * magnitude for m in (1, 2, 2.5, 5, 10) if m * magnitude >= raw)
        values = list(np.arange(math.ceil(low / step) * step, high + step * 1e-9, step))
    if not values:
        values = [low, high]
    return [(float(np.interp(v, f, np.arange(n))) + 0.5, format_frequency(v, 4)) for v in values]


class CorrelationView(QWidget):
    """The covariance or the correlation of some S-parameter data with uncertainty, as a colour map."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.data: SParamData | None = None
        self.matrix: CovarianceMatrix | None = None
        self.values: np.ndarray | None = None  # what the colour map shows: the correlation or the covariance
        self._stale = False  # new data, computed when the view is shown
        self._cells: list = []  # values and separators drawn on small matrices
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 4, 0, 0)
        controls = QHBoxLayout()
        self.scope = QComboBox()
        for key, text in SCOPES.items():
            self.scope.addItem(text, key)
        self.scope.setToolTip("Entre frecuencias: una magnitud de un parámetro en todas las frecuencias del rango.\n"
                              "En una frecuencia: todos los parámetros, en el orden de un .sdatcv.")
        self.parameter = QComboBox()
        self.parameter.setToolTip("Parámetro S")
        self.component = QComboBox()
        self.kind = QComboBox()
        for key, text in KINDS.items():
            self.kind.addItem(text, key)
        for widget in (self.scope, self.parameter, self.component, self.kind):
            widget.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
            controls.addWidget(widget)
        controls.addStretch(1)
        layout.addLayout(controls)
        ranges = QHBoxLayout()
        self.start, self.stop, self.at = QLineEdit(), QLineEdit(), QLineEdit()
        self.start_label, self.stop_label, self.at_label = QLabel("Desde"), QLabel("hasta"), QLabel("Frecuencia")
        for edit in (self.start, self.stop, self.at):
            edit.setMaximumWidth(130)
            install_frequency_completion(edit)
            edit.editingFinished.connect(self.redraw)
        self.at.setToolTip("Se usa la frecuencia de los datos más cercana")
        for widget in (self.start_label, self.start, self.stop_label, self.stop, self.at_label, self.at):
            ranges.addWidget(widget)
        ranges.addStretch(1)
        self.copy_button = QPushButton("Copiar matriz")
        self.copy_button.setToolTip("Copia la matriz al portapapeles (separada por tabuladores, con punto decimal)")
        self.copy_button.clicked.connect(self.copy_matrix)
        ranges.addWidget(self.copy_button)
        layout.addLayout(ranges)
        self.plot = pg.PlotWidget()
        self.plot.setMenuEnabled(False)
        self.plot.setAspectLocked(True)
        self.plot.invertY(True)
        self.plot.getAxis("left").setWidth(78)  # the widest labels from the start: «12,5 GHz», «S22 |S|»
        self.backdrop = QGraphicsRectItem(0, 0, 1, 1)
        self.backdrop.setBrush(QBrush(QColor(UNDEFINED_COLOR)))
        self.backdrop.setPen(QPen(Qt.PenStyle.NoPen))
        self.backdrop.setZValue(-10)
        self.plot.addItem(self.backdrop)
        self.image = pg.ImageItem(axisOrder="row-major")
        self.plot.addItem(self.image)
        self.bar = pg.ColorBarItem(values=(-1, 1), colorMap=DIVERGING, interactive=False, label="ρ", colorMapMenu=False)
        self.bar.setImageItem(self.image, insert_in=self.plot.getPlotItem())
        layout.addWidget(self.plot, 1)
        self.readout = QLabel(" ")
        self.readout.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.readout)
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        self.summary.setObjectName("pageSubtitle")
        layout.addWidget(self.summary)
        self.plot.scene().sigMouseMoved.connect(self._hover)
        self.scope.currentIndexChanged.connect(self._scope_changed)
        self.parameter.currentIndexChanged.connect(lambda _i: self.redraw())
        self.component.currentIndexChanged.connect(lambda _i: self.redraw())
        self.kind.currentIndexChanged.connect(lambda _i: self._draw())
        self._scope_changed()

    # ── data ───────────────────────────────────────────────────────────────
    def set_data(self, data: SParamData | None) -> None:
        """New data: the matrix is computed now if the view is visible, else when it is shown."""
        self.data = data
        self._stale = False
        if data is None:
            self._clear("Sin datos")
            return
        current = self.parameter.currentText()
        self.parameter.blockSignals(True)
        self.parameter.clear()
        for name, _receiver, _source in parameters(data):
            self.parameter.addItem(name)
        self.parameter.setCurrentIndex(max(self.parameter.findText(current), 0))
        self.parameter.blockSignals(False)
        f = data.frequencies
        if len(f):
            self.start.setPlaceholderText(format_frequency(f[0]))
            self.stop.setPlaceholderText(format_frequency(f[-1]))
            self.at.setPlaceholderText(format_frequency(f[0]))
        if self.isVisible():
            self.redraw()
        else:
            self._stale = True
            self.summary.setText("Se calcula al abrir esta pestaña")

    def showEvent(self, event) -> None:  # noqa: N802 (Qt API)
        super().showEvent(event)
        if self._stale:
            self.redraw()

    def _scope_changed(self) -> None:
        between = self.scope.currentData() == "frecuencias"
        self.component.blockSignals(True)
        current = self.component.currentData()
        self.component.clear()
        for key, text in (COMPONENTS if between else POINT_COMPONENTS).items():
            self.component.addItem(text, key)
        self.component.setCurrentIndex(max(self.component.findData(current), 0))
        self.component.blockSignals(False)
        self.parameter.setVisible(between)
        for widget in (self.start_label, self.start, self.stop_label, self.stop):
            widget.setVisible(between)
        self.at_label.setVisible(not between)
        self.at.setVisible(not between)
        self.redraw()

    @staticmethod
    def _frequency(edit: QLineEdit, label: str) -> float | None:
        text = edit.text().strip()
        if not text:
            return None
        try:
            return parse_frequency(text)
        except UnitError as exc:
            raise ValueError(f"«{label}»: {exc}") from exc

    def redraw(self) -> None:
        """Recompute the matrix of the current choice."""
        if self.data is None:
            return
        self._stale = False
        try:
            if self.scope.currentData() == "frecuencias":
                matrix = frequency_matrix(
                    self.data, self.parameter.currentText(), self.component.currentData(),
                    self._frequency(self.start, "Desde"), self._frequency(self.stop, "Hasta"),
                )
            else:
                frequency = self._frequency(self.at, "Frecuencia")
                point = 0 if frequency is None else nearest_point(self.data.frequencies, frequency)
                matrix = point_matrix(self.data, point, self.component.currentData())
        except (SDataError, ValueError) as exc:
            self._clear(f"✖ {exc}")
            return
        self.matrix = matrix
        self._draw()

    # ── drawing ────────────────────────────────────────────────────────────
    def _clear(self, text: str) -> None:
        self.matrix = self.values = None
        self.image.clear()
        self.backdrop.setRect(0, 0, 0, 0)
        self._remove_cells()
        for side in ("bottom", "left"):
            self.plot.getAxis(side).setTicks(None)
        self.plot.setLabel("bottom", "")
        self.readout.setText(" ")
        self.summary.setText(text)
        self.copy_button.setEnabled(False)

    def _remove_cells(self) -> None:
        for item in self._cells:
            self.plot.removeItem(item)
        self._cells = []

    def _draw(self) -> None:
        matrix = self.matrix
        if matrix is None:
            return
        kind = self.kind.currentData()
        values = self.values = matrix.values(kind)
        n = matrix.size
        if kind == "correlacion":
            levels, label = (-1.0, 1.0), "ρ"
        else:
            finite = values[np.isfinite(values)]
            top = float(np.abs(finite).max()) if finite.size else 0.0
            levels = (-(top or 1.0), top or 1.0)
            unit = matrix.unit
            label = "Covarianza" + (f" ({unit}²)" if unit else "")
        self.image.setImage(values, autoLevels=False)
        self.image.setRect(QRectF(0, 0, n, n))
        self.bar.setLevels(levels)
        self.bar.getAxis("left").setLabel(label)  # that of the bar (its ticks are on the right)
        self.backdrop.setRect(0, 0, n, n)
        self._remove_cells()
        between = self.scope.currentData() == "frecuencias"
        ticks = frequency_ticks(matrix.frequencies) if between else [(i + 0.5, text) for i, text in enumerate(matrix.labels)]
        for side in ("bottom", "left"):
            self.plot.getAxis(side).setTicks([ticks, []])
        self.plot.setLabel("bottom", "Frecuencia" if between else "")
        if not between:
            self._draw_cells(values, levels)
        self.plot.getPlotItem().getViewBox().setRange(QRectF(0, 0, n, n), padding=0.02)
        self.readout.setText(" ")
        self.summary.setText(self._summary_text())
        self.copy_button.setEnabled(True)

    def _draw_cells(self, values: np.ndarray, levels: tuple[float, float]) -> None:
        """The values in the cells of a small matrix and a line between the parameters."""
        n = len(values)
        if n <= MAX_VALUES:
            for i in range(n):
                for j in range(n):
                    value = values[i, j]
                    if math.isnan(value):
                        continue
                    strong = abs(value) > 0.6 * levels[1]
                    text = _number(value, 2) if self.kind.currentData() == "correlacion" else _scientific(value)
                    item = pg.TextItem(text, color="w" if strong else "#2c3e50", anchor=(0.5, 0.5))
                    item.setPos(j + 0.5, i + 0.5)
                    self.plot.addItem(item)
                    self._cells.append(item)
        pen = pg.mkPen("w", width=2)
        for k in range(2, n, 2):  # two quantities per parameter
            for line in (pg.PlotCurveItem([k, k], [0, n], pen=pen), pg.PlotCurveItem([0, n], [k, k], pen=pen)):
                self.plot.addItem(line)
                self._cells.append(line)

    def _summary_text(self) -> str:
        matrix = self.matrix
        if matrix is None:
            return ""
        parts = []
        if self.scope.currentData() == "frecuencias":
            what = f"{self.parameter.currentText()}, {self.component.currentText().lower()}"
            shown = f"{matrix.size} frecuencias" if matrix.size == matrix.total else f"{matrix.size} de las {matrix.total} frecuencias del rango (equiespaciadas)"
            parts.append(f"{what}: {shown}, {format_frequency(matrix.frequencies[0])} – {format_frequency(matrix.frequencies[-1])}")
            between = "frecuencias distintas"
        else:
            parts.append(f"{format_frequency(matrix.frequencies[0])}: {self.component.currentText().lower()} de cada parámetro, en el orden de un .sdatcv")
            between = "magnitudes distintas"
        invalid = int((~matrix.valid).sum())
        off = matrix.off_diagonal()
        if not matrix.valid.any():
            undefined = not matrix.defined.any()
            parts.append("el módulo y la fase no están definidos donde S = 0" if undefined else "sin incertidumbre: no hay correlación que mostrar")
        elif off.size:
            low, high = float(off.min()), float(off.max())
            parts.append(f"correlación entre {between}: de {_number(low)} a {_number(high)}")
            if self.scope.currentData() == "frecuencias":
                if np.all(np.abs(off) >= FULL):
                    parts.append("total (±1): una desviación común a todas las frecuencias")
                elif np.all(np.abs(off) <= 1e-9):
                    parts.append("nula: cada frecuencia depende de sus propias entradas")
        if invalid and matrix.valid.any():
            parts.append(f"{invalid} sin correlación (en gris): sin incertidumbre" + (" o donde S = 0" if self.component.currentData() in ("modulo", "fase", "polar") else ""))
        return " · ".join(parts)

    # ── pointer and clipboard ──────────────────────────────────────────────
    def cell_text(self, row: int, column: int) -> str:
        """What the readout says of a cell."""
        matrix, values = self.matrix, self.values
        if matrix is None or values is None or not (0 <= row < matrix.size and 0 <= column < matrix.size):
            return " "
        value = values[row, column]
        if math.isnan(value):
            shown = "sin correlación (sin incertidumbre o no definida)"
        elif self.kind.currentData() == "correlacion":
            shown = f"ρ = {_number(value, 4)}"
        else:
            shown = f"covarianza = {_scientific(value)}"
        u = matrix.standard_uncertainties

        def uncertainty(k: int) -> str:
            return _scientific(u[k]) + matrix.units[k]

        return f"{matrix.labels[row]} × {matrix.labels[column]}: {shown} · u = {uncertainty(row)} y {uncertainty(column)} (k = 1)"

    def _hover(self, position: QPointF) -> None:
        if self.matrix is None:
            return
        point = self.plot.getPlotItem().getViewBox().mapSceneToView(position)
        self.readout.setText(self.cell_text(math.floor(point.y()), math.floor(point.x())))

    def matrix_text(self) -> str:
        return self.matrix.text(self.kind.currentData()) if self.matrix is not None else ""

    def copy_matrix(self) -> None:
        if self.matrix is None:
            return
        if self.matrix.size**2 > MAX_COPY:
            QMessageBox.information(self, "Copiar matriz", f"La matriz tiene {self.matrix.size} × {self.matrix.size} valores: "
                                    "reduzca el rango de frecuencias para copiarla.")
            return
        QGuiApplication.clipboard().setText(self.matrix_text())
