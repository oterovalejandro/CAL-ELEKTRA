"""Test templates (the «test templates» of ELEKTRA): list and editor.

A test template calls a hardware setup (bench and frequency points, shown as
a diagram with the EUT in place) and defines the EUT to calibrate, whose
model gives the S-parameter specifications for pass / fail. It also sets the
source power, the trace settings (IFBW, averaging and display smoothing),
calibration, verification and export. Launching it in a folder creates a test,
the record of that measurement.
"""

from __future__ import annotations

import html
import re

import yaml
from pydantic import ValidationError
from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from calelektra.catalog.library import Library, LibraryError, validation_message
from calelektra.core.units import UnitError, parse_frequency
from calelektra.gui import style
from calelektra.gui.icons import icon
from calelektra.gui.setup_diagram import SetupDiagramView
from calelektra.gui.template_widgets import SpecEditor, TraceEditor
from calelektra.gui.test_common import equipment_text, plan_html, setup_state
from calelektra.procedures.cable_results import DEFAULT_REPORT_POINTS
from calelektra.procedures.cable_specs import (
    CABLE_FIELDS,
    VSWR_TOLERANCE_ON,
    bands_text,
    cable_specification,
)
from calelektra.procedures.engine import eut_choices, prepare_run, suitable_setups
from calelektra.procedures.model import (
    ALGORITHMS,
    CALIBRATION_METHODS,
    COUPLER_PORTS,
    DEFAULT_COUPLER_PAIRS,
    DEFAULT_REPORT_TEMPLATE,
    PLANES_NEEDED,
    TRACES,
    ProcedureTemplate,
)
from calelektra.setups.analysis import analyze_setup
from calelektra.setups.paths import default_dut_ports

ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]*$")


class ProcedureEditor(QWidget):
    changed = Signal()
    edit_model = Signal(str)  # model of the EUT: edit its specifications

    def __init__(self, library: Library, parent: QWidget | None = None):
        super().__init__(parent)
        self.library = library
        self.original: ProcedureTemplate | None = None
        self._loading = False
        self.plan = None
        self._check_timer = QTimer(self)
        self._check_timer.setSingleShot(True)
        self._check_timer.setInterval(120)
        self._check_timer.timeout.connect(self.refresh_check)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(12)

        general = style.Card("General", icon_name="procedimientos")
        form = QFormLayout()
        general.body.addLayout(form)
        self.id_edit = QLineEdit()
        self.name = QLineEdit()
        self.version = QLineEdit()
        self.status = QLineEdit()
        self.description = QPlainTextEdit()
        self.description.setMaximumHeight(64)
        self.algorithm = QComboBox()
        for key, text in ALGORITHMS.items():
            self.algorithm.addItem(text, key)
        form.addRow("Identificador *", self.id_edit)
        form.addRow("Nombre *", self.name)
        form.addRow("Versión", self.version)
        form.addRow("Estado", self.status)
        form.addRow("Descripción", self.description)
        form.addRow("Algoritmo", self.algorithm)

        setup_card = style.Card("Hardware setup", icon_name="setups")
        self.setup = QComboBox()
        self.setup.setToolTip("Setups con los planos de referencia que necesita el algoritmo")
        self.setup_info = QLabel()
        self.setup_info.setObjectName("pageSubtitle")
        self.setup_info.setWordWrap(True)
        self.diagram = SetupDiagramView()
        self.diagram.setMinimumHeight(230)
        setup_card.body.addWidget(self.setup)
        setup_card.body.addWidget(self.setup_info)
        setup_card.body.addWidget(self.diagram, 1)

        eut_card = style.Card("EUT (equipo a calibrar)", icon_name="cable")
        self.eut_label = QLabel("EUT")
        self.eut = QComboBox()
        self.eut_info = QLabel()
        self.eut_info.setObjectName("pageSubtitle")
        self.eut_info.setWordWrap(True)
        eut_form = QFormLayout()
        eut_form.addRow(self.eut_label, self.eut)
        self.eut_ports: list[QComboBox] = []
        self.ports_row = QWidget()
        ports_layout = QHBoxLayout(self.ports_row)
        ports_layout.setContentsMargins(0, 0, 0, 0)
        for index in range(2):
            combo = QComboBox()
            combo.setToolTip(f"Puerto del EUT en el plano PR{index + 1} (final del camino de P{index + 1})")
            combo.currentIndexChanged.connect(self._edited)
            self.eut_ports.append(combo)
            ports_layout.addWidget(QLabel(f"PR{index + 1}:"))
            ports_layout.addWidget(combo, 1)
        eut_form.addRow("Puertos del EUT", self.ports_row)
        self.pairs = QListWidget()
        self.pairs.setMaximumHeight(118)
        self.pairs.setToolTip("Pares de puertos del acoplador que se miden: el primero en PR1, el segundo en PR2; los otros dos en las cargas de 50 Ω")
        for first_index, first in enumerate(COUPLER_PORTS):
            for second in COUPLER_PORTS[first_index + 1 :]:
                item = QListWidgetItem(f"{first} (PR1) – {second} (PR2)")
                item.setData(Qt.ItemDataRole.UserRole, (first, second))
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(Qt.CheckState.Checked if (first, second) in DEFAULT_COUPLER_PAIRS else Qt.CheckState.Unchecked)
                self.pairs.addItem(item)
        self.pairs.itemChanged.connect(lambda _item: self._edited())
        self.pairs_label = QLabel("Pares de puertos")
        eut_form.addRow(self.pairs_label, self.pairs)
        self.eut_form = eut_form
        eut_card.body.addLayout(eut_form)
        eut_card.body.addWidget(self.eut_info)
        specs_title = QLabel("Especificaciones S del modelo del EUT (pasa / falla)")
        specs_title.setObjectName("cardTitle")
        self.specs_title = specs_title
        self.specs = SpecEditor(resolver=lambda reference: self._eut_spec_path(reference), read_only=True)
        self.specs.setMinimumHeight(120)
        self.edit_specs = QPushButton(icon("editar", style.PRIMARY.name()), "Editar las especificaciones del modelo…")
        self.edit_specs.clicked.connect(self._edit_model)
        eut_card.body.addWidget(specs_title)
        eut_card.body.addWidget(self.specs)
        eut_card.body.addWidget(self.edit_specs, 0, Qt.AlignmentFlag.AlignLeft)
        # cable by formula: K1, K2 and the VSWR of the connectors come from the model; the tolerances from the template
        self.cable_box = QWidget()
        cable_layout = QVBoxLayout(self.cable_box)
        cable_layout.setContentsMargins(0, 6, 0, 0)
        cable_title = QLabel("Especificación por fórmula (cables): tolerancias")
        cable_title.setObjectName("cardTitle")
        self.cable_info = QLabel()
        self.cable_info.setObjectName("pageSubtitle")
        self.cable_info.setWordWrap(True)
        cable_form = QFormLayout()
        self.il_tolerance = QDoubleSpinBox()
        self.il_tolerance.setRange(-0.01, 100.0)
        self.il_tolerance.setDecimals(2)
        self.il_tolerance.setSingleStep(0.1)
        self.il_tolerance.setSuffix(" dB")
        self.il_tolerance.setSpecialValueText("— (sin tolerancia)")
        self.il_tolerance.setToolTip("Pérdidas de inserción máximas = atenuación nominal A(f) del modelo + esta tolerancia")
        self.vswr_tolerance = QDoubleSpinBox()
        self.vswr_tolerance.setRange(-0.1, 1000.0)
        self.vswr_tolerance.setDecimals(1)
        self.vswr_tolerance.setSuffix(" %")
        self.vswr_tolerance.setSpecialValueText("— (sin tolerancia)")
        self.vswr_tolerance.setToolTip("VSWR máximo = VSWR del cable ensamblado (suma lineal de las reflexiones de sus conectores) · (1 + tolerancia)")
        self.vswr_on = QComboBox()
        for key, text in VSWR_TOLERANCE_ON.items():
            self.vswr_on.addItem(text[0].upper() + text[1:], key)
        self.vswr_on.setToolTip(
            "Sobre el VSWR del cable ensamblado (lo habitual del laboratorio) o sobre el de cada conector, antes de sumar las reflexiones"
        )
        self.vswr_on.currentIndexChanged.connect(self._edited)
        for spin in (self.il_tolerance, self.vswr_tolerance):  # the minimum (special text): no tolerance
            spin.setValue(spin.minimum())
            spin.valueChanged.connect(self._edited)
        cable_form.addRow("Pérdidas de inserción: + ", self.il_tolerance)
        self.vswr_tolerance_label = QLabel("VSWR: + ")
        cable_form.addRow(self.vswr_tolerance_label, self.vswr_tolerance)
        self.vswr_on_label = QLabel("Tolerancia del VSWR")
        cable_form.addRow(self.vswr_on_label, self.vswr_on)
        cable_layout.addWidget(cable_title)
        cable_layout.addWidget(self.cable_info)
        cable_layout.addLayout(cable_form)
        self.cable_form = cable_form
        eut_card.body.addWidget(self.cable_box)

        measurement = style.Card("Medida: potencia y trazas", icon_name="ajustes")
        power_form = QFormLayout()
        self.power = QDoubleSpinBox()
        self.power.setRange(-100, 30)
        self.power.setDecimals(1)
        self.power.setSuffix(" dBm")
        power_form.addRow("Potencia de la fuente", self.power)
        measurement.body.addLayout(power_form)
        self.traces = TraceEditor()
        measurement.body.addWidget(self.traces)

        calibration = style.Card("Calibración", icon_name="vna")
        cal_form = QFormLayout()
        calibration.body.addLayout(cal_form)
        self.method = QComboBox()
        for key, text in CALIBRATION_METHODS.items():
            self.method.addItem(text.split(" (")[0], key)
            self.method.setItemData(self.method.count() - 1, text, Qt.ItemDataRole.ToolTipRole)
        self.save_on_vna = QCheckBox("Guardar la calibración en el VNA")
        self.vna_name = QLineEdit()
        cal_form.addRow("Método", self.method)
        cal_form.addRow(self.save_on_vna)
        cal_form.addRow("Nombre en el VNA", self.vna_name)

        verification = style.Card("Verificación de la calibración", icon_name="ok")
        ver_form = QFormLayout()
        verification.body.addLayout(ver_form)
        self.verify = QCheckBox("Verificar tras calibrar")
        self.tolerance = QDoubleSpinBox()
        self.tolerance.setRange(0.001, 10)
        self.tolerance.setDecimals(3)
        self.tolerance.setSuffix(" dB")
        self.s11_max = QDoubleSpinBox()
        self.s11_max.setRange(-120, 0)
        self.s11_max.setSuffix(" dB")
        ver_form.addRow(self.verify)
        ver_form.addRow("Tolerancia |S21| / reciprocidad", self.tolerance)
        ver_form.addRow("|Γ| máximo de la carga", self.s11_max)

        uncertainty = style.Card("Incertidumbre", icon_name="info")
        unc_form = QFormLayout()
        uncertainty.body.addLayout(unc_form)
        self.uncertainty = QCheckBox("Calcular la incertidumbre de cada traza")
        self.uncertainty.setToolTip("Con el fichero de incertidumbres del VNA del hardware setup; las gráficas la muestran sombreada")
        self.coverage = QDoubleSpinBox()
        self.coverage.setRange(1.0, 3.0)
        self.coverage.setDecimals(2)
        self.coverage.setSingleStep(0.1)
        self.trace_noise = QCheckBox("Ruido de traza (si la IFBW es ≥ la del dato)")
        self.temperature_stability = QCheckBox("Estabilidad con la temperatura (fuera del rango de referencia)")
        self.temperature_stability.setToolTip(
            "Con la temperatura del ensayo, de las condiciones ambientales (fichero .xlsx de la sonda TPH). Hasta que se "
            "cargan, la incertidumbre queda pendiente: se calcula sin esta componente."
        )
        unc_form.addRow(self.uncertainty)
        unc_form.addRow("Factor de cobertura k", self.coverage)
        unc_form.addRow(self.trace_noise)
        unc_form.addRow(self.temperature_stability)

        export = style.Card("Exportación", icon_name="exportar")
        exp_form = QFormLayout()
        export.body.addLayout(exp_form)
        self.data_format = QComboBox()
        for key, text in (("RI", "RI — real / imaginario"), ("MA", "MA — magnitud / ángulo"), ("DB", "DB — dB / ángulo")):
            self.data_format.addItem(text, key)
        self.unit = QComboBox()
        self.unit.addItems(["HZ", "KHZ", "MHZ", "GHZ"])
        self.filename = QLineEdit()
        self.filename.setToolTip("Variables: {dut}, {fecha}, {setup}, {procedimiento}, {ensayo}")
        self.csv = QCheckBox("Exportar también CSV (Excel)")
        exp_form.addRow("Formato Touchstone", self.data_format)
        exp_form.addRow("Unidad de frecuencia", self.unit)
        exp_form.addRow("Nombre de fichero", self.filename)
        exp_form.addRow(self.csv)

        # calibration certificate of a cable (procedures/report.py): what the test record does not say
        report = style.Card("Certificado de calibración (cables)", icon_name="certificado")
        report_form = QFormLayout()
        report.body.addLayout(report_form)
        self.report_procedure = QLineEdit()
        self.report_procedure.setPlaceholderText("Procedimiento de calibración del laboratorio")
        self.report_interval = QLineEdit()
        self.report_interval.setPlaceholderText("p. ej. Anual, Bienal")
        self.report_rows = QLineEdit()
        self.report_rows.setPlaceholderText(f"{DEFAULT_REPORT_POINTS}  o una lista: 10 MHz; 1 GHz; 2 GHz")
        self.report_rows.setToolTip(
            "Filas de las tablas Fwd y Rev: n frecuencias igualmente espaciadas en el barrido (redondeadas a dos cifras "
            "significativas, salvo la primera y la última) o una lista; cada fila lleva los valores del punto medido más cercano"
        )
        self.report_aperture = QSpinBox()
        self.report_aperture.setRange(1, 1000)
        self.report_aperture.setSuffix(" pasos")
        self.report_aperture.setToolTip("Pasos de frecuencia en que se ajusta el retardo de grupo (gráficas, resultados y certificado)")
        self.report_smoothing = QDoubleSpinBox()
        self.report_smoothing.setRange(0.0, 50.0)
        self.report_smoothing.setDecimals(1)
        self.report_smoothing.setSingleStep(0.5)
        self.report_smoothing.setSuffix(" % de los puntos")
        self.report_smoothing.setSpecialValueText("sin suavizado")
        self.report_smoothing.setToolTip(
            "VSWR y retardo de grupo de las tablas: media móvil sobre este porcentaje de los puntos del barrido, "
            "como en los certificados del laboratorio (1 %); el retardo, entre puntos consecutivos. "
            "La pérdida de inserción y las incertidumbres son las del punto."
        )
        self.report_sensor = QComboBox()
        self.report_sensor.setToolTip("Sonda de las condiciones ambientales: va en la tabla de equipos del certificado")
        self.report_template = QLineEdit()
        self.report_template.setPlaceholderText(DEFAULT_REPORT_TEMPLATE)
        self.report_template.setToolTip("Plantilla de Word con etiquetas, relativa a la biblioteca")
        report_form.addRow("Procedimiento", self.report_procedure)
        report_form.addRow("Intervalo de calibración", self.report_interval)
        report_form.addRow("Frecuencias de las tablas", self.report_rows)
        report_form.addRow("Apertura del retardo de grupo", self.report_aperture)
        report_form.addRow("Suavizado de VSWR y retardo", self.report_smoothing)
        report_form.addRow("Sonda ambiental", self.report_sensor)
        report_form.addRow("Plantilla de Word", self.report_template)
        self.report_card = report

        check = style.Card("Comprobación de la plantilla", icon_name="ok")
        self.check_summary = QLabel()
        self.check_summary.setWordWrap(True)
        self.check_summary.setTextFormat(Qt.TextFormat.RichText)
        self.check_issues = QListWidget()
        self.check_issues.setWordWrap(True)
        self.check_issues.setMinimumHeight(60)
        self.check_issues.setMaximumHeight(150)
        check.body.addWidget(self.check_summary)
        check.body.addWidget(self.check_issues)

        options = style.Card("Opciones del algoritmo (YAML)", icon_name="ajustes")
        self.options = QPlainTextEdit()
        self.options.setPlaceholderText("clave: valor (opciones adicionales del algoritmo; normalmente vacío)")
        self.options.setMaximumHeight(100)
        options.body.addWidget(self.options)

        left_column, right_column = QVBoxLayout(), QVBoxLayout()
        right_cards = (check, measurement, calibration, verification, uncertainty, export, report)
        for column, cards in ((left_column, (general, setup_card, eut_card)), (right_column, right_cards)):
            column.setSpacing(12)
            for card in cards:
                column.addWidget(card)
            column.addStretch(1)
        grid.addLayout(left_column, 0, 0)
        grid.addLayout(right_column, 0, 1)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        grid.addWidget(options, 1, 0, 1, 2)
        layout.addLayout(grid)
        layout.addStretch(1)
        for combo in (self.algorithm, self.setup, self.eut, self.method, self.data_format, self.unit):
            combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            combo.setMinimumContentsLength(18)

        for widget in (
            self.id_edit, self.name, self.version, self.status, self.vna_name, self.filename,
            self.report_procedure, self.report_interval, self.report_rows, self.report_template,
        ):  # fmt: skip
            widget.textEdited.connect(self._edited)
        self.report_aperture.valueChanged.connect(self._edited)
        self.report_smoothing.valueChanged.connect(self._edited)
        self.report_sensor.currentIndexChanged.connect(self._edited)
        for widget in (self.description, self.options):
            widget.textChanged.connect(self._edited)
        for widget in (self.method, self.data_format, self.unit):
            widget.currentIndexChanged.connect(self._edited)
        for widget in (self.power, self.tolerance, self.s11_max, self.coverage):
            widget.valueChanged.connect(self._edited)
        for widget in (self.save_on_vna, self.verify, self.csv, self.uncertainty, self.trace_noise, self.temperature_stability):
            widget.toggled.connect(self._edited)
        for widget in (self.coverage, self.trace_noise, self.temperature_stability):
            self.uncertainty.toggled.connect(widget.setEnabled)
        self.traces.changed.connect(self._edited)
        self.algorithm.currentIndexChanged.connect(self._algorithm_changed)
        self.setup.currentIndexChanged.connect(self._setup_changed)
        self.eut.currentIndexChanged.connect(self._eut_changed)

    # ── behaviour ──────────────────────────────────────────────────────────
    def _edited(self, *_args) -> None:
        if not self._loading:
            self.changed.emit()
            self._check_timer.start()

    def _current_setup(self):
        setup_id = self.setup.currentData()
        return self.library.setups.get(setup_id) if setup_id else None

    def _current_eut(self):
        eut_id = self.eut.currentData()
        return self.library.equipment.get(eut_id) if eut_id else None

    def _algorithm_changed(self, *_args) -> None:
        algorithm = self.algorithm.currentData()
        self.traces.set_traces(TRACES[algorithm])
        self.report_card.setVisible(algorithm == "vna_2_puertos")
        current = self.setup.currentData()
        self.setup.blockSignals(True)
        self.setup.clear()
        self.setup.addItem("— Elija el hardware setup —", None)
        for setup in suitable_setups(self.library, algorithm):
            self.setup.addItem(icon("setups", style.PRIMARY.name()), f"{setup.name}  ·  {setup.required_range}", setup.id)
        self.setup.setCurrentIndex(max(self.setup.findData(current), 0) if current else 0)
        self.setup.blockSignals(False)
        self._setup_changed()

    def _setup_changed(self, *_args) -> None:
        setup = self._current_setup()
        _role, label, candidates = eut_choices(self.library, setup)
        self.eut_label.setText(label)
        current = self.eut.currentData()
        self.eut.blockSignals(True)
        self.eut.clear()
        self.eut.addItem("— Elija el EUT —", None)
        for equipment in candidates:
            self.eut.addItem(icon("cable", style.DUT.name()), equipment_text(self.library, equipment), equipment.id)
        self.eut.setCurrentIndex(max(self.eut.findData(current), 0) if current else 0)
        self.eut.blockSignals(False)
        if setup is not None:
            analysis = analyze_setup(setup, self.library)
            plan = analysis.frequency_plan
            vna = ", ".join(setup.ids_for("vna")) or "—"
            text = f"Rango requerido {setup.required_range} · {plan.describe() if plan else 'frecuencias con errores'} · VNA {vna}"
            if not analysis.ok:
                text += " · <span style='color:#c0392b'><b>el setup tiene errores</b></span>"
            self.setup_info.setText(text)
        else:
            self.setup_info.setText("Las frecuencias de medida las da el hardware setup.")
        self._eut_changed()

    def _update_ports(self, wanted: list[str] | None = None) -> None:
        """Ports of the EUT on each plane (not for a coupler: it is measured by pairs)."""
        algorithm = self.algorithm.currentData()
        coupler = algorithm == "vna_acoplador"
        needed = PLANES_NEEDED[algorithm]
        self.eut_form.setRowVisible(self.ports_row, not coupler)
        self.eut_form.setRowVisible(self.pairs, coupler)
        ports = default_dut_ports(self.library, self._current_eut())
        wanted = wanted if wanted is not None else [combo.currentData() for combo in self.eut_ports]
        for index, combo in enumerate(self.eut_ports):
            combo.blockSignals(True)
            combo.clear()
            combo.addItem("(orden de su categoría)" if not ports else f"{ports[index]} (orden de su categoría)" if index < len(ports) else "—", None)
            for port in ports:
                combo.addItem(port, port)
            value = wanted[index] if index < len(wanted) else None
            if value and combo.findData(value) < 0:
                combo.addItem(f"{value}  ⚠ el EUT no lo tiene", value)
            combo.setCurrentIndex(max(combo.findData(value), 0) if value else 0)
            combo.setVisible(index < needed)
            combo.blockSignals(False)
        layout = self.ports_row.layout()
        for index in range(layout.count()):
            widget = layout.itemAt(index).widget()
            if isinstance(widget, QLabel):
                widget.setVisible(index // 2 < needed)

    def eut_port_values(self) -> list[str]:
        values = [combo.currentData() for combo in self.eut_ports[: PLANES_NEEDED[self.algorithm.currentData()]]]
        return [value for value in values if value] if all(values) else []

    def pair_values(self) -> list[tuple[str, str]]:
        return [
            self.pairs.item(row).data(Qt.ItemDataRole.UserRole)
            for row in range(self.pairs.count())
            if self.pairs.item(row).checkState() == Qt.CheckState.Checked
        ]

    def set_pairs(self, pairs: list[tuple[str, str]]) -> None:
        wanted = {tuple(pair) for pair in pairs} or set(DEFAULT_COUPLER_PAIRS)
        self.pairs.blockSignals(True)
        for row in range(self.pairs.count()):
            item = self.pairs.item(row)
            first, second = item.data(Qt.ItemDataRole.UserRole)
            checked = (first, second) in wanted or (second, first) in wanted
            item.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
            if (second, first) in wanted:  # pair given the other way round: keep its order
                item.setData(Qt.ItemDataRole.UserRole, (second, first))
                item.setText(f"{second} (PR1) – {first} (PR2)")
        self.pairs.blockSignals(False)

    def _eut_changed(self, *_args) -> None:
        equipment = self._current_eut()
        model = self.library.model_of(equipment) if equipment else None
        self._update_ports()
        coupler = self.algorithm.currentData() == "vna_acoplador"
        formula_only = model is not None and not model.s_specifications and self._has_cable_formula(equipment)
        if coupler:
            title = "Valores nominales del modelo del acoplador (pasa / falla)"
        elif formula_only:
            title = "Especificaciones del modelo del EUT (pasa / falla): por fórmula, sin ficheros S2P"
        else:
            title = "Especificaciones S del modelo del EUT (pasa / falla)"
        self.specs_title.setText(title)
        self.specs.setVisible(not coupler and not formula_only)  # a cable specified by formula only: no S2P files
        self._show_cable_formula(equipment)
        self.specs.set_traces(TRACES[self.algorithm.currentData()])
        self.specs.load(model.s_specifications if model else [])
        self.edit_specs.setEnabled(model is not None)
        if equipment is not None:
            info = [f"Modelo {model.display_name}" if model else equipment.model_id]
            range_info = self.library.frequency_range_of(equipment).range
            if range_info is not None:
                info.append(f"rango {range_info}")
            self.eut_info.setText(" · ".join(info))
        else:
            self.eut_info.setText("Su modelo da las especificaciones S del pasa / falla.")
        self._edited()

    def _show_cable_formula(self, equipment) -> None:
        """Tolerances of the specification by formula: for the S-parameter and transmission tests."""
        algorithm = self.algorithm.currentData()
        self.cable_box.setVisible(algorithm in ("vna_2_puertos", "vna_transmision"))
        self.cable_form.setRowVisible(self.vswr_tolerance, algorithm == "vna_2_puertos")
        self.cable_form.setRowVisible(self.vswr_on, algorithm == "vna_2_puertos")
        spec = None
        if equipment is not None:
            spec = cable_specification({key: self.library.field(equipment, key) for key in CABLE_FIELDS}, None, None, None)
        if spec is None:
            self.cable_info.setText("El modelo del EUT no tiene especificación por fórmula (K1, K2 y VSWR de los conectores): no se aplican.")
            return
        parts = []
        if spec.has_attenuation:
            parts.append(f"Atenuación nominal {spec.formula_text()}")
        if spec.has_return_loss:
            a, b = spec.connector_vswr
            connectors = bands_text(a) if a == b else f"{bands_text(a)} y {bands_text(b)}"
            parts.append(f"VSWR de los conectores {connectors} → cable ensamblado {bands_text(spec.cable_vswr_bands())}")
        parts += spec.problems
        self.cable_info.setText(html.escape(" · ".join(parts)))

    def _has_cable_formula(self, equipment) -> bool:
        return equipment is not None and cable_specification({key: self.library.field(equipment, key) for key in CABLE_FIELDS}, None, None, None) is not None

    def cable_tolerance_values(self) -> dict[str, float]:
        values = {"perdidas_insercion_db": self.il_tolerance, "vswr_pct": self.vswr_tolerance}
        return {key: spin.value() for key, spin in values.items() if spin.value() > spin.minimum()}

    def report_values(self) -> dict:
        """``informe`` of the template: the settings of the certificate of a cable."""
        rows_text = self.report_rows.text().strip()
        rows: int | list[str] | None = None
        if rows_text.isdigit():
            rows = int(rows_text)
        elif rows_text:
            rows = [part.strip() for part in rows_text.split(";") if part.strip()]
            for part in rows:
                try:
                    parse_frequency(part)
                except UnitError as exc:
                    raise ValueError(f"Frecuencias del certificado: «{part}» no es una frecuencia ({exc})") from exc
        values = {
            "plantilla": self.report_template.text().strip() or None,
            "procedimiento": self.report_procedure.text().strip() or None,
            "intervalo": self.report_interval.text().strip() or None,
            "frecuencias": rows,
            "apertura_retardo": self.report_aperture.value(),
            "suavizado_pct": self.report_smoothing.value() or None,
            "sonda_ambiental": self.report_sensor.currentData(),
        }
        return {key: value for key, value in values.items() if value is not None}

    def _eut_spec_path(self, reference: str):
        equipment = self._current_eut()
        model = self.library.model_of(equipment) if equipment else None
        return self.library.spec_path(model, reference) if model else self.library.root / reference

    def _edit_model(self) -> None:
        equipment = self._current_eut()
        if equipment is not None:
            self.edit_model.emit(equipment.model_id)

    def reload_specs(self) -> None:
        """The model of the EUT may have changed (its specifications)."""
        self._eut_changed()
        self.refresh_check()

    def refresh_check(self) -> None:
        """Check the template as it is in the editor (setup, EUT, specifications, VNA limits)."""
        self.check_issues.clear()
        try:
            procedure = self.build()
        except ValueError as exc:
            self.plan = None
            self.check_summary.setText(f"<span style='color:#c0392b'>{exc}</span>")
            self._show_diagram(None)
            return
        self.plan = prepare_run(self.library, procedure)
        self.check_summary.setText(plan_html(self.library, self.plan))
        for issue in sorted(self.plan.issues, key=lambda i: -int(i.severity)):
            self.check_issues.addItem(QListWidgetItem(style.severity_icon(self, issue.severity), issue.message))
        if not self.plan.issues:
            self.check_issues.addItem(QListWidgetItem(style.severity_icon(self, None), "Lista para lanzar: hardware setup, EUT y especificaciones compatibles"))
        self._show_diagram(self.plan)

    def _show_diagram(self, plan) -> None:
        if plan is not None and plan.setup is not None and plan.template is not None:
            self.diagram.show_setup(plan.template, plan.setup, self.library, plan.analysis, setup_state(plan.template))
        else:
            setup = self._current_setup()
            template = self.library.resolved_template(setup.template) if setup else None
            if setup is None or template is None:
                self.diagram.scene().clear()
                return
            self.diagram.show_setup(template, setup, self.library, None, setup_state(template))
        QTimer.singleShot(0, self.diagram.fit)

    # ── load / build ───────────────────────────────────────────────────────
    def load(self, procedure: ProcedureTemplate, is_new: bool = False) -> None:
        self._loading = True
        self.original = None if is_new else procedure
        self.id_edit.setText(procedure.id)
        self.id_edit.setReadOnly(not is_new)
        self.name.setText(procedure.name)
        self.version.setText(procedure.version)
        self.status.setText(procedure.status or "")
        self.description.setPlainText(procedure.description or "")
        self.algorithm.blockSignals(True)
        self.algorithm.setCurrentIndex(max(self.algorithm.findData(procedure.algorithm), 0))
        self.algorithm.blockSignals(False)
        # the wanted setup and EUT are kept as current data while the lists are filled
        for combo, value in ((self.setup, procedure.setup_id), (self.eut, procedure.eut_id)):
            combo.blockSignals(True)
            combo.clear()
            combo.addItem("—", value)
            combo.blockSignals(False)
        self._algorithm_changed()
        if procedure.setup_id and self.setup.currentData() != procedure.setup_id:
            self.setup.addItem(icon("error", style.ERROR.name()), f"{procedure.setup_id} (no existe o no sirve para el algoritmo)", procedure.setup_id)
            self.setup.setCurrentIndex(self.setup.count() - 1)
        if procedure.eut_id and self.eut.currentData() != procedure.eut_id:
            self.eut.blockSignals(True)
            self.eut.addItem(icon("error", style.ERROR.name()), f"{procedure.eut_id} (no existe o no sirve para el setup)", procedure.eut_id)
            self.eut.setCurrentIndex(self.eut.count() - 1)
            self.eut.blockSignals(False)
            self._eut_changed()
        self._update_ports(list(procedure.eut_ports))
        self.set_pairs(list(procedure.pairs))
        sweep = procedure.sweep
        self.power.setValue(sweep.power_dbm)
        self.traces.load(sweep)
        calibration = procedure.calibration
        self.method.setCurrentIndex(max(self.method.findData(calibration.method), 0))
        self.save_on_vna.setChecked(calibration.save_on_vna)
        self.vna_name.setText(calibration.vna_cal_name)
        verification = procedure.verification
        self.verify.setChecked(verification.active)
        self.tolerance.setValue(verification.s21_tolerance_db)
        self.s11_max.setValue(verification.s11_max_db)
        uncertainty = procedure.uncertainty
        self.uncertainty.setChecked(uncertainty.active)
        self.coverage.setValue(uncertainty.coverage_factor)
        self.trace_noise.setChecked(uncertainty.trace_noise)
        self.temperature_stability.setChecked(uncertainty.temperature_stability)
        tolerances = procedure.cable_tolerances
        self.il_tolerance.setValue(self.il_tolerance.minimum() if tolerances.insertion_loss_db is None else tolerances.insertion_loss_db)
        self.vswr_tolerance.setValue(self.vswr_tolerance.minimum() if tolerances.vswr_pct is None else tolerances.vswr_pct)
        self.vswr_on.setCurrentIndex(max(self.vswr_on.findData(tolerances.vswr_on), 0))
        for widget in (self.coverage, self.trace_noise, self.temperature_stability):
            widget.setEnabled(uncertainty.active)
        export = procedure.export
        self.data_format.setCurrentIndex(max(self.data_format.findData(export.data_format), 0))
        self.unit.setCurrentText(export.frequency_unit)
        self.filename.setText(export.filename)
        self.csv.setChecked(export.csv)
        self.options.setPlainText(yaml.safe_dump(procedure.options, allow_unicode=True, sort_keys=False) if procedure.options else "")
        report = procedure.report
        self.report_procedure.setText(report.procedure or "")
        self.report_interval.setText(report.interval or "")
        rows = report.frequencies
        self.report_rows.setText("; ".join(rows) if isinstance(rows, list) else ("" if rows is None else str(rows)))
        self.report_aperture.setValue(report.aperture)
        self.report_smoothing.setValue(report.smoothing_pct)
        self.report_sensor.clear()
        self.report_sensor.addItem("— (ninguna)", None)
        for equipment in sorted(self.library.equipment_in_categories(["sensor_ambiental"]), key=lambda e: e.id):
            self.report_sensor.addItem(icon("condiciones", style.PRIMARY.name()), equipment_text(self.library, equipment), equipment.id)
        if report.ambient_sensor and self.report_sensor.findData(report.ambient_sensor) < 0:
            self.report_sensor.addItem(icon("error", style.ERROR.name()), f"{report.ambient_sensor} (no existe)", report.ambient_sensor)
        self.report_sensor.setCurrentIndex(max(self.report_sensor.findData(report.ambient_sensor), 0))
        self.report_template.setText(report.template or "")
        self._loading = False
        self._check_timer.stop()
        self.refresh_check()

    def build(self) -> ProcedureTemplate:
        procedure_id = self.id_edit.text().strip()
        if not ID_PATTERN.match(procedure_id):
            raise ValueError("Identificador no válido (letras, números, «-», «_» o «.»)")
        sweep = {"potencia_dbm": self.power.value(), **self.traces.values()}
        try:
            options = yaml.safe_load(self.options.toPlainText()) or {}
        except yaml.YAMLError as exc:
            raise ValueError(f"Opciones: YAML no válido ({exc})") from exc
        if not isinstance(options, dict):
            raise ValueError("Opciones: se espera un diccionario YAML (clave: valor)")
        data = {
            "formato": "calelektra/procedimiento@0.3",
            "id": procedure_id,
            "nombre": self.name.text().strip(),
            "version": self.version.text().strip() or "1.0",
            "estado": self.status.text().strip() or None,
            "descripcion": self.description.toPlainText().strip() or None,
            "algoritmo": self.algorithm.currentData(),
            "hardware_setup": self.setup.currentData(),
            "eut": self.eut.currentData(),
            "puertos_eut": self.eut_port_values() if self.algorithm.currentData() != "vna_acoplador" else [],
            "pares": [list(pair) for pair in self.pair_values()] if self.algorithm.currentData() == "vna_acoplador" else [],
            "barrido": sweep,
            "calibracion": {
                "metodo": self.method.currentData(),
                "guardar_en_vna": self.save_on_vna.isChecked(),
                "nombre_en_vna": self.vna_name.text().strip() or "CALELEKTRA_{ensayo}",
            },
            "verificacion": {
                "activa": self.verify.isChecked(),
                "tolerancia_s21_db": self.tolerance.value(),
                "s11_max_db": self.s11_max.value(),
            },
            "incertidumbre": {
                "activa": self.uncertainty.isChecked(),
                "k": self.coverage.value(),
                "ruido_traza": self.trace_noise.isChecked(),
                "estabilidad_temperatura": self.temperature_stability.isChecked(),
            },
            "tolerancias_cable": (
                {**self.cable_tolerance_values(), "vswr_sobre": self.vswr_on.currentData()}
                if self.algorithm.currentData() in ("vna_2_puertos", "vna_transmision")
                else {}
            ),
            "exportacion": {
                "formato": self.data_format.currentData(),
                "unidad_frecuencia": self.unit.currentText(),
                "nombre_fichero": self.filename.text().strip() or "{dut}_{fecha}",
                "csv": self.csv.isChecked(),
            },
            "opciones": options,
            "informe": self.report_values(),
        }
        if not data["nombre"]:
            raise ValueError("Indique el nombre de la plantilla de ensayo")
        try:
            procedure = ProcedureTemplate.model_validate({k: v for k, v in data.items() if v is not None})
        except ValidationError as exc:
            raise ValueError(validation_message(exc)) from exc
        if self.original is not None:
            procedure.set_source_path(self.original.source_path)
        return procedure


class ProceduresView(QWidget):
    launch = Signal(str)  # template id: launch it in a folder
    procedures_changed = Signal()
    edit_model = Signal(str)
    title = "Plantillas de ensayo"

    def __init__(self, library: Library, parent: QWidget | None = None):
        super().__init__(parent)
        self.library = library
        self.dirty = False
        self.is_new = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 10)
        self.header = style.PageHeader(
            "Plantillas de ensayo",
            "Las «test templates» de ELEKTRA: cada plantilla llama a un hardware setup (banco y frecuencias) y define el "
            "EUT, cuyo modelo da las especificaciones del pasa / falla. Al lanzarla en una carpeta se crea un ensayo: "
            "el registro de la medida.",
        )
        layout.addWidget(self.header)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.list = QListWidget()
        self.list.setMinimumWidth(300)
        self.list.currentItemChanged.connect(self._selected)
        self.editor = ProcedureEditor(library)
        self.editor.changed.connect(self._changed)
        self.editor.edit_model.connect(self.edit_model.emit)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.editor)
        splitter.addWidget(self.list)
        splitter.addWidget(scroll)
        splitter.setStretchFactor(1, 3)
        layout.addWidget(splitter, 1)

        self.action_new = QAction(icon("nuevo", style.PRIMARY.name()), "Nueva\nplantilla", self)
        self.action_copy = QAction(icon("duplicar", style.PRIMARY.name()), "Duplicar", self)
        self.action_save = QAction(icon("guardar", style.PRIMARY.name()), "Guardar", self)
        self.action_delete = QAction(icon("eliminar", style.ERROR.name()), "Eliminar", self)
        self.action_launch = QAction(icon("ejecutar", style.OK.name()), "Lanzar\nensayo", self)
        self.action_new.triggered.connect(lambda: self.new_procedure())
        self.action_copy.triggered.connect(self.duplicate)
        self.action_save.triggered.connect(self.save)
        self.action_delete.triggered.connect(self.delete)
        self.action_launch.triggered.connect(lambda: self._current_id() and self.launch.emit(self._current_id()))
        self.reload()

    def ribbon_actions(self) -> list[QAction | None]:
        return [self.action_new, self.action_copy, self.action_save, self.action_delete, None, self.action_launch]

    def set_library(self, library: Library) -> None:
        self.library = library
        self.editor.library = library
        self.reload()

    def reload(self, select: str | None = None) -> None:
        current = select or self._current_id()
        self.list.blockSignals(True)
        self.list.clear()
        for procedure in sorted(self.library.procedures.values(), key=lambda p: p.name):
            setup = self.library.setups.get(procedure.setup_id or "")
            details = f"{setup.name if setup else procedure.setup_id or 'sin hardware setup'} · EUT {procedure.eut_id or '—'}"
            item = QListWidgetItem(icon("procedimientos", style.PRIMARY.name()), f"{procedure.name}\n{details}")
            item.setData(Qt.ItemDataRole.UserRole, procedure.id)
            item.setToolTip(str(procedure.source_path or ""))
            self.list.addItem(item)
        row = next((i for i in range(self.list.count()) if self.list.item(i).data(Qt.ItemDataRole.UserRole) == current), 0)
        if self.list.count():
            self.list.setCurrentRow(row)
        self.list.blockSignals(False)
        if not self.dirty and self.list.currentItem() is not None:
            self._selected(self.list.currentItem())  # a form being edited is never overwritten by a reload…
        elif self.dirty:
            self.editor.refresh_check()  # … but it is checked again with the current equipment and setups
        self._update_actions()

    def _current_id(self) -> str | None:
        item = self.list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def _selected(self, item: QListWidgetItem | None, previous: QListWidgetItem | None = None) -> None:
        if self.dirty and previous is not None and not self._confirm_discard():
            self.list.blockSignals(True)
            self.list.setCurrentItem(previous)
            self.list.blockSignals(False)
            return
        if item is None:
            return
        procedure = self.library.procedures.get(item.data(Qt.ItemDataRole.UserRole))
        if procedure is not None:
            self.editor.load(procedure)
            self.is_new = False
            self._set_dirty(False)

    def _confirm_discard(self) -> bool:
        answer = QMessageBox.question(self, "Plantilla de ensayo", "Hay cambios sin guardar. ¿Descartarlos?")
        return answer == QMessageBox.StandardButton.Yes

    def _changed(self) -> None:
        self._set_dirty(True)

    def _set_dirty(self, dirty: bool) -> None:
        self.dirty = dirty
        self.header.title.setText("Plantillas de ensayo" + (" — cambios sin guardar" if dirty else ""))
        self._update_actions()

    def _update_actions(self) -> None:
        has = self._current_id() is not None
        self.action_save.setEnabled(self.dirty)
        self.action_copy.setEnabled(has)
        self.action_delete.setEnabled(has and not self.is_new)
        self.action_launch.setEnabled(has and not self.dirty and not self.is_new)
        self.action_launch.setToolTip(
            "Guarde la plantilla para lanzarla" if self.dirty else "Lanzar la plantilla en una carpeta: crea el ensayo (registro de la medida)"
        )

    def new_procedure(self, setup_id: str | None = None) -> None:
        """New template; with ``setup_id`` it calls that hardware setup."""
        if self.dirty and not self._confirm_discard():
            return
        algorithm = "vna_2_puertos"
        setup = self.library.setups.get(setup_id or "")
        if setup is not None:
            template = self.library.resolved_template(setup.template)
            if template is not None and template.algorithms and template.algorithms[0] in ALGORITHMS:
                algorithm = template.algorithms[0]  # the test the setup is made for
        new_id, counter = "nueva_plantilla", 2
        while new_id in self.library.procedures:
            new_id, counter = f"nueva_plantilla_{counter}", counter + 1
        base = ProcedureTemplate(id=new_id, nombre="Nueva plantilla de ensayo", algoritmo=algorithm, hardware_setup=setup_id)
        self.editor.load(base, is_new=True)
        self.is_new = True
        self._set_dirty(True)

    def duplicate(self) -> None:
        procedure_id = self._current_id()
        if procedure_id is None:
            return
        original = self.library.procedures[procedure_id]
        new_id, counter = f"{procedure_id}_copia", 2
        while new_id in self.library.procedures:
            new_id, counter = f"{procedure_id}_copia{counter}", counter + 1
        copy = original.model_copy(deep=True, update={"id": new_id, "name": f"{original.name} (copia)"})
        self.editor.load(copy, is_new=True)
        self.is_new = True
        self._set_dirty(True)

    def save(self) -> bool:
        try:
            procedure = self.editor.build()
        except ValueError as exc:
            QMessageBox.warning(self, "Guardar plantilla", str(exc))
            return False
        if self.is_new and procedure.id in self.library.procedures:
            QMessageBox.warning(self, "Guardar plantilla", f"Ya existe una plantilla «{procedure.id}».")
            return False
        try:
            self.library.save_procedure(procedure)
        except OSError as exc:
            QMessageBox.critical(self, "Guardar plantilla", str(exc))
            return False
        self.is_new = False
        self._set_dirty(False)
        self.reload(select=procedure.id)
        self.procedures_changed.emit()
        return True

    def delete(self) -> None:
        procedure_id = self._current_id()
        if procedure_id is None:
            return
        answer = QMessageBox.question(
            self,
            "Eliminar plantilla",
            f"¿Eliminar la plantilla de ensayo «{self.library.procedures[procedure_id].name}»? Los ensayos ya lanzados se conservan.",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            self.library.delete_procedure(procedure_id)
        except (LibraryError, OSError) as exc:
            QMessageBox.warning(self, "Eliminar plantilla", str(exc))
            return
        self._set_dirty(False)
        self.reload()
        self.procedures_changed.emit()
