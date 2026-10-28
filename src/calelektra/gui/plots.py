"""Charts of S-parameters (pyqtgraph) and the dialogs that use them.

Units of the charts: VSWR (reflections) with the magnitude in dB of the
transmissions, magnitude (dB) or phase (°); the chart of the 1-port impedance
measurement shows the impedance instead (Ω, dBΩ, phase). A directional coupler
is shown in two charts (``CouplerPlots``): the reflection of each port and the
transmission of each measured pair, with its nominal values; ``ResultPlots``
shows one chart or the other after the algorithm of the test.
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
from calelektra.core.frequency import FrequencyRange
from calelektra.core.networks import (
    SMOOTHING_PCT,
    gamma_to_impedance,
    group_delay,
    smoothed,
)
from calelektra.core.touchstone import TouchstoneError, read_touchstone
from calelektra.core.units import UnitError, parse_frequency
from calelektra.gui import style
from calelektra.instruments.base import InstrumentError
from calelektra.instruments.dummy_vna import DummyVna
from calelektra.instruments.factory import open_vna
from calelektra.instruments.simulation import touchstone_model
from calelektra.procedures.coupler import (
    CouplerLimit,
    current_pair_name,
    pair_ports,
    return_loss_sources,
    transmission_symbol,
)
from calelektra.procedures.model import COUPLER_PORTS
from calelektra.procedures.processing import smooth
from calelektra.procedures.specs import SpecCurve
from calelektra.uncertainty.impedance import Z0, impedance_uncertainty
from calelektra.uncertainty.traces import TraceUncertainty

TRACE_COLORS = {"S11": "#2b7bb9", "S21": "#c0392b", "S12": "#d98a00", "S22": "#2e8b57"}
DATASET_COLORS = ["#2b7bb9", "#c0392b", "#2e8b57", "#d98a00", "#7b5ea7", "#4f8a8b", "#8d6e63"]
# several 2-port data sets (e.g. the port pairs of a coupler): one colour per data set, one line style per parameter
TRACE_STYLES = {"S21": Qt.PenStyle.SolidLine, "S12": Qt.PenStyle.DashDotLine, "S11": Qt.PenStyle.DashLine, "S22": Qt.PenStyle.DotLine}
PORT_COLORS = dict(zip(COUPLER_PORTS, ("#2b7bb9", "#c0392b", "#2e8b57", "#d98a00")))
SPEC_COLOR = "#34495e"  # a nominal value that applies to several traces
# units of the charts of S-parameters: VSWR of the reflections with the magnitude of the transmissions (second
# axis), magnitude or phase. The impedance is another quantity: only in the chart of the 1-port impedance measurement.
S_MODES = {"vswr_db": "VSWR, Magnitud (dB)", "db": "Magnitud (dB)", "fase": "Fase (°)"}
# the phase of the transmissions as time: the group delay (−dφ/dω, the laboratory's: over 1 % of the points) and U_φ/(360°·f)
DELAY_MODES = {"retardo": "Retardo de grupo (ns)"}
# the VSWR and the group delay are always smoothed over 1 % of the points, as in the certificates (``core/networks.py``);
# the smoothing of the template (settings of the traces) is that of the other units
FIXED_SMOOTHING = f"El VSWR y el retardo de grupo se suavizan siempre: media móvil del {SMOOTHING_PCT:g} % de los puntos"
IMPEDANCE_MODES = {"z": "Impedancia (Ω)", "z_db": "Impedancia (dBΩ)", "z_fase": "Fase (°)"}  # its uncertainty: uncertainty/impedance.py

BAND_ALPHA = 60  # opacity (0–255) of the uncertainty band of a trace

pg.setConfigOptions(antialias=True, background="w", foreground="#2c3e50")


def _db(values: np.ndarray) -> np.ndarray:
    with np.errstate(divide="ignore"):
        return 20 * np.log10(np.maximum(np.abs(values), 1e-15))


def vswr(gamma_magnitude: np.ndarray | float) -> np.ndarray:
    """VSWR of a reflection of magnitude |Γ| (NaN from |Γ| = 1)."""
    g = np.asarray(gamma_magnitude, dtype=float)
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(g < 1, (1 + g) / (1 - g), np.nan)


def vswr_band(gamma_magnitude: np.ndarray, u_db: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """VSWR at |Γ| ∓ its expanded uncertainty in dB (the band is not symmetric)."""
    return vswr(gamma_magnitude * 10 ** (-u_db / 20)), vswr(gamma_magnitude * 10 ** (u_db / 20))


def smoothed_vswr(
    gamma_magnitude: np.ndarray, u_db: np.ndarray | None = None
) -> tuple[np.ndarray, tuple[np.ndarray, np.ndarray] | None]:
    """VSWR averaged over 1 % of the points, as the laboratory, and the band of each point around it."""
    point = vswr(gamma_magnitude)
    y = smoothed(point)
    if u_db is None:
        return y, None
    low, high = vswr_band(gamma_magnitude, u_db)
    return y, (y - (point - low), y + (high - point))


def _template_smoothing(mode: str, reflection: bool) -> bool:
    """Whether the smoothing of the template applies to a trace in ``mode`` (not to the VSWR nor to the delay)."""
    return not (mode == "retardo" or (mode == "vswr_db" and reflection))


def smoothing_tip(smoothing: dict[str, float]) -> str:
    template = (
        "Suavizado de la plantilla de ensayo: solo cambia la visualización, no los datos"
        if smoothing
        else "La plantilla de ensayo no define suavizado (ajustes de trazas)"
    )
    return f"{template}. {FIXED_SMOOTHING}"


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


def add_band(target, x: np.ndarray, y: np.ndarray, low: np.ndarray, high: np.ndarray, color: str, wrap: bool = False) -> None:
    """Shaded zone between ``low`` and ``high`` behind the trace ``y`` (``x`` in view coordinates: log10 with a logarithmic axis).

    ``target`` is a ``PlotWidget`` or a ``ViewBox`` (the second axis).
    """
    brush = pg.mkColor(color)
    brush.setAlpha(BAND_ALPHA)
    for segment in band_segments(y, high - low, wrap):
        upper = pg.PlotCurveItem(x[segment], high[segment])
        lower = pg.PlotCurveItem(x[segment], low[segment])
        fill = pg.FillBetweenItem(upper, lower, brush=brush, pen=pg.mkPen(None))
        fill.setZValue(-5)
        target.addItem(fill)


def _is_reflection(parameter: str) -> bool:
    return parameter[1] == parameter[2]


class SParameterPlot(QWidget):
    """S-parameters of one or several data sets, with their specifications and uncertainty.

    Units (``S_MODES``): VSWR of the reflections with the magnitude in dB of
    the transmissions on a second axis, magnitude (dB) or phase (°); and the
    group delay of the transmissions (ns, ``DELAY_MODES``). The chart
    of the 1-port impedance measurement shows the impedance instead
    (``set_impedance``): |Z| in Ω or dBΩ and the phase of Z. ``result``: the
    chart of a test, whose «Especificaciones» and «Suavizado» boxes are always
    shown (disabled when there is nothing to show).
    """

    def __init__(self, parent: QWidget | None = None, result: bool = False):
        super().__init__(parent)
        self.datasets: dict[str, tuple[np.ndarray, np.ndarray]] = {}
        self.visible: dict[str, bool] = {}
        self.limits: list[SpecCurve] = []
        self.smoothing: dict[str, float] = {}  # trace (S11…) → % of the points; display only
        self.uncertainty: dict[str, dict[str, tuple[np.ndarray, np.ndarray]]] = {}  # data set → trace → (U dB, U °)
        self.coverage_factor = 2.0
        self.result = result
        self.impedance = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        controls = QHBoxLayout()
        self.mode = QComboBox()
        self.mode.setToolTip("Unidades de la gráfica")
        self._fill_modes()
        self.mode.currentIndexChanged.connect(lambda _i: self.redraw())
        self.log_x = QCheckBox("Frecuencia logarítmica")
        self.log_x.toggled.connect(lambda _on: self.redraw())
        self.show_limits = QCheckBox("Especificaciones")
        self.show_limits.setChecked(True)
        self.show_limits.toggled.connect(lambda _on: self.redraw())
        self.show_limits.setVisible(False)
        self.smooth_box = QCheckBox("Suavizado")
        self.smooth_box.setChecked(True)
        self.smooth_box.toggled.connect(lambda _on: self.redraw())
        self.smooth_box.setVisible(False)
        self.uncertainty_box = QCheckBox("Incertidumbre")
        self.uncertainty_box.setChecked(True)
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
        # second axis (right): magnitude of the transmissions next to the VSWR of the reflections
        item = self.plot.getPlotItem()
        self.right = pg.ViewBox()
        item.scene().addItem(self.right)
        item.getAxis("right").linkToView(self.right)
        self.right.setXLink(item.vb)
        item.vb.sigResized.connect(self._sync_right)
        item.hideAxis("right")
        layout.addWidget(self.plot, 1)
        self.empty = pg.TextItem("Sin datos", color="#8a94a3", anchor=(0.5, 0.5))
        self.redraw()

    def _sync_right(self) -> None:
        main = self.plot.getPlotItem().vb
        self.right.setGeometry(main.sceneBoundingRect())
        self.right.linkedViewChanged(main, self.right.XAxis)

    def _fill_modes(self) -> None:
        modes = IMPEDANCE_MODES if self.impedance else {**S_MODES, **DELAY_MODES}
        current = self.mode.currentData()
        self.mode.blockSignals(True)
        self.mode.clear()
        for key, text in modes.items():
            self.mode.addItem(text, key)
        default = "z" if self.impedance else "db"
        self.mode.setCurrentIndex(self.mode.findData(current if current in modes else default))
        self.mode.blockSignals(False)

    def set_impedance(self, impedance: bool) -> None:
        """Impedance (|Z| in Ω or dBΩ, phase of Z) instead of S-parameters: the 1-port impedance measurement."""
        if impedance != self.impedance:
            self.impedance = impedance
            self._fill_modes()
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
        self.uncertainty[name] = uncertainty_bands(traces)
        self.coverage_factor = coverage_factor
        self.uncertainty_box.setText(f"Incertidumbre (k={coverage_factor:g})")
        self.redraw()

    def set_smoothing(self, smoothing: dict[str, float]) -> None:
        """Display smoothing per trace (% of the points); the data sets are not changed."""
        self.smoothing = {name: pct for name, pct in smoothing.items() if pct > 0}
        self.smooth_box.setText(smoothing_text(self.smoothing))
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

    def _sync_boxes(self, mode: str) -> None:
        """«Especificaciones», «Suavizado» and «Incertidumbre»: shown, and usable, when they apply."""
        impedance = mode in IMPEDANCE_MODES
        limits = bool(self.limits) and not impedance and mode not in DELAY_MODES
        self.show_limits.setVisible(bool(self.limits) or self.result)
        self.show_limits.setEnabled(limits)
        if limits:
            self.show_limits.setToolTip("Curvas de los ficheros de especificación del modelo del EUT (pasa / falla); discontinuas")
        elif self.limits and mode in DELAY_MODES:
            self.show_limits.setToolTip("Las especificaciones son de la magnitud de los parámetros S: no se dibujan sobre el retardo")
        elif self.limits:
            self.show_limits.setToolTip("Las especificaciones son de los parámetros S: no se dibujan sobre la impedancia")
        else:
            self.show_limits.setToolTip("El modelo del EUT no tiene especificaciones S")
        self.smooth_box.setVisible(bool(self.smoothing) or self.result)
        self.smooth_box.setEnabled(bool(self.smoothing))
        self.smooth_box.setToolTip(smoothing_tip(self.smoothing))
        self.uncertainty_box.setVisible(bool(self.uncertainty))
        if impedance:
            tip = (
                "Zona sombreada: incertidumbre expandida de Z, calculada a partir de la de S11 (Z0 = 50 Ω); "
                "cerca del abierto o del cortocircuito no es simétrica"
            )
        elif mode in DELAY_MODES:
            tip = "Zona sombreada: incertidumbre del retardo de grupo, U_φ / (360°·f), con la de la fase de la transmisión"
        else:
            tip = "Zona sombreada: valor ± incertidumbre expandida de cada punto, calculada con el fichero de incertidumbres del VNA"
        self.uncertainty_box.setToolTip(tip)

    def redraw(self) -> None:
        item = self.plot.getPlotItem()
        self.plot.clear()
        self.right.clear()
        self.legend.clear()
        while self.trace_boxes.count():
            widget = self.trace_boxes.takeAt(0).widget()
            if widget:
                widget.deleteLater()
        mode = self.mode.currentData()
        self._sync_boxes(mode)
        traces = self._traces()
        if mode in IMPEDANCE_MODES:
            traces = [trace for trace in traces if _is_reflection(trace[2])]  # the impedance seen at a port
        elif mode in DELAY_MODES:
            traces = [trace for trace in traces if not _is_reflection(trace[2])]  # the delay of a transmission
        reflections = any(_is_reflection(trace[2]) for trace in traces)
        transmissions = any(not _is_reflection(trace[2]) for trace in traces)
        split = mode == "vswr_db" and reflections and transmissions  # transmissions on the second axis
        left = {
            "vswr_db": f"VSWR, suavizado {SMOOTHING_PCT:g} %" if reflections else "Magnitud (dB)",
            "db": "Magnitud (dB)",
            "fase": "Fase (°)",
            "z": "Impedancia |Z|",
            "z_db": "Impedancia |Z| (dBΩ)",
            "z_fase": "Fase de Z (°)",
            "retardo": f"Retardo de grupo (ns), suavizado {SMOOTHING_PCT:g} %",
        }[mode]
        self.plot.setLabel("left", left, units="Ω" if mode == "z" else None)
        if split:
            item.showAxis("right")
            item.setLabel("right", "Magnitud (dB)")
        else:
            item.hideAxis("right")
        if not traces:
            self.plot.setLogMode(x=False, y=False)
            self.plot.addItem(self.empty)
            self.empty.setPos(0.5, 0.5)
            return
        log_x = self.log_x.isChecked() and all(f[0] > 0 for f, _ in self.datasets.values())
        self.plot.setLogMode(x=log_x, y=False)
        smoothing = self.smoothing if self.smooth_box.isChecked() else {}
        bands = self.uncertainty_box.isChecked()
        for label, dataset, parameter, f, values, color in traces:
            reflection = _is_reflection(parameter)
            if parameter in smoothing and _template_smoothing(mode, reflection):
                values = smooth(values, smoothing[parameter])
            self.visible.setdefault(label, True)
            box = QCheckBox(label)
            box.setChecked(self.visible[label])
            box.setStyleSheet(f"QCheckBox {{ color: {color}; font-weight: 600; }}")
            box.toggled.connect(lambda on, key=label: self._toggle(key, on))
            self.trace_boxes.addWidget(box)
            if not self.visible[label]:
                continue
            y, band = self._values(mode, dataset, parameter, values, bands, f)
            x = np.maximum(f, 1e-3)
            second = split and not reflection
            if band is not None:
                add_band(self.right if second else self.plot, np.log10(x) if log_x else x, y, *band, color, wrap=mode in ("fase", "z_fase"))
            style = TRACE_STYLES.get(parameter, Qt.PenStyle.SolidLine) if len(self.datasets) > 1 else Qt.PenStyle.SolidLine
            self._curve(second, x, y, pg.mkPen(color, width=2, style=style), label, log_x)
        self._draw_limits(mode, log_x, split)
        if split:
            self.right.enableAutoRange(axis=pg.ViewBox.YAxis)
            self._sync_right()

    def _values(
        self, mode: str, dataset: str, parameter: str, values: np.ndarray, bands: bool, frequencies: np.ndarray
    ) -> tuple[np.ndarray, tuple[np.ndarray, np.ndarray] | None]:
        """The trace in the units of ``mode`` and its uncertainty band (lower, upper)."""
        u = self.uncertainty.get(dataset, {}).get(parameter) if bands else None
        if u is not None and len(u[0]) != len(values):
            u = None
        if mode == "retardo":
            y = group_delay(frequencies, values) * 1e9
            with np.errstate(divide="ignore", invalid="ignore"):
                spread = np.where(frequencies > 0, u[1] / (360.0 * frequencies) * 1e9, np.nan) if u is not None else None
            return y, ((y - spread, y + spread) if spread is not None else None)
        if mode == "vswr_db" and _is_reflection(parameter):
            return smoothed_vswr(np.abs(values), u[0] if u is not None else None)
        if mode in ("vswr_db", "db"):
            y = _db(values)
            return y, ((y - u[0], y + u[0]) if u is not None else None)
        if mode == "fase":
            y = np.degrees(np.angle(values))
            return y, ((y - u[1], y + u[1]) if u is not None else None)
        found = impedance_uncertainty(values, u[0], u[1], "dB", Z0) if u is not None else None  # from that of S11
        with np.errstate(divide="ignore", invalid="ignore"):
            z = gamma_to_impedance(values, Z0)
            if mode == "z":
                y = np.abs(z)
                return y, ((y - found.magnitude_minus, y + found.magnitude_plus) if found else None)
            if mode == "z_db":
                y = 20 * np.log10(np.abs(z))
                return y, ((y - found.magnitude_db_minus, y + found.magnitude_db_plus) if found else None)
            y = np.degrees(np.angle(z))
            return y, ((y - found.phase_minus, y + found.phase_plus) if found else None)

    def _curve(self, second: bool, x: np.ndarray, y: np.ndarray, pen, name: str | None, log_x: bool) -> None:
        if not second:
            self.plot.plot(x, y, pen=pen, name=name)
            return
        curve = pg.PlotDataItem(x, y, pen=pen, name=name)
        curve.setLogMode(log_x, False)
        self.right.addItem(curve)
        if name:
            self.legend.addItem(curve, name)

    def _draw_limits(self, mode: str, log_x: bool, split: bool) -> None:
        quantity = {"vswr_db": "magnitud_db", "db": "magnitud_db", "fase": "fase_grados"}.get(mode)
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
            reflection = _is_reflection(curve.parameter)

            def convert(v: np.ndarray, reflection: bool = reflection) -> np.ndarray:
                if quantity == "fase_grados":
                    return (v + 180.0) % 360.0 - 180.0
                return vswr(10 ** (v / 20)) if mode == "vswr_db" and reflection else v

            second = split and not reflection
            pen = pg.mkPen(TRACE_COLORS.get(curve.parameter, "#555"), width=1.6, style=Qt.PenStyle.DashLine)
            label = f"Espec. {curve.parameter}" + (f" ({curve.name})" if len(names) > 1 else "")
            if curve.criterion == "tolerancia" and curve.tolerance:
                self._curve(second, f, convert(values + curve.tolerance), pen, f"{label} ± {curve.tolerance:g}", log_x)
                self._curve(second, f, convert(values - curve.tolerance), pen, None, log_x)
            else:
                self._curve(second, f, convert(values), pen, label, log_x)

    def _toggle(self, key: str, on: bool) -> None:
        self.visible[key] = on
        self.redraw()


def uncertainty_bands(traces: Mapping[str, Any]) -> dict[str, tuple[np.ndarray, np.ndarray]]:
    """(U dB, U °) per trace from ``TraceUncertainty`` or (U dB, U °) pairs."""
    bands = {}
    for parameter, trace in traces.items():
        if isinstance(trace, TraceUncertainty):
            bands[parameter] = (trace.magnitude_db, trace.phase_deg)
        else:
            bands[parameter] = (np.asarray(trace[0], dtype=float), np.asarray(trace[1], dtype=float))
    return bands


def smoothing_text(smoothing: dict[str, float]) -> str:
    details = ", ".join(f"{name} {pct:g} %" for name, pct in smoothing.items())
    return f"Suavizado ({details})" if details else "Suavizado"


@dataclass
class ChartTrace:
    label: str
    frequencies: np.ndarray
    values: np.ndarray  # in the units of the chart
    band: tuple[np.ndarray, np.ndarray] | None  # uncertainty: lower and upper values
    color: str
    source: str  # where it comes from (tooltip)


@dataclass
class ChartLine:
    """A nominal value drawn on a chart (dashed)."""

    label: str | None
    frequencies: np.ndarray
    values: np.ndarray
    color: str


class TraceChart(QWidget):
    """One chart of the coupler: a heading, a check box per trace and the plot."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.visible: dict[str, bool] = {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.heading = QLabel()
        layout.addWidget(self.heading)
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
        self._last: tuple = ("", "", [], [], False, False)
        self.draw("", "", [], [], False)

    def labels(self) -> list[str]:
        return [self.trace_boxes.itemAt(i).widget().text() for i in range(self.trace_boxes.count())]

    def draw(self, heading: str, axis: str, traces: list[ChartTrace], lines: list[ChartLine], log_x: bool, wrap: bool = False) -> None:
        self._last = (heading, axis, traces, lines, log_x, wrap)
        self.heading.setText(f"<b>{heading}</b>")
        self.plot.setLabel("left", axis)
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
            if trace.band is not None:
                add_band(self.plot, np.log10(x) if log_x else x, trace.values, *trace.band, trace.color, wrap=wrap)
            self.plot.plot(x, trace.values, pen=pg.mkPen(trace.color, width=2), name=trace.label)
        for line in lines:
            pen = pg.mkPen(line.color, width=1.6, style=Qt.PenStyle.DashLine)
            self.plot.plot(line.frequencies, line.values, pen=pen, name=line.label)

    def _toggle(self, key: str, on: bool) -> None:
        self.visible[key] = on
        self.draw(*self._last)


COUPLER_SPEC_KEYS = {"IL": "perdidas_insercion", "C": "acoplamiento", "I": "aislamiento"}


class CouplerPlots(QWidget):
    """Directional coupler: the reflection of each port and the transmission of each measured pair, in two charts.

    The data sets are the measured pairs (``In-Out``…: first port on P1,
    second on P2). The reflection of In and Out comes from the In–Out pair
    (each of them is measured in two pairs) and that of the other ports from
    the first pair that measures them (``procedures/coupler.py``); the
    transmission of a pair is its S21. Units: VSWR of each port with the TL
    (dB) of each pair, RL and TL (dB) or phase (°). The nominal values of the
    coupler (``set_nominal_values``) are drawn as dashed lines. Same interface
    as ``SParameterPlot`` for the test window and the explorer.
    """

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.datasets: dict[str, tuple[np.ndarray, np.ndarray]] = {}
        self.limits: list[SpecCurve] = []  # S2P specifications do not apply: the coupler has its nominal values
        self.nominal: dict[str, CouplerLimit] = {}
        self.nominal_range: FrequencyRange | None = None
        self.smoothing: dict[str, float] = {}
        self.uncertainty: dict[str, dict[str, tuple[np.ndarray, np.ndarray]]] = {}
        self.coverage_factor = 2.0
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        controls = QHBoxLayout()
        self.mode = QComboBox()
        self.mode.setToolTip("Unidades de las gráficas")
        for key, text in S_MODES.items():
            self.mode.addItem(text, key)
        self.mode.setCurrentIndex(self.mode.findData("db"))
        self.mode.currentIndexChanged.connect(lambda _i: self.redraw())
        self.log_x = QCheckBox("Frecuencia logarítmica")
        self.log_x.toggled.connect(lambda _on: self.redraw())
        self.show_limits = QCheckBox("Especificaciones")
        self.show_limits.setChecked(True)
        self.show_limits.toggled.connect(lambda _on: self.redraw())
        self.smooth_box = QCheckBox("Suavizado")
        self.smooth_box.setChecked(True)
        self.smooth_box.toggled.connect(lambda _on: self.redraw())
        self.uncertainty_box = QCheckBox("Incertidumbre")
        self.uncertainty_box.setChecked(True)
        self.uncertainty_box.setToolTip("Zona sombreada: valor ± incertidumbre expandida de cada punto (la del parámetro S del que sale cada traza)")
        self.uncertainty_box.toggled.connect(lambda _on: self.redraw())
        self.uncertainty_box.setVisible(False)
        for widget in (self.mode, self.log_x, self.show_limits, self.smooth_box, self.uncertainty_box):
            controls.addWidget(widget)
        controls.addStretch(1)
        layout.addLayout(controls)
        self.reflection = TraceChart()
        self.transmission = TraceChart()
        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.addWidget(self.reflection)
        splitter.addWidget(self.transmission)
        layout.addWidget(splitter, 1)
        self.redraw()

    def sizeHint(self) -> QSize:  # noqa: N802 (Qt API)
        return QSize(super().sizeHint().width(), 640)  # both charts in view in the explorer

    def clear(self) -> None:
        self.datasets.clear()
        self.limits = []
        self.nominal = {}
        self.nominal_range = None
        self.smoothing = {}
        self.uncertainty = {}
        self.redraw()

    def set_data(self, name: str, frequencies: np.ndarray, s: np.ndarray, log_x: bool | None = None) -> None:
        self.datasets[current_pair_name(name)] = (np.asarray(frequencies, dtype=float), np.asarray(s))
        if log_x is not None:
            self.log_x.blockSignals(True)
            self.log_x.setChecked(bool(log_x))
            self.log_x.blockSignals(False)
        self.redraw()

    def set_uncertainty(self, name: str, traces: Mapping[str, Any], coverage_factor: float = 2.0) -> None:
        """Expanded uncertainty of the S-parameters of pair ``name``: ``TraceUncertainty`` or (U dB, U °) per parameter."""
        self.uncertainty[current_pair_name(name)] = uncertainty_bands(traces)
        self.coverage_factor = coverage_factor
        self.uncertainty_box.setText(f"Incertidumbre (k={coverage_factor:g})")
        self.redraw()

    def set_smoothing(self, smoothing: dict[str, float]) -> None:
        """Display smoothing per S-parameter (% of the points): the reflections with that of S11 / S22, the transmissions with S21."""
        self.smoothing = {name: pct for name, pct in smoothing.items() if pct > 0}
        self.smooth_box.setText(smoothing_text(self.smoothing))
        self.redraw()

    def set_limits(self, curves: list[SpecCurve]) -> None:
        self.limits = list(curves)

    def set_nominal_values(self, limits: Mapping[str, CouplerLimit], nominal_range: FrequencyRange | None = None) -> None:
        """Nominal values of the model of the coupler (the pass / fail) and the range where they apply."""
        self.nominal = dict(limits)
        self.nominal_range = nominal_range
        self.redraw()

    def _pairs(self) -> dict[str, tuple[np.ndarray, np.ndarray]]:
        """The data sets that are port pairs of the coupler (2 × 2 S matrices)."""
        pairs = {}
        for name, (f, s) in self.datasets.items():
            first, second = pair_ports(name)
            if first in COUPLER_PORTS and second in COUPLER_PORTS and s.ndim == 3 and s.shape[1:] == (2, 2):
                pairs[name] = (f, s)
        return pairs

    def _convert(
        self, mode: str, name: str, parameter: str, values: np.ndarray, reflection: bool
    ) -> tuple[np.ndarray, tuple[np.ndarray, np.ndarray] | None]:
        """A trace in the units of ``mode`` (losses in dB, VSWR or phase) and its uncertainty band."""
        if self.smooth_box.isChecked() and parameter in self.smoothing and _template_smoothing(mode, reflection):
            values = smooth(values, self.smoothing[parameter])
        u = self.uncertainty.get(name, {}).get(parameter) if self.uncertainty_box.isChecked() else None
        if u is not None and len(u[0]) != len(values):
            u = None
        if mode == "fase":
            y = np.degrees(np.angle(values))
            return y, ((y - u[1], y + u[1]) if u is not None else None)
        if mode == "vswr_db" and reflection:
            return smoothed_vswr(np.abs(values), u[0] if u is not None else None)
        y = -_db(values)
        return y, ((y - u[0], y + u[0]) if u is not None else None)

    def reflection_traces(self, mode: str | None = None) -> list[ChartTrace]:
        """Return loss, VSWR or phase of the reflection of each port measured."""
        mode = mode or self.mode.currentData()
        pairs = self._pairs()
        traces = []
        for port, (name, index) in return_loss_sources(list(pairs)).items():
            f, s = pairs[name]
            parameter = f"S{index + 1}{index + 1}"
            values, band = self._convert(mode, name, parameter, s[:, index, index], reflection=True)
            source = f"{port}: {parameter} de la medida {name} ({port} en P{index + 1})"
            traces.append(ChartTrace(f"{port} ({name}, {parameter})", f, values, band, PORT_COLORS.get(port, "#555"), source))
        return traces

    def transmission_traces(self, mode: str | None = None) -> list[ChartTrace]:
        """Transmission loss or phase of each pair: its S21, from the port on P1 to the port on P2."""
        mode = mode or self.mode.currentData()
        traces = []
        for index, (name, (f, s)) in enumerate(self._pairs().items()):
            first, second = pair_ports(name)
            symbol = transmission_symbol(name)
            values, band = self._convert(mode, name, "S21", s[:, 1, 0], reflection=False)
            label = f"{first} → {second}" + (f" · {symbol}" if symbol else "")
            source = f"Medida {name}: S21, de {first} (P1) a {second} (P2)"
            traces.append(ChartTrace(label, f, values, band, DATASET_COLORS[index % len(DATASET_COLORS)], source))
        return traces

    def _span(self) -> np.ndarray | None:
        """Frequencies where the nominal values apply: the measured range inside the nominal range of the coupler."""
        pairs = self._pairs()
        if not pairs:
            return None
        low = min(f[0] for f, _ in pairs.values())
        high = max(f[-1] for f, _ in pairs.values())
        if self.nominal_range is not None:
            low, high = max(low, self.nominal_range.start), min(high, self.nominal_range.stop)
        return np.array([low, high]) if high > low else None

    def nominal_lines(self, mode: str | None = None) -> tuple[list[ChartLine], list[ChartLine]]:
        """Dashed lines of the nominal values: return loss (or VSWR) on the first chart; IL, C and I on the second one."""
        mode = mode or self.mode.currentData()
        span = self._span()
        if mode == "fase" or span is None or not self.nominal or not self.show_limits.isChecked():
            return [], []
        if self.log_x.isChecked() and span[0] <= 0:
            return [], []
        ports = f"{COUPLER_PORTS[0]}, {COUPLER_PORTS[1]}"
        reflection = []
        limit = self.nominal.get("perdidas_retorno")
        if limit is not None:
            if mode == "vswr_db":
                value = float(vswr(10 ** (-limit.value / 20)))
                reflection.append(ChartLine(f"Espec. VSWR ≤ {value:.3g} ({ports})", span, np.full(2, value), SPEC_COLOR))
            else:
                reflection.append(ChartLine(f"Espec. RL {limit.describe()} ({ports})", span, np.full(2, limit.value), SPEC_COLOR))
        transmission = []
        drawn = set()
        for name, trace in zip(self._pairs(), self.transmission_traces(mode)):
            symbol = transmission_symbol(name)
            limit = self.nominal.get(COUPLER_SPEC_KEYS.get(symbol or "", ""))
            if limit is None or (symbol, limit.value) in drawn:
                continue
            drawn.add((symbol, limit.value))
            label = f"Espec. {symbol} {limit.describe()}"
            if limit.criterion == "tolerancia" and limit.tolerance:
                transmission.append(ChartLine(label, span, np.full(2, limit.value + limit.tolerance), trace.color))
                transmission.append(ChartLine(None, span, np.full(2, limit.value - limit.tolerance), trace.color))
            else:
                transmission.append(ChartLine(label, span, np.full(2, limit.value), trace.color))
        return reflection, transmission

    def _sync_boxes(self, mode: str) -> None:
        nominal = bool(self.nominal)
        self.show_limits.setEnabled(nominal and mode != "fase")
        self.show_limits.setToolTip(
            "Valores nominales del modelo del acoplador (pasa / falla) dentro de su rango nominal; discontinuas"
            if nominal
            else "Sin valores nominales: el modelo del acoplador no los tiene o el ensayo no los guardó"
        )
        self.smooth_box.setEnabled(bool(self.smoothing))
        self.smooth_box.setToolTip(smoothing_tip(self.smoothing))
        self.uncertainty_box.setVisible(bool(self.uncertainty))

    def redraw(self) -> None:
        mode = self.mode.currentData()
        self._sync_boxes(mode)
        pairs = self._pairs()
        log_x = self.log_x.isChecked() and bool(pairs) and all(f[0] > 0 for f, _ in pairs.values())
        reflection_lines, transmission_lines = self.nominal_lines(mode)
        reflection = {
            "vswr_db": (f"VSWR de cada puerto (suavizado {SMOOTHING_PCT:g} %)", "VSWR"),
            "db": ("Pérdidas de retorno (RL) de cada puerto", "RL (dB)"),
            "fase": ("Fase de la reflexión de cada puerto", "Fase (°)"),
        }[mode]
        transmission = (
            ("Fase de la transmisión de cada medida", "Fase (°)") if mode == "fase" else ("Pérdidas de transmisión (TL) de cada medida", "TL (dB)")
        )
        wrap = mode == "fase"
        self.reflection.draw(*reflection, self.reflection_traces(mode), reflection_lines, log_x, wrap)
        self.transmission.draw(*transmission, self.transmission_traces(mode), transmission_lines, log_x, wrap)


class ResultPlots(QStackedWidget):
    """Chart of the result of a test: S-parameters (the impedance for the 1-port impedance measurement) or,
    for a directional coupler, its two charts."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.s_parameters = SParameterPlot(result=True)
        self.coupler = CouplerPlots()
        self.addWidget(self.s_parameters)
        self.addWidget(self.coupler)

    def for_algorithm(self, algorithm: str | None) -> SParameterPlot | CouplerPlots:
        """Show (and return) the chart of the algorithm of a test."""
        plot = self.coupler if algorithm == "vna_acoplador" else self.s_parameters
        if plot is self.s_parameters:
            plot.set_impedance(algorithm == "vna_1_puerto")
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
