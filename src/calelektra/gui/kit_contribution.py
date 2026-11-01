"""The contribution of a calibration kit to the uncertainty of a calibrated measurement (``kits/contribution.py``),
as METAS VNA Tools propagates it: «Contribución a la incertidumbre…» in the tab «Patrones» of a kit unit.

* Calibration: OSM, TOSM (flush through or the through of the kit), UOSM or transmission normalization, with the
  genders of the standards of each port, and the definitions of the unit (its real values) or of the kit model
  (the specifications of the manufacturer).
* DUT: a reflection |Γ| (at a phase, or at the worst one), a through, a matched attenuator, or a Touchstone file.
* Results: U (k = 2) of a parameter, standard by standard, with that of the uncertainty file of a VNA (the
  datasheet of the laboratory) for comparison; the effective error terms that the kit leaves, beside those of
  the file («effective system data»); and the corrected DUT with the uncertainty of the kit (chart, table and
  correlation), which can be saved as ``.sdatb``.
"""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QAbstractItemView,
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
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from calelektra.catalog.library import Library
from calelektra.catalog.model import Equipment, EquipmentModel
from calelektra.core.connectors import GENDERS
from calelektra.core.touchstone import TouchstoneError, read_touchstone
from calelektra.core.units import format_frequency
from calelektra.gui.kit_standards import StandardPreview
from calelektra.gui.plots import DATASET_COLORS, ChartLine, ChartTrace, TraceChart
from calelektra.kits import KitError, kit_frequencies
from calelektra.kits.contribution import (
    FLAT,
    METHODS,
    KitContribution,
    kit_contribution,
    reflection_dut,
    two_port_dut,
    worst_phase,
)
from calelektra.sdata import SDataError, save_sparam_data
from calelektra.uncertainty.engine import UncertaintyCalculationError
from calelektra.uncertainty.traces import trace_uncertainty

POINTS = 361  # frequencies of the evaluation
QUANTITIES = {"modulo": "U |S| (lineal)", "db": "U |S| (dB)", "fase": "U ∠S (°)"}
DUTS = {"reflexion": "Reflexión |Γ|", "through": "Through (0 dB)", "atenuador": "Atenuador adaptado", "fichero": "Fichero .s1p / .s2p…"}
TOTAL_COLOR, VNA_COLOR = "#2c3e50", "#c0392b"
# effective terms of the datasheet of a VNA (``effective_system_data`` of its uncertainty file)
DATASHEET_TERMS = {
    "Directividad": "directivity_min_db",
    "Adaptación de fuente": "source_match_min_db",
    "Adaptación de carga": "load_match_min_db",
    "Seguimiento de reflexión": "reflection_tracking_max_db",
    "Seguimiento de transmisión": "transmission_tracking_max_db",
}


def _number(text: str, label: str) -> float:
    try:
        return float(text.strip().replace(",", "."))
    except ValueError as exc:
        raise ValueError(f"«{label}»: «{text}» no es un número") from exc


def _g(value: float, digits: int = 3) -> str:
    return f"{value:.{digits}g}".replace(".", ",")


def vna_reference(library: Library, vna: Equipment, parameter: str, frequencies: np.ndarray, values: np.ndarray, quantity: str) -> np.ndarray | None:
    """U (k = 2) of the uncertainty file of a VNA (the algorithm of the laboratory) for these values: NaN where the
    file does not cover them."""
    try:
        engine = library.uncertainty_engine(vna)
    except UncertaintyCalculationError:
        return None
    name = engine.equipment_model
    if quantity == "modulo":
        result = np.full(len(frequencies), np.nan)
        for k, (f, v) in enumerate(zip(frequencies, values)):
            try:
                result[k] = engine.calculate(name, parameter, float(f), float(abs(v)), "linear").expanded_uncertainty
            except (UncertaintyCalculationError, ValueError, TypeError):
                pass
        return result
    trace = trace_uncertainty(engine, name, parameter, frequencies, values)
    return trace.magnitude_db if quantity == "db" else trace.phase_deg


class KitContributionDialog(QDialog):
    """The contribution of the kit to the uncertainty of a calibrated measurement."""

    def __init__(self, library: Library, model: EquipmentModel, unit: Equipment | None = None, parent: QWidget | None = None):
        super().__init__(parent)
        self.library, self.model, self.unit = library, model, unit
        self.result: KitContribution | None = None
        self.dut_matrix: np.ndarray | None = None  # the S-parameters of the DUT of the last computation
        self.worst: dict[str, dict[str, np.ndarray]] | None = None  # a reflection at its worst phase, by quantity
        self.worst_case: tuple | None = None  # (standards, |Γ|) of that reflection
        self.setWindowTitle(f"Contribución del kit {unit.id if unit else model.designation} a la incertidumbre")
        self.resize(1240, 900)
        layout = QVBoxLayout(self)
        intro = QLabel(
            "Como METAS VNA Tools: la incertidumbre de la definición de cada patrón se propaga linealmente por la calibración "
            "hasta la medida corregida. Solo la del kit: el ruido, la deriva, los cables y la repetibilidad de conectores van aparte."
        )
        intro.setWordWrap(True)
        intro.setObjectName("pageSubtitle")
        layout.addWidget(intro)

        box = QGroupBox("Calibración y DUT")
        form = QFormLayout(box)
        self.definitions = QComboBox()
        if unit is not None:
            self.definitions.addItem(f"Las de la unidad {unit.id} (sus valores reales o especificaciones propias)", "unidad")
        self.definitions.addItem("Las del modelo del kit (especificaciones del fabricante)", "modelo")
        self.method = QComboBox()
        for key, text in METHODS.items():
            self.method.addItem(text, key)
        self.gender1, self.gender2 = QComboBox(), QComboBox()
        for combo in (self.gender1, self.gender2):
            for key in ("macho", "hembra"):
                combo.addItem(f"patrones {GENDERS[key].lower()}", key)
        self.gender2.setCurrentIndex(1)
        self.through = QComboBox()
        self.through.addItem("Plano (definición exacta)", FLAT)
        for standard in model.standards:
            if standard.type == "through" and standard.definition.kind != "ninguna":
                self.through.addItem(f"{standard.label or standard.id} del kit", standard.id)
        calibration = QHBoxLayout()
        self.gender2_label, self.through_label = QLabel("Puerto 2:"), QLabel("Through:")
        for widget in (self.method, QLabel("Puerto 1:"), self.gender1, self.gender2_label, self.gender2, self.through_label, self.through):
            calibration.addWidget(widget)
        calibration.addStretch(1)
        form.addRow("Definiciones", self.definitions)
        form.addRow("Calibración", calibration)
        self.dut = QComboBox()
        for key, text in DUTS.items():
            self.dut.addItem(text, key)
        self.magnitude, self.phase = QLineEdit("0,1"), QLineEdit()
        self.phase.setPlaceholderText("la peor")
        self.attenuation, self.match = QLineEdit("20"), QLineEdit("-20")
        self.file = QLineEdit()
        self.file.setReadOnly(True)
        dut = QHBoxLayout()
        self.dut_fields = {
            "reflexion": [QLabel("|Γ|"), self.magnitude, QLabel("fase (°)"), self.phase],
            "atenuador": [QLabel("atenuación (dB)"), self.attenuation, QLabel("adaptación (dB)"), self.match],
            "fichero": [self.file],
        }
        dut.addWidget(self.dut)
        for widgets in self.dut_fields.values():
            for widget in widgets:
                dut.addWidget(widget)
        dut.addStretch(1)
        form.addRow("DUT", dut)
        self.vna = QComboBox()
        self.vna.addItem("—", None)
        for equipment in sorted(library.equipment.values(), key=lambda e: e.id):
            if equipment.uncertainty_file:
                self.vna.addItem(f"{equipment.id} ({library.uncertainty_designation(equipment)})", equipment.id)
        if self.vna.count() > 1:
            self.vna.setCurrentIndex(1)
        form.addRow("Comparar con", self.vna)
        compute = QPushButton("Calcular")
        compute.setObjectName("primary")
        compute.clicked.connect(self.compute)
        form.addRow(compute)
        layout.addWidget(box)

        self.tabs = QTabWidget()
        page = QWidget()
        page_layout = QVBoxLayout(page)
        page_layout.setContentsMargins(0, 4, 0, 0)
        controls = QHBoxLayout()
        self.parameter = QComboBox()
        self.quantity = QComboBox()
        for key, text in QUANTITIES.items():
            self.quantity.addItem(text, key)
        self.parameter.currentIndexChanged.connect(lambda _i: self.redraw())
        self.quantity.currentIndexChanged.connect(lambda _i: self.redraw())
        controls.addWidget(self.parameter)
        controls.addWidget(self.quantity)
        controls.addStretch(1)
        page_layout.addLayout(controls)
        self.chart = TraceChart()
        page_layout.addWidget(self.chart, 1)
        self.tabs.addTab(page, "Contribución por patrón")
        self.terms = QTableWidget(0, 1)
        self.terms.verticalHeader().setVisible(False)
        self.terms.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.terms.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.terms.horizontalHeader().setStretchLastSection(True)
        terms_page = QWidget()
        terms_layout = QVBoxLayout(terms_page)
        terms_layout.setContentsMargins(0, 4, 0, 0)
        terms_layout.addWidget(self.terms, 1)
        note = QLabel("Kit: la incertidumbre (k = 2) del término que deja la calibración, el peor valor de la banda; directividad y adaptaciones "
                      "como en una hoja de datos (−20·log10 del radio: cuanto mayor, mejor). VNA: el «effective system data» de su fichero de incertidumbres.")
        note.setWordWrap(True)
        note.setObjectName("pageSubtitle")
        terms_layout.addWidget(note)
        self.tabs.addTab(terms_page, "Términos de error efectivos")
        self.preview = StandardPreview()
        self.tabs.addTab(self.preview, "Medida corregida")
        layout.addWidget(self.tabs, 1)
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        buttons = QHBoxLayout()
        self.save_button = QPushButton("Guardar .sdatb…")
        self.save_button.setToolTip("La medida corregida con la incertidumbre del kit, para compararla en METAS VNA Tools")
        self.save_button.clicked.connect(self.save)
        self.copy_button = QPushButton("Copiar tabla")
        self.copy_button.setToolTip("La contribución por patrón, separada por tabuladores")
        self.copy_button.clicked.connect(lambda: QGuiApplication.clipboard().setText(self.table_text()))
        close = QPushButton("Cerrar")
        close.clicked.connect(self.close)
        buttons.addWidget(self.save_button)
        buttons.addWidget(self.copy_button)
        buttons.addStretch(1)
        buttons.addWidget(close)
        layout.addLayout(buttons)
        self.method.currentIndexChanged.connect(self._method_changed)
        self.dut.currentIndexChanged.connect(self._dut_changed)
        self._method_changed()
        self.chart.draw("", "", [], [], False)

    # ── choices ────────────────────────────────────────────────────────────
    def _method_changed(self) -> None:
        method = self.method.currentData()
        for widget in (self.gender2_label, self.gender2):
            widget.setVisible(method in ("TOSM", "UOSM"))
        for widget in (self.through_label, self.through):
            widget.setVisible(method in ("TOSM", "normalizacion"))
        self.gender1.setEnabled(method != "normalizacion")
        self.dut.blockSignals(True)
        current = self.dut.currentData()
        self.dut.clear()
        for key, text in DUTS.items():
            if method == "OSM" and key in ("through", "atenuador"):
                continue
            if method != "OSM" and key == "reflexion":
                continue
            self.dut.addItem(text, key)
        self.dut.setCurrentIndex(max(self.dut.findData(current), 0))
        self.dut.blockSignals(False)
        self._dut_changed()

    def _dut_changed(self) -> None:
        kind = self.dut.currentData()
        for key, widgets in self.dut_fields.items():
            for widget in widgets:
                widget.setVisible(key == kind)
        if kind == "fichero" and not self.file.text():
            self.choose_file()

    def choose_file(self) -> None:
        name, _ = QFileDialog.getOpenFileName(self, "DUT", str(self.library.root), "Touchstone (*.s1p *.s2p *.S1P *.S2P)")
        if name:
            self.file.setText(name)

    def frequencies(self) -> np.ndarray:
        settings = self.library.kit_settings(self.model, self.unit)
        f = kit_frequencies(settings)
        return np.linspace(f[0], f[-1], POINTS)

    def build_dut(self, f: np.ndarray, ports: int) -> np.ndarray | None:
        """The S-parameters of the DUT on ``f``; None for the worst phase of a reflection."""
        kind = self.dut.currentData()
        if kind == "reflexion":
            magnitude = _number(self.magnitude.text(), "|Γ|")
            if not 0 <= magnitude <= 1:
                raise ValueError("«|Γ|» debe estar entre 0 y 1")
            if not self.phase.text().strip():
                return None
            return reflection_dut(f, magnitude * np.exp(1j * math.radians(_number(self.phase.text(), "fase"))))
        if kind == "through":
            return two_port_dut(f, 0.0)
        if kind == "atenuador":
            return two_port_dut(f, -abs(_number(self.attenuation.text(), "atenuación")), -abs(_number(self.match.text(), "adaptación")))
        path = Path(self.file.text())
        if not self.file.text() or not path.is_file():
            raise ValueError("Elija el fichero Touchstone del DUT")
        try:
            data = read_touchstone(path)
        except TouchstoneError as exc:
            raise ValueError(str(exc)) from exc
        values = np.stack([np.interp(f, data.frequencies, data.s[:, i, j].real) + 1j * np.interp(f, data.frequencies, data.s[:, i, j].imag)
                           for i in range(data.ports) for j in range(data.ports)], axis=1).reshape(len(f), data.ports, data.ports)
        if ports == 1:
            return values[:, :1, :1]
        if data.ports < 2:
            raise ValueError(f"{path.name} es de 1 puerto: la calibración {self.method.currentData()} mide 2")
        return values[:, :2, :2]

    # ── computation ────────────────────────────────────────────────────────
    def compute(self) -> bool:
        QGuiApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            return self._compute()
        finally:
            QGuiApplication.restoreOverrideCursor()

    def _compute(self) -> bool:
        method = self.method.currentData()
        unit = self.unit if self.definitions.currentData() == "unidad" else None
        f = self.frequencies()
        try:
            standards = self.library.calibration_standards(self.model, unit, method, (self.gender1.currentData(), self.gender2.currentData()),
                                                           self.through.currentData(), f)
            dut = self.build_dut(f, standards.ports)
            self.worst, self.worst_case = None, None
            if dut is None:  # a reflection at the worst phase: computed for each quantity when it is shown
                magnitude = _number(self.magnitude.text(), "|Γ|")
                self.worst, self.worst_case = {}, (standards, magnitude)
                dut = reflection_dut(f, magnitude)
            self.result = kit_contribution(standards, dut, self.library.kit_settings(self.model, unit).zr)
        except (KitError, SDataError, ValueError) as exc:
            self.result = None
            self.summary.setText(f"✖ {exc}")
            self.chart.draw("", "", [], [], False)
            return False
        self.dut_matrix = dut
        n = self.result.data.n_ports
        current = self.parameter.currentText()
        self.parameter.blockSignals(True)
        self.parameter.clear()
        for c in range(n):
            for r in range(n):
                self.parameter.addItem(f"S{r + 1}{c + 1}")
        self.parameter.setCurrentIndex(max(self.parameter.findText(current), 0))
        self.parameter.blockSignals(False)
        self.preview.show_data(self.result.data, f"{METHODS[method]}: el DUT corregido con la incertidumbre del kit")
        self._fill_terms()
        self.redraw()
        return True

    def contribution(self, parameter: str, quantity: str) -> dict[str, np.ndarray]:
        r, c = int(parameter[1]) - 1, int(parameter[2]) - 1
        if self.worst is not None and parameter == "S11":
            if quantity not in self.worst:
                QGuiApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
                try:
                    self.worst[quantity] = worst_phase(*self.worst_case, quantity)
                finally:
                    QGuiApplication.restoreOverrideCursor()
            return self.worst[quantity]
        return self.result.breakdown(r, c, quantity)

    def redraw(self) -> None:
        if self.result is None:
            return
        parameter, quantity = self.parameter.currentText() or "S11", self.quantity.currentData()
        f = self.result.data.frequencies
        values = self.contribution(parameter, quantity)
        traces = [ChartTrace("Total del kit", f, values["total"], None, TOTAL_COLOR, "Todas las entradas del kit")]
        for k, (group, u) in enumerate((g, u) for g, u in values.items() if g != "total"):
            traces.append(ChartTrace(group, f, u, None, DATASET_COLORS[k % len(DATASET_COLORS)], group))
        lines = []
        reference = self.reference(parameter, quantity)
        if reference is not None and np.any(np.isfinite(reference)):
            lines.append(ChartLine(f"{self.vna.currentData()}: su fichero de incertidumbres (total)", f, reference, VNA_COLOR))
        dut = "la peor fase" if self.worst is not None and parameter == "S11" else ""
        self.chart.draw(f"{parameter}: U (k = 2) debida al kit{' — ' + dut if dut else ''}", QUANTITIES[quantity], traces, lines, False)
        total = values["total"]
        text = f"{parameter}: U {QUANTITIES[quantity].split(' ', 1)[1]} del kit de {_g(float(np.nanmin(total)))} a {_g(float(np.nanmax(total)))}"
        if reference is not None and np.any(np.isfinite(reference)):
            ratio = total / np.where(reference > 0, reference, np.nan)
            text += f"; su fichero de incertidumbres da {_g(float(np.nanmin(reference)))} a {_g(float(np.nanmax(reference)))} (el kit solo: hasta {_g(float(np.nanmax(ratio)), 2)} veces)"
        self.summary.setText(text)

    def reference(self, parameter: str, quantity: str) -> np.ndarray | None:
        vna_id = self.vna.currentData()
        if not vna_id or self.result is None:
            return None
        r, c = int(parameter[1]) - 1, int(parameter[2]) - 1
        values = self.dut_matrix[:, r, c]
        if not np.any(np.abs(values) > 0):
            return None
        return vna_reference(self.library, self.library.equipment[vna_id], parameter, self.result.data.frequencies, values, quantity)

    def _fill_terms(self) -> None:
        """The effective error terms of the kit by bands, beside those of the uncertainty file of the VNA."""
        f = self.result.data.frequencies
        datasheet = []
        vna_id = self.vna.currentData()
        if vna_id:
            try:
                data = self.library.uncertainty_engine(self.library.equipment[vna_id]).data
                datasheet = data.get("effective_system_data", {}).get("frequency_ranges", [])
            except UncertaintyCalculationError:
                datasheet = []
        bands = [(r["f_start_hz"], r["f_stop_hz"], r) for r in datasheet if r["f_start_hz"] < f[-1]] or [(f[0], f[-1], {})]
        self.terms.clear()
        self.terms.setColumnCount(1 + len(bands))
        self.terms.setHorizontalHeaderLabels(["Término"] + [f"{format_frequency(a)} – {format_frequency(b)}" for a, b, _ in bands])
        self.terms.setRowCount(0)
        for term in self.result.terms:
            row = self.terms.rowCount()
            self.terms.insertRow(row)
            self.terms.setItem(row, 0, QTableWidgetItem(f"{term.name} ({term.port})"))
            for column, (low, high, sheet) in enumerate(bands, start=1):
                mask = (f >= low) & (f <= high)
                if not mask.any():
                    continue
                if term.tracking:
                    text = f"kit ±{_g(float(term.magnitude_db[mask].max()), 2)} dB, ±{_g(float(term.phase_deg[mask].max()), 2)}°"
                else:
                    text = f"kit {float(term.db[mask].min()):.1f} dB".replace(".", ",")
                key = DATASHEET_TERMS.get(term.name)
                if key and key in sheet:
                    text += f" · VNA {'±' if term.tracking else ''}{_g(float(sheet[key]))} dB"
                self.terms.setItem(row, column, QTableWidgetItem(text))

    # ── output ─────────────────────────────────────────────────────────────
    def table_text(self) -> str:
        if self.result is None:
            return ""
        parameter, quantity = self.parameter.currentText() or "S11", self.quantity.currentData()
        values = self.contribution(parameter, quantity)
        groups = list(values)
        lines = ["\t".join(["f (Hz)", *groups])]
        for k, frequency in enumerate(self.result.data.frequencies):
            lines.append("\t".join([f"{frequency:.12g}", *(repr(float(values[g][k])) for g in groups)]))
        return "\n".join(lines)

    def save(self) -> None:
        if self.result is None:
            return
        name, _ = QFileDialog.getSaveFileName(self, "Medida corregida con la incertidumbre del kit", str(self.library.root), "METAS VNA Tools (*.sdatb)")
        if not name:
            return
        try:
            save_sparam_data(Path(name).with_suffix(".sdatb"), self.result.data)
        except (SDataError, OSError) as exc:
            QMessageBox.warning(self, self.windowTitle(), str(exc))
