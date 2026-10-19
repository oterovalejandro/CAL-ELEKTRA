"""Dialogs with charts: S-parameters of an equipment and the VNA functional check."""

from __future__ import annotations

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
from calelektra.core.touchstone import TouchstoneError
from calelektra.core.units import UnitError, parse_frequency
from calelektra.instruments.base import InstrumentError
from calelektra.instruments.dummy_vna import STANDARD_DEVICES, DummyVna
from calelektra.instruments.factory import open_vna

TRACE_COLORS = {"S11": "#2b7bb9", "S21": "#c0392b", "S12": "#d98a00", "S22": "#2e8b57"}

pg.setConfigOptions(antialias=True, background="w", foreground="#2c3e50")


def _frequency_axis_plot() -> pg.PlotWidget:
    plot = pg.PlotWidget()
    plot.showGrid(x=True, y=True, alpha=0.25)
    plot.setLabel("bottom", "Frecuencia", units="Hz")
    plot.setLabel("left", "Magnitud", units="dB")
    plot.addLegend(offset=(10, 10))
    return plot


def _db(values: np.ndarray) -> np.ndarray:
    with np.errstate(divide="ignore"):
        return 20 * np.log10(np.abs(values))


class SParameterDialog(QDialog):
    """|Sij| in dB of the Touchstone file of an equipment."""

    def __init__(self, library: Library, equipment: Equipment, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle(f"Parámetros S — {equipment.id}")
        self.resize(900, 560)
        layout = QVBoxLayout(self)
        try:
            data = library.touchstone_of(equipment)
        except TouchstoneError as exc:
            layout.addWidget(QLabel(f"No se pueden leer los datos S: {exc}"))
            return
        reference = equipment.s_parameters.reference if equipment.s_parameters else ""
        layout.addWidget(
            QLabel(f"<b>{equipment.s_parameters.file}</b> — {reference}<br>{data.n_ports} puertos, {len(data.frequencies)} puntos, {data.frequency_range}")
        )
        controls = QHBoxLayout()
        self.plot = _frequency_axis_plot()
        self.log_x = QCheckBox("Eje X logarítmico")
        self.log_x.setChecked(True)
        self.log_x.toggled.connect(lambda on: self.plot.setLogMode(x=on, y=False))
        controls.addWidget(self.log_x)
        self.curves = {}
        frequencies = np.maximum(data.frequencies, 1e-3)
        for i in range(data.n_ports):
            for j in range(data.n_ports):
                name = f"S{i + 1}{j + 1}"
                curve = self.plot.plot(frequencies, data.s_db(name), pen=pg.mkPen(TRACE_COLORS.get(name, "#555"), width=2), name=name)
                self.curves[name] = curve
                box = QCheckBox(name)
                box.setChecked(True)
                box.toggled.connect(curve.setVisible)
                controls.addWidget(box)
        controls.addStretch(1)
        self.plot.setLogMode(x=True, y=False)
        layout.addLayout(controls)
        layout.addWidget(self.plot)


class VnaCheckDialog(QDialog):
    """Functional check of a VNA through the high-level interface (dummy VNA for now)."""

    def __init__(self, library: Library, equipment: Equipment, parent: QWidget | None = None):
        super().__init__(parent)
        self.library = library
        self.setWindowTitle(f"Comprobación funcional — {equipment.id}")
        self.resize(1100, 680)
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
        self.points.setRange(2, 100001)
        self.points.setValue(401)
        self.sweep_type = QComboBox()
        self.sweep_type.addItems(["lineal", "logaritmico"])
        self.ifbw = QLineEdit("1 kHz")
        self.power = QDoubleSpinBox()
        self.power.setRange(-100, 30)
        self.power.setValue(-10)
        self.power.setSuffix(" dBm")
        self.device = QComboBox()
        for standard in STANDARD_DEVICES:
            self.device.addItem(f"Estándar: {standard}", standard)
        for other in library.equipment.values():
            if other.s_parameters is not None:
                self.device.addItem(f"Red S2P: {other.id}", other.id)
        self.parameter = QComboBox()
        self.parameter.addItems([f"S{i}{j}" for i in range(1, self.vna.port_count + 1) for j in range(1, self.vna.port_count + 1)])
        self.parameter.setCurrentText("S21" if self.vna.port_count > 1 else "S11")
        sweep_button = QPushButton("Conectar y barrer")
        sweep_button.clicked.connect(self.sweep)
        self.status = QLabel()
        self.status.setWordWrap(True)
        mode = "SIMULACIÓN (VNA dummy)" if self.vna.simulated else "equipo real"
        form.addRow(QLabel(f"<b>{self.vna.name}</b><br>{mode}<br>Especificación: {rng}, {self.vna.port_count} puertos"))
        form.addRow("Inicio", self.start)
        form.addRow("Fin", self.stop)
        form.addRow("Puntos", self.points)
        form.addRow("Tipo de barrido", self.sweep_type)
        form.addRow("IFBW", self.ifbw)
        form.addRow("Potencia", self.power)
        form.addRow("Dispositivo conectado", self.device)
        form.addRow("Parámetro", self.parameter)
        form.addRow(sweep_button)
        form.addRow(self.status)

        self.plot = _frequency_axis_plot()
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
                if device in STANDARD_DEVICES:
                    vna.connect_device(device)
                else:
                    vna.connect_device(self.library.touchstone_of(self.library.equipment[device]), device)
            vna.configure_sweep(start, stop, self.points.value(), self.sweep_type.currentText())
            vna.set_if_bandwidth(ifbw)
            vna.set_power(self.power.value())
            vna.single_sweep()
            frequencies = vna.frequencies()
            name = self.parameter.currentText()
            trace = vna.read_s_parameter(name)
        except (InstrumentError, UnitError, ValueError, TouchstoneError) as exc:
            self.status.setText(f"<span style='color:#c0392b'>{exc}</span>")
            self._show_log()
            return
        self.plot.clear()
        self.plot.plot(frequencies, _db(trace), pen=pg.mkPen(TRACE_COLORS.get(name, "#555"), width=2), name=name)
        self.plot.setLogMode(x=self.sweep_type.currentText() == "logaritmico", y=False)
        self.status.setText(f"<span style='color:#2e8b57'>Barrido completado: {len(frequencies)} puntos</span>")
        self._show_log()

    def _show_log(self) -> None:
        if self.vna is not None:
            self.log.setPlainText("\n".join(str(entry) for entry in self.vna.log))
            self.log.verticalScrollBar().setValue(self.log.verticalScrollBar().maximum())

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt API)
        if self.vna is not None and self.vna.connected:
            self.vna.disconnect()
        super().closeEvent(event)
