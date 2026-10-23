"""Charts of S-parameters (pyqtgraph) and the dialogs that use them."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt
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
from calelektra.procedures.processing import smooth
from calelektra.procedures.specs import SpecCurve
from calelektra.uncertainty.traces import TraceUncertainty

TRACE_COLORS = {"S11": "#2b7bb9", "S21": "#c0392b", "S12": "#d98a00", "S22": "#2e8b57"}
DATASET_COLORS = ["#2b7bb9", "#c0392b", "#2e8b57", "#d98a00", "#7b5ea7", "#4f8a8b", "#8d6e63"]
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
                    name = f"S{i + 1}{j + 1}"
                    label = f"{dataset} · {name}" if several else name
                    color = DATASET_COLORS[index % len(DATASET_COLORS)] if several and n == 1 else TRACE_COLORS.get(name, "#555")
                    traces.append((label, dataset, name, f, s[:, i, j], color))
        return traces

    def _band(self, dataset: str, parameter: str, mode: str, points: int) -> np.ndarray | None:
        bands = self.uncertainty.get(dataset, {}).get(parameter)
        if bands is None or mode not in ("db", "fase"):
            return None
        u = bands[0] if mode == "db" else bands[1]
        return u if len(u) == points else None

    def _draw_band(self, x: np.ndarray, y: np.ndarray, u: np.ndarray, color: str, wrap: bool) -> None:
        brush = pg.mkColor(color)
        brush.setAlpha(BAND_ALPHA)
        for segment in band_segments(y, u, wrap):
            upper = pg.PlotCurveItem(x[segment], (y + u)[segment])
            lower = pg.PlotCurveItem(x[segment], (y - u)[segment])
            fill = pg.FillBetweenItem(upper, lower, brush=brush, pen=pg.mkPen(None))
            fill.setZValue(-5)
            self.plot.addItem(fill)

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
                self._draw_band(np.log10(x) if log_x else x, y, u, color, wrap=mode == "fase")  # view coordinates
            self.plot.plot(x, y, pen=pg.mkPen(color, width=2), name=label)
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
