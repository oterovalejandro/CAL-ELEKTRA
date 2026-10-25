"""Charts of S-parameters (pyqtgraph) and the dialogs that use them.

A directional coupler is shown in two charts of losses (``CouplerPlots``):
the return loss (RL) of each port and the transmission loss (TL) of each
measured pair; ``ResultPlots`` shows one chart or the other after the
algorithm of the test.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QSplitter,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from calelektra.catalog.library import Library
from calelektra.catalog.model import Equipment
from calelektra.core.networks import gamma_to_impedance
from calelektra.core.touchstone import TouchstoneError, read_touchstone
from calelektra.core.units import UnitError, parse_frequency
from calelektra.gui import style
from calelektra.instruments.base import InstrumentError
from calelektra.instruments.dummy_vna import DummyVna
from calelektra.instruments.factory import open_vna
from calelektra.instruments.simulation import touchstone_model
from calelektra.procedures.coupler import (
    pair_ports,
    return_loss_sources,
    transmission_symbol,
)
from calelektra.procedures.model import COUPLER_PORTS
from calelektra.procedures.processing import smooth
from calelektra.procedures.specs import SpecCurve
from calelektra.uncertainty.traces import TraceUncertainty

TRACE_COLORS = {"S11": "#2b7bb9", "S21": "#c0392b", "S12": "#d98a00", "S22": "#2e8b57"}
DATASET_COLORS = ["#2b7bb9", "#c0392b", "#2e8b57", "#d98a00", "#7b5ea7", "#4f8a8b", "#8d6e63"]
# several 2-port data sets (e.g. the port pairs of a coupler): one colour per data set, one line style per parameter
TRACE_STYLES = {"S21": Qt.PenStyle.SolidLine, "S12": Qt.PenStyle.DashDotLine, "S11": Qt.PenStyle.DashLine, "S22": Qt.PenStyle.DotLine}
PORT_COLORS = {"ENTRADA": "#2b7bb9", "SALIDA": "#c0392b", "ACOPLADO": "#2e8b57", "AISLADO": "#d98a00"}
MODES = {
    "db": ("Magnitud", "dB"),
    "fase": ("Fase", "°"),
    "z": ("Impedancia |Z|", "Ω"),
}

BAND_ALPHA = 60  # opacity (0–255) of the uncertainty band of a trace

pg.setConfigOptions(antialias=True, background="w", foreground="#2c3e50")


def _db(values: np.ndarray) -> np.ndarray:
    with np.errstate(divide="ignore"):
        return 20 * np.log10(np.maximum(np.abs(values), 1e-15))


def band_segments(y: np.ndarray, u: np.ndarray, wrap: bool = False) -> list[slice]:
    """Runs of points where the band can be drawn: ``u`` finite and, for a wrapped phase, no ±180° jump."""
    valid = np.isfinite(y) & np.isfinite(u)
    breaks = np.zeros(len(y), dtype=bool)  # True: the point does not connect to the previous one
    if wrap and len(y) > 1:
        breaks[1:] = np.abs(np.diff(y)) > 180
    segments, start = [], None
    for k in range(len(y)):
        if not valid[k] or (breaks[k] and start is not None):
            if start is not None and k - start >= 2:
                segments.append(slice(start, k))
            start = k if valid[k] else None
        elif start is None:
            start = k
    if start is not None and len(y) - start >= 2:
        segments.append(slice(start, len(y)))
    return segments


def add_band(plot: pg.PlotWidget, x: np.ndarray, y: np.ndarray, u: np.ndarray, color: str, wrap: bool = False) -> None:
    """Shaded zone y ± u behind the traces (``x`` in view coordinates: log10 with a logarithmic axis)."""
    brush = pg.mkColor(color)
    brush.setAlpha(BAND_ALPHA)
    for segment in band_segments(y, u, wrap):
        upper = pg.PlotCurveItem(x[segment], (y + u)[segment])
        lower = pg.PlotCurveItem(x[segment], (y - u)[segment])
        fill = pg.FillBetweenItem(upper, lower, brush=brush, pen=pg.mkPen(None))
        fill.setZValue(-5)
        plot.addItem(fill)


class SParameterPlot(QWidget):
    """Magnitude, phase or impedance of one or several S-parameter data sets, with specification lines."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.datasets: dict[str, tuple[np.ndarray, np.ndarray]] = {}
        self.visible: dict[str, bool] = {}
        self.limits: list[SpecCurve] = []
        self.smoothing: dict[str, float] = {}  # trace (S11…) → % of the points; display only
        self.uncertainty: dict[str, dict[str, tuple[np.ndarray, np.ndarray]]] = {}  # data set → trace → (U dB, U °)
        self.coverage_factor = 2.0
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        controls = QHBoxLayout()
        self.mode = QComboBox()
        for key, (name, unit) in MODES.items():
            self.mode.addItem(f"{name} ({unit})", key)
        self.mode.currentIndexChanged.connect(lambda _i: self.redraw())
        self.log_x = QCheckBox("Frecuencia logarítmica")
        self.log_x.toggled.connect(lambda _on: self.redraw())
        self.show_limits = QCheckBox("Especificaciones")
        self.show_limits.setChecked(True)
        self.show_limits.setToolTip("Curvas de los ficheros de especificación (pasa / falla); discontinuas")
        self.show_limits.toggled.connect(lambda _on: self.redraw())
        self.show_limits.setVisible(False)
        self.smooth_box = QCheckBox("Suavizado")
        self.smooth_box.setChecked(True)
        self.smooth_box.setToolTip("Suavizado de la plantilla de ensayo: solo cambia la visualización, no los datos")
        self.smooth_box.toggled.connect(lambda _on: self.redraw())
        self.smooth_box.setVisible(False)
        self.uncertainty_box = QCheckBox("Incertidumbre")
        self.uncertainty_box.setChecked(True)
        self.uncertainty_box.setToolTip(
            "Zona sombreada: valor ± incertidumbre expandida de cada punto, calculada con el fichero de incertidumbres del VNA"
        )
        self.uncertainty_box.toggled.connect(lambda _on: self.redraw())
        self.uncertainty_box.setVisible(False)
        controls.addWidget(self.mode)
        controls.addWidget(self.log_x)
        controls.addWidget(self.show_limits)
        controls.addWidget(self.smooth_box)
        controls.addWidget(self.uncertainty_box)
        controls.addStretch(1)
        layout.addLayout(controls)
        traces = QHBoxLayout()
        self.trace_boxes = QHBoxLayout()
        traces.addLayout(self.trace_boxes)
        traces.addStretch(1)
        layout.addLayout(traces)
        self.plot = pg.PlotWidget()
        self.plot.showGrid(x=True, y=True, alpha=0.25)
        self.plot.setLabel("bottom", "Frecuencia", units="Hz")
        self.legend = self.plot.addLegend(offset=(10, 10))
        layout.addWidget(self.plot, 1)
        self.empty = pg.TextItem("Sin datos", color="#8a94a3", anchor=(0.5, 0.5))
        self.redraw()

    def clear(self) -> None:
        self.datasets.clear()
        self.visible.clear()
        self.limits = []
        self.smoothing = {}
        self.uncertainty = {}
        self.redraw()

    def set_uncertainty(self, name: str, traces: Mapping[str, Any], coverage_factor: float = 2.0) -> None:
        """Expanded uncertainty of the traces of data set ``name``: ``TraceUncertainty`` or (U dB, U °) per trace."""
        bands = {}
        for parameter, trace in traces.items():
            if isinstance(trace, TraceUncertainty):
                bands[parameter] = (trace.magnitude_db, trace.phase_deg)
            else:
                bands[parameter] = (np.asarray(trace[0], dtype=float), np.asarray(trace[1], dtype=float))
        self.uncertainty[name] = bands
        self.coverage_factor = coverage_factor
        self.uncertainty_box.setText(f"Incertidumbre (k={coverage_factor:g})")
        self.redraw()

    def set_smoothing(self, smoothing: dict[str, float]) -> None:
        """Display smoothing per trace (% of the points); the data sets are not changed."""
        self.smoothing = {name: pct for name, pct in smoothing.items() if pct > 0}
        details = ", ".join(f"{name} {pct:g} %" for name, pct in self.smoothing.items())
        self.smooth_box.setText(f"Suavizado ({details})" if details else "Suavizado")
        self.redraw()

    def set_limits(self, curves: list[SpecCurve]) -> None:
        self.limits = list(curves)
        self.redraw()

    def set_data(self, name: str, frequencies: np.ndarray, s: np.ndarray, log_x: bool | None = None) -> None:
        if s.ndim == 1:
            s = s[:, None, None]
        self.datasets[name] = (np.asarray(frequencies, dtype=float), np.asarray(s))
        if log_x is not None:
            self.log_x.blockSignals(True)
            self.log_x.setChecked(bool(log_x))
            self.log_x.blockSignals(False)
        self.redraw()

    def _traces(self) -> list[tuple[str, str, str, np.ndarray, np.ndarray, str]]:
        """(trace key, data set, parameter, f, values, colour) of every parameter of every data set."""
        traces = []
        several = len(self.datasets) > 1
        for index, (dataset, (f, s)) in enumerate(self.datasets.items()):
            n = s.shape[1]
            for i in range(n):
                for j in range(n):
                    if not np.isfinite(s[:, i, j]).any():
                        continue  # not measured (e.g. all but S21 in the transmission test)
                    name = f"S{i + 1}{j + 1}"
                    label = f"{dataset} · {name}" if several else name
                    color = DATASET_COLORS[index % len(DATASET_COLORS)] if several else TRACE_COLORS.get(name, "#555")
                    traces.append((label, dataset, name, f, s[:, i, j], color))
        return traces

    def _band(self, dataset: str, parameter: str, mode: str, points: int) -> np.ndarray | None:
        bands = self.uncertainty.get(dataset, {}).get(parameter)
        if bands is None or mode not in ("db", "fase"):
            return None
        u = bands[0] if mode == "db" else bands[1]
        return u if len(u) == points else None

    def redraw(self) -> None:
        self.plot.clear()
        self.legend.clear()
        while self.trace_boxes.count():
            widget = self.trace_boxes.takeAt(0).widget()
            if widget:
                widget.deleteLater()
        mode = self.mode.currentData()
        name, unit = MODES[mode]
        self.plot.setLabel("left", name, units=unit if mode != "db" else None)
        if mode == "db":
            self.plot.setLabel("left", "Magnitud (dB)")
        traces = self._traces()
        if not traces:
            self.plot.addItem(self.empty)
            self.empty.setPos(0.5, 0.5)
            return
        log_x = self.log_x.isChecked() and all(f[0] > 0 for f, _ in self.datasets.values())
        self.plot.setLogMode(x=log_x, y=False)
        self.smooth_box.setVisible(bool(self.smoothing))
        smoothing = self.smoothing if self.smooth_box.isChecked() else {}
        self.uncertainty_box.setVisible(bool(self.uncertainty) and mode in ("db", "fase"))
        bands = self.uncertainty_box.isChecked()
        for label, dataset, parameter, f, values, color in traces:
            if parameter in smoothing:
                values = smooth(values, smoothing[parameter])
            reflection = parameter[1] == parameter[2]
            if mode == "z" and not reflection:
                continue
            self.visible.setdefault(label, True)
            box = QCheckBox(label)
            box.setChecked(self.visible[label])
            box.setStyleSheet(f"QCheckBox {{ color: {color}; font-weight: 600; }}")
            box.toggled.connect(lambda on, key=label: self._toggle(key, on))
            self.trace_boxes.addWidget(box)
            if not self.visible[label]:
                continue
            if mode == "db":
                y = _db(values)
            elif mode == "fase":
                y = np.degrees(np.angle(values))
            else:
                with np.errstate(divide="ignore", invalid="ignore"):
                    y = np.abs(gamma_to_impedance(values))
            x = np.maximum(f, 1e-3)
            u = self._band(dataset, parameter, mode, len(y)) if bands else None
            if u is not None:
                add_band(self.plot, np.log10(x) if log_x else x, y, u, color, wrap=mode == "fase")  # view coordinates
            style = TRACE_STYLES.get(parameter, Qt.PenStyle.SolidLine) if len(self.datasets) > 1 else Qt.PenStyle.SolidLine
            self.plot.plot(x, y, pen=pg.mkPen(color, width=2, style=style), name=label)
        self._draw_limits(mode, log_x)

    def _draw_limits(self, mode: str, log_x: bool) -> None:
        self.show_limits.setVisible(bool(self.limits))
        quantity = {"db": "magnitud_db", "fase": "fase_grados"}.get(mode)
        if not self.limits or quantity is None or not self.show_limits.isChecked():
            return
        low = min((f[0] for f, _ in self.datasets.values()), default=None)
        high = max((f[-1] for f, _ in self.datasets.values()), default=None)
        names = {curve.name for curve in self.limits}
        for curve in self.limits:
            if curve.quantity != quantity:
                continue
            f, values = curve.frequencies, curve.values
            if low is not None and high is not None:
                inside = (f >= low * (1 - 1e-9)) & (f <= high * (1 + 1e-9))
                f, values = f[inside], values[inside]
            if log_x:
                keep = f > 0
                f, values = f[keep], values[keep]
            if not len(f):
                continue
            if quantity == "fase_grados":
                values = (values + 180.0) % 360.0 - 180.0
            pen = pg.mkPen(TRACE_COLORS.get(curve.parameter, "#555"), width=1.6, style=Qt.PenStyle.DashLine)
            label = f"Espec. {curve.parameter}" + (f" ({curve.name})" if len(names) > 1 else "")
            if curve.criterion == "tolerancia" and curve.tolerance:
                self.plot.plot(f, values + curve.tolerance, pen=pen, name=f"{label} ± {curve.tolerance:g}")
                self.plot.plot(f, values - curve.tolerance, pen=pen)
            else:
                self.plot.plot(f, values, pen=pen, name=label)

    def _toggle(self, key: str, on: bool) -> None:
        self.visible[key] = on
        self.redraw()


@dataclass
class LossTrace:
    label: str
    frequencies: np.ndarray
    losses: np.ndarray  # dB, positive: −20·log10|S|
    uncertainty: np.ndarray | None  # expanded uncertainty (dB) of each point
    color: str
    source: str  # where it comes from (tooltip)


class LossChart(QWidget):
    """One chart of losses (dB, positive) with a check box per trace."""

    def __init__(self, title: str, axis: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.visible: dict[str, bool] = {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        heading = QLabel(f"<b>{title}</b>")
        layout.addWidget(heading)
        traces = QHBoxLayout()
        self.trace_boxes = QHBoxLayout()
        traces.addLayout(self.trace_boxes)
        traces.addStretch(1)
        layout.addLayout(traces)
        self.plot = pg.PlotWidget()
        self.plot.showGrid(x=True, y=True, alpha=0.25)
        self.plot.setLabel("bottom", "Frecuencia", units="Hz")
        self.plot.setLabel("left", axis)
        self.legend = self.plot.addLegend(offset=(10, 10))
        layout.addWidget(self.plot, 1)
        self.empty = pg.TextItem("Sin datos", color="#8a94a3", anchor=(0.5, 0.5))
        self.traces: list[LossTrace] = []
        self.log_x = False
        self.bands = True
        self.draw([], False, False)

    def labels(self) -> list[str]:
        return [self.trace_boxes.itemAt(i).widget().text() for i in range(self.trace_boxes.count())]

    def draw(self, traces: list[LossTrace], log_x: bool, bands: bool) -> None:
        self.traces, self.log_x, self.bands = traces, log_x, bands
        self.plot.clear()
        self.legend.clear()
        while self.trace_boxes.count():
            widget = self.trace_boxes.takeAt(0).widget()
            if widget:
                widget.deleteLater()
        if not traces:
            self.plot.setLogMode(x=False, y=False)
            self.plot.addItem(self.empty)
            self.empty.setPos(0.5, 0.5)
            return
        self.plot.setLogMode(x=log_x, y=False)
        for trace in traces:
            self.visible.setdefault(trace.label, True)
            box = QCheckBox(trace.label)
            box.setChecked(self.visible[trace.label])
            box.setToolTip(trace.source)
            box.setStyleSheet(f"QCheckBox {{ color: {trace.color}; font-weight: 600; }}")
            box.toggled.connect(lambda on, key=trace.label: self._toggle(key, on))
            self.trace_boxes.addWidget(box)
            if not self.visible[trace.label]:
                continue
            x = np.maximum(trace.frequencies, 1e-3)
            u = trace.uncertainty if bands else None
            if u is not None and len(u) == len(trace.losses):
                add_band(self.plot, np.log10(x) if log_x else x, trace.losses, u, trace.color)
            self.plot.plot(x, trace.losses, pen=pg.mkPen(trace.color, width=2), name=trace.label)

    def _toggle(self, key: str, on: bool) -> None:
        self.visible[key] = on
        self.draw(self.traces, self.log_x, self.bands)


class CouplerPlots(QWidget):
    """Directional coupler: return loss (RL) of each port and transmission loss (TL) of each measured pair.

    The data sets are the measured pairs (``ENTRADA-SALIDA``…: first port on
    P1, second on P2). The RL of ENTRADA and SALIDA comes from the
    ENTRADA–SALIDA pair (each of them is measured in two pairs) and that of
    the other ports from the first pair that measures them
    (``procedures/coupler.py``); the TL of a pair is −|S21|. Same interface
    as ``SParameterPlot`` for the test window and the explorer.
    """

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.datasets: dict[str, tuple[np.ndarray, np.ndarray]] = {}
        self.limits: list[SpecCurve] = []  # S2P specifications do not apply: the coupler is evaluated with its nominal values
        self.smoothing: dict[str, float] = {}
        self.uncertainty: dict[str, dict[str, tuple[np.ndarray, np.ndarray]]] = {}
        self.coverage_factor = 2.0
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        controls = QHBoxLayout()
        self.log_x = QCheckBox("Frecuencia logarítmica")
        self.log_x.toggled.connect(lambda _on: self.redraw())
        self.smooth_box = QCheckBox("Suavizado")
        self.smooth_box.setChecked(True)
        self.smooth_box.setToolTip("Suavizado de la plantilla de ensayo: solo cambia la visualización, no los datos")
        self.smooth_box.toggled.connect(lambda _on: self.redraw())
        self.smooth_box.setVisible(False)
        self.uncertainty_box = QCheckBox("Incertidumbre")
        self.uncertainty_box.setChecked(True)
        self.uncertainty_box.setToolTip(
            "Zona sombreada: valor ± incertidumbre expandida de cada punto (la del parámetro S del que sale cada traza)"
        )
        self.uncertainty_box.toggled.connect(lambda _on: self.redraw())
        self.uncertainty_box.setVisible(False)
        for widget in (self.log_x, self.smooth_box, self.uncertainty_box):
            controls.addWidget(widget)
        controls.addStretch(1)
        layout.addLayout(controls)
        self.return_loss = LossChart("Pérdidas de retorno (RL) de cada puerto", "RL (dB)")
        self.transmission = LossChart("Pérdidas de transmisión (TL) de cada medida", "TL (dB)")
        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.addWidget(self.return_loss)
        splitter.addWidget(self.transmission)
        layout.addWidget(splitter, 1)
        self.redraw()

    def sizeHint(self) -> QSize:  # noqa: N802 (Qt API)
        return QSize(super().sizeHint().width(), 640)  # both charts in view in the explorer

    def clear(self) -> None:
        self.datasets.clear()
        self.limits = []
        self.smoothing = {}
        self.uncertainty = {}
        self.redraw()

    def set_data(self, name: str, frequencies: np.ndarray, s: np.ndarray, log_x: bool | None = None) -> None:
        self.datasets[name] = (np.asarray(frequencies, dtype=float), np.asarray(s))
        if log_x is not None:
            self.log_x.blockSignals(True)
            self.log_x.setChecked(bool(log_x))
            self.log_x.blockSignals(False)
        self.redraw()

    def set_uncertainty(self, name: str, traces: Mapping[str, Any], coverage_factor: float = 2.0) -> None:
        """Expanded uncertainty of the S-parameters of pair ``name``: ``TraceUncertainty`` or (U dB, U °) per parameter."""
        bands = {}
        for parameter, trace in traces.items():
            if isinstance(trace, TraceUncertainty):
                bands[parameter] = (trace.magnitude_db, trace.phase_deg)
            else:
                bands[parameter] = (np.asarray(trace[0], dtype=float), np.asarray(trace[1], dtype=float))
        self.uncertainty[name] = bands
        self.coverage_factor = coverage_factor
        self.uncertainty_box.setText(f"Incertidumbre (k={coverage_factor:g})")
        self.redraw()

    def set_smoothing(self, smoothing: dict[str, float]) -> None:
        """Display smoothing per S-parameter (% of the points): RL with that of S11 / S22, TL with that of S21."""
        self.smoothing = {name: pct for name, pct in smoothing.items() if pct > 0}
        details = ", ".join(f"{name} {pct:g} %" for name, pct in self.smoothing.items())
        self.smooth_box.setText(f"Suavizado ({details})" if details else "Suavizado")
        self.redraw()

    def set_limits(self, curves: list[SpecCurve]) -> None:
        self.limits = list(curves)

    def _pairs(self) -> dict[str, tuple[np.ndarray, np.ndarray]]:
        """The data sets that are port pairs of the coupler (2 × 2 S matrices)."""
        pairs = {}
        for name, (f, s) in self.datasets.items():
            first, second = pair_ports(name)
            if first in COUPLER_PORTS and second in COUPLER_PORTS and s.ndim == 3 and s.shape[1:] == (2, 2):
                pairs[name] = (f, s)
        return pairs

    def _loss(self, name: str, parameter: str, values: np.ndarray) -> tuple[np.ndarray, np.ndarray | None]:
        if self.smooth_box.isChecked() and parameter in self.smoothing:
            values = smooth(values, self.smoothing[parameter])
        band = self.uncertainty.get(name, {}).get(parameter)
        return -_db(values), (band[0] if band is not None else None)

    def return_loss_traces(self) -> list[LossTrace]:
        pairs = self._pairs()
        traces = []
        for port, (name, index) in return_loss_sources(list(pairs)).items():
            f, s = pairs[name]
            parameter = f"S{index + 1}{index + 1}"
            losses, u = self._loss(name, parameter, s[:, index, index])
            source = f"RL de {port}: {parameter} de la medida {name} ({port} en P{index + 1})"
            traces.append(LossTrace(f"{port} ({name}, {parameter})", f, losses, u, PORT_COLORS.get(port, "#555"), source))
        return traces

    def transmission_traces(self) -> list[LossTrace]:
        traces = []
        for index, (name, (f, s)) in enumerate(self._pairs().items()):
            first, second = pair_ports(name)
            symbol = transmission_symbol(name)
            losses, u = self._loss(name, "S21", s[:, 1, 0])
            label = f"{first} → {second}" + (f" · {symbol}" if symbol else "")
            source = f"TL de la medida {name}: S21, de {first} (P1) a {second} (P2)"
            traces.append(LossTrace(label, f, losses, u, DATASET_COLORS[index % len(DATASET_COLORS)], source))
        return traces

    def redraw(self) -> None:
        pairs = self._pairs()
        log_x = self.log_x.isChecked() and bool(pairs) and all(f[0] > 0 for f, _ in pairs.values())
        self.smooth_box.setVisible(bool(self.smoothing))
        self.uncertainty_box.setVisible(bool(self.uncertainty))
        bands = self.uncertainty_box.isChecked()
        self.return_loss.draw(self.return_loss_traces(), log_x, bands)
        self.transmission.draw(self.transmission_traces(), log_x, bands)


class ResultPlots(QStackedWidget):
    """Chart of the result of a test: S-parameters or, for a directional coupler, its RL and TL charts."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.s_parameters = SParameterPlot()
        self.coupler = CouplerPlots()
        self.addWidget(self.s_parameters)
        self.addWidget(self.coupler)

    def for_algorithm(self, algorithm: str | None) -> SParameterPlot | CouplerPlots:
        """Show (and return) the chart of the algorithm of a test."""
        plot = self.coupler if algorithm == "vna_acoplador" else self.s_parameters
        self.setCurrentWidget(plot)
        self.updateGeometry()
        return plot

    def sizeHint(self) -> QSize:  # noqa: N802 (Qt API)
        return self.currentWidget().sizeHint()  # the page shown, not the largest one

    def minimumSizeHint(self) -> QSize:  # noqa: N802 (Qt API)
        return self.currentWidget().minimumSizeHint()

    def clear(self) -> None:
        self.s_parameters.clear()
        self.coupler.clear()


class SParameterDialog(QDialog):
    """S-parameters of the Touchstone file of an equipment (or of any file)."""

    def __init__(self, library: Library | None, equipment: Equipment | None, parent: QWidget | None = None, path: Path | None = None):
        super().__init__(parent)
        self.resize(980, 600)
        layout = QVBoxLayout(self)
        try:
            if equipment is not None and library is not None:
                data = library.touchstone_of(equipment)
                title = equipment.id
                reference = equipment.s_parameters.reference if equipment.s_parameters else ""
                source = equipment.s_parameters.file if equipment.s_parameters else ""
            else:
                assert path is not None
                data = read_touchstone(path)
                title, reference, source = path.name, "", str(path)
        except (TouchstoneError, AssertionError) as exc:
            self.setWindowTitle("Parámetros S")
            layout.addWidget(QLabel(f"No se pueden leer los datos S: {exc}"))
            return
        self.setWindowTitle(f"Parámetros S — {title}")
        header = style.PageHeader(
            f"Parámetros S — {title}",
            f"{Path(source).name}{' — ' + reference if reference else ''} · {data.n_ports} puertos · {len(data.frequencies)} puntos · {data.frequency_range}",
        )
        layout.addWidget(header)
        self.plot = SParameterPlot()
        self.plot.set_data(title, data.frequencies, data.s, log_x=bool(data.frequencies[0] > 0))
        layout.addWidget(self.plot, 1)


class VnaCheckDialog(QDialog):
    """Functional check of a VNA through the high-level interface (raw sweep, no calibration)."""

    def __init__(self, library: Library, equipment: Equipment, parent: QWidget | None = None):
        super().__init__(parent)
        self.library = library
        self.setWindowTitle(f"Comprobación funcional — {equipment.id}")
        self.resize(1150, 700)
        self.vna = None
        layout = QVBoxLayout(self)
        try:
            self.vna = open_vna(equipment, library)
        except InstrumentError as exc:
            layout.addWidget(QLabel(str(exc)))
            return

        form_widget = QWidget()
        form = QFormLayout(form_widget)
        rng = self.vna.frequency_range
        self.start = QLineEdit(f"{max(rng.start, 1e6) / 1e6:g} MHz")
        self.stop = QLineEdit(f"{min(rng.stop, 18e9) / 1e9:g} GHz")
        self.points = QSpinBox()
        self.points.setRange(2, self.vna.max_points or 100001)
        self.points.setValue(401)
        self.sweep_type = QComboBox()
        self.sweep_type.addItems(["lineal", "logaritmico"])
        self.ifbw = QLineEdit("1 kHz")
        self.power = QDoubleSpinBox()
        self.power.setRange(-100, 30)
        self.power.setValue(-10)
        self.power.setSuffix(" dBm")
        self.device = QComboBox()
        self.device.addItem("Through plano P1–P2", "THROUGH")
        for standard, text in (("OPEN", "Abierto"), ("SHORT", "Cortocircuito"), ("MATCH", "Carga adaptada")):
            self.device.addItem(f"{text} en P1", standard)
        for other in library.equipment.values():
            if other.s_parameters is not None:
                self.device.addItem(f"Red S2P: {other.id}", other.id)
        sweep_button = QPushButton("Conectar y barrer")
        sweep_button.setObjectName("primary")
        sweep_button.clicked.connect(self.sweep)
        self.status = QLabel()
        self.status.setWordWrap(True)
        mode = "SIMULACIÓN (medida en bruto, sin calibrar)" if self.vna.simulated else "equipo real (medida con la corrección activa del VNA)"
        form.addRow(QLabel(f"<b>{self.vna.name}</b><br>{mode}<br>Especificación: {rng}, {self.vna.port_count} puertos"))
        form.addRow("Inicio", self.start)
        form.addRow("Fin", self.stop)
        form.addRow("Puntos", self.points)
        form.addRow("Tipo de barrido", self.sweep_type)
        form.addRow("IFBW", self.ifbw)
        form.addRow("Potencia", self.power)
        if self.vna.simulated:
            form.addRow("Conectado (simulación)", self.device)
        form.addRow(sweep_button)
        form.addRow(self.status)

        self.plot = SParameterPlot()
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setPlaceholderText("Registro de operaciones de alto nivel enviadas al VNA")
        right = QSplitter(Qt.Orientation.Vertical)
        right.addWidget(self.plot)
        right.addWidget(self.log)
        right.setStretchFactor(0, 3)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(form_widget)
        splitter.addWidget(right)
        splitter.setStretchFactor(1, 3)
        layout.addWidget(splitter)

    def sweep(self) -> None:
        vna = self.vna
        assert vna is not None
        try:
            start, stop = parse_frequency(self.start.text()), parse_frequency(self.stop.text())
            ifbw = parse_frequency(self.ifbw.text())
            if not vna.connected:
                vna.connect()
                vna.identify()
            device = self.device.currentData()
            if isinstance(vna, DummyVna):
                if device == "THROUGH":
                    vna.connect_through((1, 2))
                elif device in ("OPEN", "SHORT", "MATCH"):
                    vna.connect_standard(device, 1)
                else:
                    data = self.library.touchstone_of(self.library.equipment[device])
                    vna.connect_network(touchstone_model(data), (1, 2), device)
            vna.configure_sweep(start, stop, self.points.value(), self.sweep_type.currentText())
            vna.set_if_bandwidth(ifbw)
            vna.set_power(self.power.value())
            vna.single_sweep()
            frequencies = vna.frequencies()
            ports = tuple(range(1, min(vna.port_count, 2) + 1))
            data = vna.read_s_matrix(ports)
        except (InstrumentError, UnitError, ValueError, TouchstoneError) as exc:
            self.status.setText(f"<span style='color:#c0392b'>{exc}</span>")
            self._show_log()
            return
        self.plot.set_data("VNA", frequencies, data, log_x=self.sweep_type.currentText() == "logaritmico")
        self.status.setText(f"<span style='color:#2e8b57'>Barrido completado: {len(frequencies)} puntos ({vna.calibration_status()})</span>")
        self._show_log()

    def _show_log(self) -> None:
        if self.vna is not None:
            self.log.setPlainText("\n".join(str(entry) for entry in self.vna.log))
            self.log.verticalScrollBar().setValue(self.log.verticalScrollBar().maximum())

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt API)
        if self.vna is not None and self.vna.connected:
            self.vna.disconnect()
        super().closeEvent(event)
