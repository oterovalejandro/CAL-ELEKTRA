"""Tests (ELEKTRA-like): new-test dialog, test window and operator dialogs.

A test is launched in a folder chosen by the user, from a test template. The
new-test dialog defines it: template, hardware setup (shown as a diagram with
the cable to calibrate in place), cable to calibrate (DUT), specification
files for pass / fail, and operator. It creates the prepared test
(``Library.create_test``) and the test window runs it.

The procedure runs in a worker thread. Operator requests are shown in the GUI
thread through a blocking queued signal, so the run waits for the answer just
like the real measurement waits for the operator.
"""

from __future__ import annotations

import html
from datetime import date, datetime
from pathlib import Path

import numpy as np
from PySide6.QtCore import QObject, QSettings, Qt, QThread, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QAction, QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSplitter,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from calelektra.catalog.library import Library, LibraryError
from calelektra.catalog.model import Equipment
from calelektra.core.issues import Severity
from calelektra.core.touchstone import TouchstoneError, read_touchstone
from calelektra.core.units import format_frequency
from calelektra.gui import style
from calelektra.gui.icons import icon
from calelektra.gui.illustrations import illustration
from calelektra.gui.plots import SParameterPlot
from calelektra.gui.setup_diagram import SetupDiagramView
from calelektra.gui.template_widgets import SpecEditor
from calelektra.gui.test_folders import TestFolderTree
from calelektra.instruments.factory import is_simulated
from calelektra.procedures.engine import (
    STEPS,
    AutoOperator,
    ProcedureRunner,
    PromptRequest,
    RunListener,
    RunPlan,
    RunResult,
    compatible,
    dut_role_of,
    prepare_run,
    with_dut,
)
from calelektra.procedures.model import ALGORITHMS, ProcedureTemplate, SweepSettings
from calelektra.procedures.record import TestDefinition, TestRecord
from calelektra.procedures.specs import VERDICTS, SpecCurve, spec_curves
from calelektra.setups.analysis import analyze_setup
from calelektra.setups.model import ConfiguredSetup

SETTINGS = ("CAL-ELEKTRA", "CAL-ELEKTRA")


# ── helpers shared with the test explorer ──────────────────────────────────
def compatible_setups(library: Library, procedure: ProcedureTemplate) -> list[ConfiguredSetup]:
    """Setups whose template (or an ancestor) is in the compatible templates of the test template."""
    return sorted((s for s in library.setups.values() if compatible(library, procedure, s)), key=lambda s: s.name)


def dut_choices(library: Library, setup: ConfiguredSetup | None) -> tuple[str | None, str, list[Equipment]]:
    """DUT role of the setup, its label and the equipment of its categories."""
    template = library.resolved_template(setup.template) if setup is not None else None
    role_key = dut_role_of(template)
    if template is None or role_key is None:
        return None, "Equipo a calibrar", []
    role = template.roles[role_key]
    equipment = sorted(library.equipment_in_categories(role.categories), key=lambda e: e.id)
    return role_key, role.display(role_key), equipment


def equipment_text(library: Library, equipment: Equipment) -> str:
    model = library.model_of(equipment)
    text = f"{equipment.id} — {model.designation}" if model else equipment.id
    if equipment.serial_number:
        text += f" · nº serie {equipment.serial_number}"
    return text


def trace_text(sweep: SweepSettings, traces: tuple[str, ...]) -> str:
    """IFBW, averaging and smoothing of all traces, grouped when they are set per trace."""

    def one(ifbw: float, averages: int, smoothing: float) -> str:
        text = f"IFBW {format_frequency(ifbw)}, {averages} promedio{'s' if averages != 1 else ''}"
        return text + (f", suavizado {smoothing:g} %" if smoothing else "")

    if not sweep.per_trace:
        return one(sweep.if_bandwidth, sweep.averages, sweep.smoothing_pct)
    groups: dict[tuple, list[str]] = {}
    for trace in traces:
        groups.setdefault(sweep.trace(trace), []).append(trace)
    if len(groups) == 1:
        return one(*next(iter(groups)))
    return "; ".join(f"{', '.join(names)}: {one(*settings)}" for settings, names in groups.items())


def plan_html(library: Library, plan: RunPlan) -> str:
    procedure = plan.procedure
    sweep = procedure.sweep
    muted = "<span style='color:#5b6b80'>{}</span>"
    lines = [f"<b>Plantilla:</b> {html.escape(procedure.name)} " + muted.format(html.escape(ALGORITHMS[procedure.algorithm].split(" (")[0]))]
    frequencies = plan.frequency_plan.describe() if plan.frequency_plan is not None else sweep.summary()
    lines.append(f"<b>Frecuencias:</b> {html.escape(frequencies)} · {sweep.power_dbm:g} dBm")
    lines.append(f"<b>Trazas:</b> {html.escape(trace_text(sweep, procedure.traces))}")
    if plan.vna is not None:
        simulated = " <span style='color:#6c3483'><b>(SIMULADO)</b></span>" if is_simulated(plan.vna, library) else ""
        lines.append(f"<b>VNA:</b> {plan.vna.id}{simulated}")
    lines.append(f"<b>Equipo a calibrar:</b> {html.escape(equipment_text(library, plan.dut)) if plan.dut else '—'}")
    design = plan.design
    if procedure.calibration.method == "existente":
        lines.append("<b>Calibración:</b> se usa la calibración activa del VNA")
    elif design is not None and design.method:
        planes = ", ".join(f"{p.name} → P{p.vna_port} ({p.connector.label() if p.connector else '?'})" for p in design.planes)
        kit = f" · kit {design.kit.id}" if design.kit else " · kit del VNA"
        lines.append(f"<b>Calibración:</b> {design.method}{kit}<br>&nbsp;&nbsp;{html.escape(design.reason)}<br>&nbsp;&nbsp;Planos: {html.escape(planes)}")
    if procedure.specifications:
        specs = "; ".join(f"{s.name} ({s.describe()})" for s in procedure.specifications)
        lines.append(f"<b>Especificaciones:</b> {html.escape(specs)}")
    else:
        lines.append("<b>Especificaciones:</b> ninguna (sin veredicto pasa / falla)")
    if plan.lines != ["—"]:
        lines.append(f"<b>Líneas:</b> {', '.join(plan.lines)}")
    return "<br>".join(lines)


def is_logarithmic(record: TestRecord) -> bool:
    return record.sweep.get("modo", record.sweep.get("tipo")) == "logaritmico"


def record_datasets(record: TestRecord) -> list[tuple[str, np.ndarray, np.ndarray]]:
    """Measured data of a test, read back from its result Touchstone files.

    Only the files at the top of the test folder are results: the
    specification files kept in ``especificaciones/`` are not measurements.
    """
    datasets = []
    folder = record.folder
    if folder is None:
        return datasets
    for name in record.files:
        if "/" in name or not name.lower().endswith((".s1p", ".s2p")):
            continue
        try:
            data = read_touchstone(folder / name)
        except (TouchstoneError, OSError):
            continue
        label = name.rsplit(".", 1)[0].rsplit("_", 1)[-1] if name.lower().endswith(".s1p") else "DUT"
        datasets.append((label, data.frequencies, data.s))
    return datasets


def record_spec_curves(library: Library, record: TestRecord) -> list[SpecCurve]:
    """Curves of the specification files of a test (its own copies)."""
    if record.definition is None:
        return []
    try:
        template = record.definition.template()
    except ValueError:
        return []
    curves: list[SpecCurve] = []
    for spec in template.specifications:
        curves += spec_curves(spec, library.procedure_file(spec.file, record.folder))
    return curves


def verdict_chip(verdict: str | None) -> QLabel | None:
    if not verdict or verdict == "SIN_ESPECIFICACIONES":
        return None
    label = style.chip(VERDICTS.get(verdict, verdict).upper(), style.VERDICT_COLORS.get(verdict, style.NEUTRAL))
    label.setToolTip("Veredicto frente a los ficheros de especificación")
    return label


def setup_state(template) -> str | None:
    states = template.connection_states if template else {}
    return "medida" if "medida" in states else next(iter(states), None)


# ── new test ───────────────────────────────────────────────────────────────
class NewTestDialog(QDialog):
    """Launch a test template in a folder: template, setup (with its diagram), cable to calibrate and specifications.

    With ``record`` (a prepared test) the dialog changes its definition instead.
    """

    def __init__(
        self,
        library: Library,
        procedure_id: str | None = None,
        setup_id: str | None = None,
        folder: Path | None = None,
        record: TestRecord | None = None,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.library = library
        self.record = record
        self.created: TestRecord | None = None
        self.plan: RunPlan | None = None
        self._snapshot = record.definition.template() if record is not None and record.definition else None
        self._preferred_setup = setup_id or (record.definition.setup_id if record and record.definition else None)
        self._preferred_dut = record.definition.dut_id if record is not None and record.definition else None
        self._specs_from: str | None = None
        self._name_edited = record is not None
        editing = record is not None
        self.setWindowTitle("Definición del ensayo" if editing else "Nuevo ensayo")
        self.resize(1320, 820)
        layout = QVBoxLayout(self)
        layout.addWidget(
            style.PageHeader(
                "Definición del ensayo" if editing else "Nuevo ensayo",
                "Elija la carpeta, la plantilla de ensayo, el hardware setup y el equipo a calibrar. Las especificaciones "
                "(ficheros S2P) dan el veredicto pasa / falla. La comprobación previa usa los campos de los equipos.",
            )
        )
        splitter = QSplitter(Qt.Orientation.Horizontal)

        left = style.Card("Ensayo", icon_name="ensayos")
        folder_title = QLabel("Carpeta del ensayo")
        folder_title.setObjectName("cardTitle")
        self.folders = TestFolderTree(library)
        self.folders.setMinimumHeight(150)
        folder_buttons = QHBoxLayout()
        self.new_folder_button = QPushButton(icon("carpeta_nueva", style.PRIMARY.name()), "Nueva carpeta…")
        self.external_button = QPushButton(icon("carpeta_externa", style.PRIMARY.name()), "Carpeta externa…")
        self.new_folder_button.clicked.connect(self.folders.create_folder)
        self.external_button.clicked.connect(self.folders.add_external)
        folder_buttons.addWidget(self.new_folder_button)
        folder_buttons.addWidget(self.external_button)
        folder_buttons.addStretch(1)
        left.body.addWidget(folder_title)
        left.body.addWidget(self.folders, 1)
        left.body.addLayout(folder_buttons)
        left.body.addWidget(style.separator())
        form = QFormLayout()
        self.name = QLineEdit()
        self.name.setPlaceholderText("Nombre del ensayo")
        self.procedure = QComboBox()
        for procedure in sorted(library.procedures.values(), key=lambda p: p.name):
            self.procedure.addItem(icon("procedimientos", style.PRIMARY.name()), procedure.name, procedure.id)
        if self._snapshot is not None:
            index = self.procedure.findData(self._snapshot.id)
            if index < 0:
                self.procedure.addItem(icon("procedimientos", style.PRIMARY.name()), f"{self._snapshot.name} (del ensayo)", self._snapshot.id)
        self.setup = QComboBox()
        self.dut_label = QLabel("Equipo a calibrar")
        self.dut = QComboBox()
        self.operator = QLineEdit()
        self.operator.setPlaceholderText("Nombre del operador")
        self.operator.setText(record.operator if record is not None and record.operator else str(QSettings(*SETTINGS).value("operador", "")))
        self.automatic = QCheckBox("Simulación automática (sin diálogos al operador)")
        self.automatic.setToolTip("Solo con el VNA simulado: el banco simulado conecta los estándares y el DUT sin preguntar.")
        for combo in (self.procedure, self.setup, self.dut):
            combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            combo.setMinimumContentsLength(24)
        form.addRow("Nombre", self.name)
        form.addRow("Plantilla de ensayo", self.procedure)
        form.addRow("Hardware setup", self.setup)
        form.addRow(self.dut_label, self.dut)
        form.addRow("Operador", self.operator)
        form.addRow(self.automatic)
        left.body.addLayout(form)

        right = QSplitter(Qt.Orientation.Vertical)
        setup_card = style.Card("Hardware setup del ensayo", icon_name="setups")
        self.setup_info = QLabel()
        self.setup_info.setObjectName("pageSubtitle")
        self.setup_info.setWordWrap(True)
        self.diagram = SetupDiagramView()
        self.diagram.setMinimumHeight(240)
        setup_card.body.addWidget(self.setup_info)
        setup_card.body.addWidget(self.diagram, 1)
        self.tabs = QTabWidget()
        specs_page = QWidget()
        specs_layout = QVBoxLayout(specs_page)
        specs_layout.setContentsMargins(8, 8, 8, 8)
        note = QLabel(
            "Vienen de la plantilla de ensayo y se pueden cambiar para este ensayo. Al crearlo, cada fichero se copia "
            "en la carpeta del ensayo («especificaciones/»)."
        )
        note.setObjectName("pageSubtitle")
        note.setWordWrap(True)
        self.specs = SpecEditor(resolver=self._resolve, importer=lambda path: str(path))
        specs_layout.addWidget(note)
        specs_layout.addWidget(self.specs, 1)
        check_page = QWidget()
        check_layout = QVBoxLayout(check_page)
        check_layout.setContentsMargins(8, 8, 8, 8)
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        self.summary.setTextFormat(Qt.TextFormat.RichText)
        self.issues = QListWidget()
        self.issues.setWordWrap(True)
        check_layout.addWidget(self.summary)
        check_layout.addWidget(self.issues, 1)
        self.tabs.addTab(check_page, icon("ok", style.PRIMARY.name()), "Comprobación previa")
        self.tabs.addTab(specs_page, icon("veredicto", style.PRIMARY.name()), "Especificaciones (pasa / falla)")
        right.addWidget(setup_card)
        right.addWidget(self.tabs)
        right.setSizes([330, 400])
        splitter.addWidget(left)
        splitter.addWidget(right)
        splitter.setSizes([500, 820])
        layout.addWidget(splitter, 1)

        bottom = QHBoxLayout()
        self.status = QLabel()
        self.status.setWordWrap(True)
        cancel = QPushButton("Cancelar")
        cancel.clicked.connect(self.reject)
        self.accept_button = QPushButton(icon("ejecutar", "#ffffff"), "Guardar definición" if editing else "Crear ensayo")
        self.accept_button.setObjectName("primary")
        self.accept_button.clicked.connect(self._accept)
        bottom.addWidget(self.status, 1)
        bottom.addWidget(cancel)
        bottom.addWidget(self.accept_button)
        layout.addLayout(bottom)

        if editing:
            assert record is not None and record.folder is not None
            self.name.setText(record.title)
            self.folders.select_folder(record.folder.parent)
            self.folders.setEnabled(False)
            self.new_folder_button.setEnabled(False)
            self.external_button.setEnabled(False)
        elif folder is not None:
            self.folders.select_folder(Path(folder))
        self.procedure.currentIndexChanged.connect(lambda _i: self._procedure_changed())
        self.setup.currentIndexChanged.connect(lambda _i: self._setup_changed())
        self.dut.currentIndexChanged.connect(lambda _i: self._dut_changed())
        self.specs.changed.connect(self._check)
        self.name.textEdited.connect(self._name_typed)
        self.folders.folder_changed.connect(lambda _f: self._update_status())
        wanted = procedure_id or (self._snapshot.id if self._snapshot is not None else None)
        if wanted is None and setup_id in library.setups:
            wanted = next(
                (p.id for p in sorted(library.procedures.values(), key=lambda p: p.name) if compatible(library, p, library.setups[setup_id])),
                None,
            )
        if wanted is not None:
            self.procedure.setCurrentIndex(max(self.procedure.findData(wanted), 0))
        self._procedure_changed()

    # ── current selection ──────────────────────────────────────────────────
    def _resolve(self, reference: str) -> Path:
        return self.library.procedure_file(reference, self.record.folder if self.record is not None else None)

    def current_procedure(self) -> ProcedureTemplate | None:
        procedure_id = self.procedure.currentData()
        if self._snapshot is not None and procedure_id == self._snapshot.id:
            return self._snapshot
        return self.library.procedures.get(procedure_id)

    def current_setup(self) -> ConfiguredSetup | None:
        return self.library.setups.get(self.setup.currentData())

    def current_dut(self) -> str | None:
        return self.dut.currentData()

    def current_folder(self) -> Path | None:
        return self.folders.current_folder()

    # ── reactions ──────────────────────────────────────────────────────────
    def _name_typed(self, _text: str) -> None:
        self._name_edited = True
        self._update_status()

    def _procedure_changed(self) -> None:
        procedure = self.current_procedure()
        self.setup.blockSignals(True)
        current = self.setup.currentData() or self._preferred_setup
        self.setup.clear()
        if procedure is not None:
            self.specs.set_traces(procedure.traces)
            if self._specs_from != procedure.id:
                self.specs.load(procedure.specifications)
                self._specs_from = procedure.id
            for setup in compatible_setups(self.library, procedure):
                self.setup.addItem(icon("setups", style.PRIMARY.name()), f"{setup.name}  ·  {setup.required_range}", setup.id)
        self.setup.setCurrentIndex(max(self.setup.findData(current), 0))
        self.setup.blockSignals(False)
        self._setup_changed()

    def _setup_changed(self) -> None:
        setup = self.current_setup()
        role_key, label, candidates = dut_choices(self.library, setup)
        self.dut_label.setText(label)
        previous = self.current_dut() or self._preferred_dut
        if previous is None and setup is not None and role_key:
            assigned = setup.ids_for(role_key)
            previous = assigned[0] if assigned else None
        self.dut.blockSignals(True)
        self.dut.clear()
        self.dut.addItem("— Elija el equipo a calibrar —", None)
        for equipment in candidates:
            self.dut.addItem(icon("cable", style.DUT.name()), equipment_text(self.library, equipment), equipment.id)
        self.dut.setCurrentIndex(max(self.dut.findData(previous), 0) if previous else 0)
        self.dut.blockSignals(False)
        self._dut_changed()

    def _dut_changed(self) -> None:
        if not self._name_edited:
            procedure = self.current_procedure()
            base = self.current_dut() or (procedure.id if procedure else "ensayo")
            self.name.setText(f"{base} {date.today():%Y-%m-%d}")
        self._update_diagram()
        self._check()

    def _update_diagram(self) -> None:
        setup = self.current_setup()
        template = self.library.resolved_template(setup.template) if setup is not None else None
        if setup is None or template is None:
            self.diagram.scene().clear()
            self.setup_info.setText("No hay ningún hardware setup compatible con la plantilla de ensayo." if setup is None else "")
            return
        effective = with_dut(setup, dut_role_of(template), self.current_dut())
        analysis = analyze_setup(effective, self.library)
        self.diagram.show_setup(template, effective, self.library, analysis, setup_state(template))
        QTimer.singleShot(0, self.diagram.fit)
        self.setup_info.setText(f"{setup.name} · plantilla «{template.name}» · rango requerido {setup.required_range}")

    def definition(self) -> TestDefinition:
        """Definition of the test with the specifications of the editor; ``ValueError`` if something is wrong."""
        procedure = self.current_procedure()
        setup = self.current_setup()
        if procedure is None:
            raise ValueError("Elija la plantilla de ensayo")
        if setup is None:
            raise ValueError("No hay ningún hardware setup compatible con la plantilla de ensayo")
        data = procedure.to_yaml_dict()
        data["especificaciones"] = self.specs.values()
        template = ProcedureTemplate.model_validate(data)
        return TestDefinition.from_template(template, setup.id, self.current_dut())

    def _check(self) -> None:
        self.issues.clear()
        try:
            definition = self.definition()
        except ValueError as exc:
            self.plan = None
            self.summary.setText(html.escape(str(exc)))
            self._update_status(str(exc))
            return
        setup = self.current_setup()
        assert setup is not None
        self.plan = prepare_run(
            self.library, definition.template(), setup, dut_id=definition.dut_id, test_folder=self.record.folder if self.record else None
        )
        self.summary.setText(plan_html(self.library, self.plan))
        for issue in sorted(self.plan.issues, key=lambda i: -int(i.severity)):
            self.issues.addItem(QListWidgetItem(style.severity_icon(self, issue.severity), issue.message))
        if not self.plan.issues:
            self.issues.addItem(QListWidgetItem(style.severity_icon(self, None), "Todo listo: plantilla, setup y equipo a calibrar compatibles"))
        errors = sum(1 for i in self.plan.issues if i.severity is Severity.ERROR)
        warnings = sum(1 for i in self.plan.issues if i.severity is Severity.WARNING)
        text = "Comprobación previa" + (f" ({errors} errores)" if errors else (f" ({warnings} avisos)" if warnings else ""))
        self.tabs.setTabText(0, text)
        self.tabs.setTabIcon(0, style.severity_icon(self, Severity.ERROR if errors else (Severity.WARNING if warnings else None)))
        simulated = self.plan.vna is not None and is_simulated(self.plan.vna, self.library)
        self.automatic.setEnabled(simulated)
        if not simulated:
            self.automatic.setChecked(False)
        self._update_status()

    def _update_status(self, problem: str | None = None) -> None:
        folder = self.current_folder()
        if problem is None:
            if folder is None:
                problem = "Elija la carpeta del ensayo"
            elif not self.name.text().strip():
                problem = "Indique el nombre del ensayo"
            elif self.plan is None:
                problem = "Complete la definición del ensayo"
            elif not self.plan.ok:
                errors = [i.message for i in self.plan.issues if i.severity is Severity.ERROR]
                problem = f"{len(errors)} error(es): {errors[0]}"
        if problem:
            self.status.setText(f"<span style='color:{style.ERROR.name()}'>✖ {html.escape(problem)}</span>")
            self.accept_button.setEnabled(False)
            return
        assert self.plan is not None and folder is not None
        warnings = sum(1 for i in self.plan.issues if i.severity is Severity.WARNING)
        where = "Se guardará en" if self.record is not None else "Se creará en"
        extra = f" ({warnings} avisos)" if warnings else ""
        self.status.setText(f"<span style='color:{style.OK.name()}'>✔ Listo{extra}.</span> {where} {html.escape(str(folder))}")
        self.accept_button.setEnabled(True)

    def _accept(self) -> None:
        if self.plan is None or not self.plan.ok:
            return
        try:
            definition = self.definition()
        except ValueError as exc:
            QMessageBox.warning(self, self.windowTitle(), str(exc))
            return
        operator = self.operator.text().strip()
        QSettings(*SETTINGS).setValue("operador", operator)
        name = self.name.text().strip()
        try:
            if self.record is not None:
                self.record.name = name
                self.record.operator = operator or None
                self.created = self.library.redefine_test(self.record, definition)
            else:
                folder = self.current_folder()
                assert folder is not None
                self.created = self.library.create_test(folder, name, definition, operator or None)
        except (LibraryError, OSError, ValueError) as exc:
            QMessageBox.warning(self, self.windowTitle(), str(exc))
            return
        self.accept()


# ── operator dialogs ───────────────────────────────────────────────────────
class PromptDialog(QDialog):
    """Request to the operator: connect a standard, the DUT… or confirm."""

    def __init__(self, prompt: PromptRequest, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle(prompt.title)
        self.setModal(True)
        self.setMinimumWidth(560)
        layout = QVBoxLayout(self)
        title = QLabel(prompt.title)
        title.setObjectName("pageTitle")
        title.setWordWrap(True)
        layout.addWidget(title)
        if prompt.image:
            picture = QLabel()
            picture.setPixmap(illustration(prompt.image))
            picture.setAlignment(Qt.AlignmentFlag.AlignCenter)
            picture.setStyleSheet("background: #ffffff; border: 1px solid #dde3ea; border-radius: 8px; padding: 8px;")
            layout.addWidget(picture)
        message = QLabel(prompt.message)
        message.setWordWrap(True)
        message.setStyleSheet("font-size: 13px;")
        layout.addWidget(message)
        for detail in prompt.details:
            label = QLabel(f"• {detail}")
            label.setWordWrap(True)
            layout.addWidget(label)
        buttons = QHBoxLayout()
        stop = QPushButton(icon("detener", style.ERROR.name()), "Detener ensayo" if prompt.kind == "conectar" else "No")
        stop.setObjectName("danger")
        stop.clicked.connect(self.reject)
        go = QPushButton(icon("ok", "#ffffff"), "Hecho, continuar" if prompt.kind == "conectar" else "Sí, continuar")
        go.setObjectName("primary")
        go.setDefault(True)
        go.clicked.connect(self.accept)
        buttons.addWidget(stop)
        buttons.addStretch(1)
        buttons.addWidget(go)
        layout.addLayout(buttons)


class PromptBridge(QObject):
    """Lives in the GUI thread; the worker emits ``requested`` and blocks until answered."""

    requested = Signal(object)

    def __init__(self, parent_widget: QWidget):
        super().__init__()
        self.parent_widget = parent_widget
        self.answer = False
        self.requested.connect(self._show, Qt.ConnectionType.BlockingQueuedConnection)

    @Slot(object)
    def _show(self, prompt: PromptRequest) -> None:
        self.answer = PromptDialog(prompt, self.parent_widget).exec() == QDialog.DialogCode.Accepted


class GuiOperator:
    def __init__(self, bridge: PromptBridge):
        self.bridge = bridge

    def request(self, prompt: PromptRequest) -> bool:
        self.bridge.requested.emit(prompt)  # blocks the worker thread until the dialog closes
        return self.bridge.answer


class RunWorker(QObject):
    step = Signal(str, str, str)
    log = Signal(str, str)
    data = Signal(str, object, object)
    progress = Signal(float, str)
    finished = Signal(object)

    def __init__(self, runner: ProcedureRunner | None):
        super().__init__()
        self.runner = runner

    @Slot()
    def run(self) -> None:
        assert self.runner is not None
        result = self.runner.run()
        self.finished.emit(result)


class _Listener(RunListener):
    def __init__(self, worker: RunWorker):
        self.worker = worker

    def step_changed(self, key: str, status: str, detail: str = "") -> None:
        self.worker.step.emit(key, status, detail)

    def log(self, level: str, message: str) -> None:
        self.worker.log.emit(level, message)

    def data(self, name: str, frequencies, s) -> None:
        self.worker.data.emit(name, frequencies, s)

    def progress(self, fraction: float, text: str = "") -> None:
        self.worker.progress.emit(fraction, text)


LOG_COLORS = {"error": "#c0392b", "aviso": "#d98a00", "operador": "#2b7bb9"}
SPEC_COLUMNS = ["Especificación", "Medida", "Parámetro", "Criterio", "Resultado", "Margen mínimo", "A la frecuencia", "Puntos fuera de especificación"]


# ── test window ────────────────────────────────────────────────────────────
class TestWindow(QWidget):
    """One test: its definition, the run (steps, live chart, operator dialogs), the verdict and the results."""

    __test__ = False  # not a pytest test class

    finished = Signal(str)  # folder of the test
    redefined = Signal(str)  # folder of the test

    def __init__(self, library: Library, record: TestRecord, automatic: bool = False, parent: QWidget | None = None):
        super().__init__(parent)
        self.library = library
        self.record = record
        self.automatic = automatic
        self.result: RunResult | None = None
        self.thread: QThread | None = None
        self.worker: RunWorker | None = None
        self.runner: ProcedureRunner | None = None
        self.running = False
        self.procedure: ProcedureTemplate | None = None
        self.setup: ConfiguredSetup | None = None
        self.plan: RunPlan | None = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 8)

        header = QHBoxLayout()
        texts = QVBoxLayout()
        self.title_label = QLabel()
        self.title_label.setObjectName("pageTitle")
        self.subtitle = QLabel()
        self.subtitle.setObjectName("pageSubtitle")
        self.subtitle.setWordWrap(True)
        texts.addWidget(self.title_label)
        texts.addWidget(self.subtitle)
        header.addLayout(texts, 1)
        self.chips = QHBoxLayout()
        self.chips.setAlignment(Qt.AlignmentFlag.AlignVCenter)
        header.addLayout(self.chips)
        self.badge = style.StatusBadge()
        header.addWidget(self.badge, 0, Qt.AlignmentFlag.AlignVCenter)
        layout.addLayout(header)

        controls = QHBoxLayout()
        self.start_button = QPushButton(icon("ejecutar", "#ffffff"), "Iniciar ensayo")
        self.start_button.setObjectName("primary")
        self.start_button.clicked.connect(self.start)
        self.stop_button = QPushButton(icon("detener", style.ERROR.name()), "Detener")
        self.stop_button.setObjectName("danger")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self.stop)
        self.edit_button = QPushButton(icon("editar", style.PRIMARY.name()), "Definición…")
        self.edit_button.setToolTip("Cambiar el setup, el equipo a calibrar o las especificaciones antes de ejecutar")
        self.edit_button.clicked.connect(self.edit_definition)
        self.folder_button = QPushButton(icon("carpeta", style.PRIMARY.name()), "Abrir carpeta")
        self.folder_button.clicked.connect(self.open_folder)
        self.action_start = QAction(icon("ejecutar", style.OK.name()), "Iniciar", self)
        self.action_start.triggered.connect(self.start)
        self.action_stop = QAction(icon("detener", style.ERROR.name()), "Detener", self)
        self.action_stop.triggered.connect(self.stop)
        self.action_edit = QAction(icon("editar", style.PRIMARY.name()), "Definición", self)
        self.action_edit.triggered.connect(self.edit_definition)
        self.action_folder = QAction(icon("carpeta", style.PRIMARY.name()), "Abrir\ncarpeta", self)
        self.action_folder.triggered.connect(self.open_folder)
        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.setTextVisible(False)
        self.progress_label = QLabel()
        self.progress_label.setObjectName("pageSubtitle")
        controls.addWidget(self.start_button)
        controls.addWidget(self.stop_button)
        controls.addWidget(self.edit_button)
        controls.addSpacing(10)
        controls.addWidget(self.progress, 1)
        controls.addWidget(self.progress_label)
        controls.addSpacing(10)
        controls.addWidget(self.folder_button)
        layout.addLayout(controls)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        steps_card = style.Card("Flujo del ensayo", icon_name="procedimientos")
        self.steps = QListWidget()
        self.steps.setMinimumWidth(330)
        self.steps.setWordWrap(True)
        self.step_items: dict[str, QListWidgetItem] = {}
        for key, name in STEPS.items():
            item = QListWidgetItem(style.status_icon("pendiente"), name)
            item.setData(Qt.ItemDataRole.UserRole, key)
            self.steps.addItem(item)
            self.step_items[key] = item
        steps_card.body.addWidget(self.steps)
        plan_card = style.Card("Definición del ensayo", icon_name="info")
        self.plan_label = QLabel()
        self.plan_label.setWordWrap(True)
        self.plan_label.setTextFormat(Qt.TextFormat.RichText)
        self.plan_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        plan_card.body.addWidget(self.plan_label)
        plan_card.body.addStretch(1)
        left_layout.addWidget(steps_card, 3)
        left_layout.addWidget(plan_card, 3)

        self.tabs = QTabWidget()
        self.plot = SParameterPlot()
        self.spec_table = style.make_table(SPEC_COLUMNS)
        self.diagram = SetupDiagramView()
        self.results = style.make_table(["Resultado", "Valor"], stretch_column=1)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.tabs.addTab(self.plot, icon("grafica", style.PRIMARY.name()), "Gráfica")
        self.tabs.addTab(self.spec_table, icon("veredicto", style.PRIMARY.name()), "Pasa / falla")
        self.tabs.addTab(self.diagram, icon("setups", style.PRIMARY.name()), "Hardware setup")
        self.tabs.addTab(self.results, icon("ok", style.PRIMARY.name()), "Resultados")
        self.tabs.addTab(self.log_view, icon("procedimientos", style.PRIMARY.name()), "Registro")
        splitter.addWidget(left)
        splitter.addWidget(self.tabs)
        splitter.setStretchFactor(1, 3)
        layout.addWidget(splitter, 1)
        self.load_record()

    # ── state ──────────────────────────────────────────────────────────────
    @property
    def title(self) -> str:
        return f"Ensayo: {self.record.title}"

    @property
    def folder(self) -> str:
        return str(self.record.folder)

    def ribbon_actions(self) -> list[QAction | None]:
        return [self.action_start, self.action_stop, None, self.action_edit, self.action_folder]

    def load_record(self) -> None:
        """Show the test: definition and check if prepared, or its results if it has been run."""
        record = self.record
        definition = record.definition
        self.procedure = self.setup = self.plan = None
        problem = None
        if definition is None:
            problem = "El ensayo no tiene definición"
        else:
            try:
                self.procedure = definition.template()
            except ValueError as exc:
                problem = f"La definición del ensayo no es válida: {exc}"
            self.setup = self.library.setups.get(definition.setup_id)
            if self.setup is None:
                problem = f"El hardware setup «{definition.setup_id}» ya no existe en la biblioteca"
        if self.procedure is not None and self.setup is not None:
            self.plan = prepare_run(self.library, self.procedure, self.setup, dut_id=definition.dut_id, test_folder=record.folder)
        self.title_label.setText(record.title)
        dut = record.dut_id or "—"
        template_name = record.procedure.get("nombre", self.procedure.name if self.procedure else "—")
        setup_name = record.setup.get("nombre", self.setup.name if self.setup else "—")
        self.subtitle.setText(f"{template_name} · setup {setup_name} · equipo a calibrar {dut} · {record.folder}")
        self._set_chips()
        self.plan_label.setText(plan_html(self.library, self.plan) if self.plan is not None else html.escape(problem or ""))
        self._show_diagram()
        self.log_view.clear()
        self.results.setRowCount(0)
        self.spec_table.setRowCount(0)
        self.plot.clear()
        for key, item in self.step_items.items():
            item.setIcon(style.status_icon("pendiente"))
            item.setText(STEPS[key])
        prepared = record.status == "preparado"
        if prepared:
            self.progress.setValue(0)
            self.progress_label.setText("")
            for issue in self.plan.issues if self.plan else []:
                self._append_log("error" if issue.severity is Severity.ERROR else "aviso", issue.message)
            if problem:
                self._append_log("error", problem)
            runnable = self.plan is not None and self.plan.ok
            self.badge.set_state("Preparado" if runnable else "No ejecutable", style.status_color("preparado") if runnable else style.ERROR)
            self.start_button.setText("Iniciar ensayo")
            self.start_button.setEnabled(runnable)
        else:
            for step in record.steps:
                self._step(step.key, step.status, step.detail)
            self.progress.setValue(1000 if record.status in ("completado", "verificacion_fallida") else self.progress.value())
            self.progress_label.setText(record.status_name)
            self.badge.set_state(record.status_name, style.status_color(record.status))
            self.start_button.setText("Repetir ensayo")
            self.start_button.setToolTip("Crea un ensayo nuevo con la misma definición, en la misma carpeta")
            self.start_button.setEnabled(definition is not None and self.setup is not None)
            for label, frequencies, s in record_datasets(record):
                self.plot.set_data(label, frequencies, s, log_x=is_logarithmic(record))
            self.plot.set_limits(record_spec_curves(self.library, record))
            self._fill_results(record)
            self._fill_specs(record)
            log = record.folder / "registro.txt" if record.folder else None
            if log is not None and log.is_file():
                self.log_view.setPlainText(log.read_text(encoding="utf-8", errors="replace"))
        self.stop_button.setEnabled(False)
        self.edit_button.setEnabled(prepared and not self.running)
        self.folder_button.setEnabled(record.folder is not None)
        self._sync_actions()

    def _set_chips(self) -> None:
        while self.chips.count():
            widget = self.chips.takeAt(0).widget()
            if widget:
                widget.deleteLater()
        record = self.record
        if record.simulated or (self.plan is not None and self.plan.vna is not None and is_simulated(self.plan.vna, self.library)):
            self.chips.addWidget(style.chip("SIMULACIÓN" + (" AUTOMÁTICA" if self.automatic else ""), style.SIMULATION), 0, Qt.AlignmentFlag.AlignVCenter)
        chip = verdict_chip(record.verdict)
        if chip is not None:
            self.chips.addWidget(chip, 0, Qt.AlignmentFlag.AlignVCenter)

    def _show_diagram(self) -> None:
        if self.setup is None:
            self.diagram.scene().clear()
            return
        template = self.library.resolved_template(self.setup.template)
        if template is None:
            return
        effective = self.plan.setup if self.plan is not None else self.setup
        self.diagram.show_setup(template, effective, self.library, self.plan.analysis if self.plan else None, setup_state(template))
        QTimer.singleShot(0, self.diagram.fit)

    def _sync_actions(self) -> None:
        self.action_start.setText("Repetir" if self.record.status != "preparado" else "Iniciar")
        self.action_start.setEnabled(self.start_button.isEnabled())
        self.action_stop.setEnabled(self.stop_button.isEnabled())
        self.action_edit.setEnabled(self.edit_button.isEnabled())
        self.action_folder.setEnabled(self.folder_button.isEnabled())

    # ── actions ────────────────────────────────────────────────────────────
    def edit_definition(self) -> None:
        if self.running or self.record.status != "preparado":
            return
        dialog = NewTestDialog(self.library, record=self.record, parent=self)
        if dialog.exec() and dialog.created is not None:
            self.record = dialog.created
            self.load_record()
            self.redefined.emit(self.folder)

    def start(self) -> None:
        if self.running:
            return
        operator_name = self.record.operator or str(QSettings(*SETTINGS).value("operador", "")) or None
        if self.record.status != "preparado":
            # repetition: a new prepared test with the same definition, in the same folder
            assert self.record.folder is not None and self.record.definition is not None
            try:
                self.record = self.library.create_test(
                    self.record.folder.parent, f"{self.record.title} (repetición)", self.record.definition, operator_name, self.record.folder
                )
            except (LibraryError, OSError, ValueError) as exc:
                QMessageBox.warning(self, "Repetir ensayo", str(exc))
                return
            self.load_record()
            if self.plan is None or not self.plan.ok:
                return
        self.result = None
        self.plot.clear()
        self.results.setRowCount(0)
        self.spec_table.setRowCount(0)
        self.log_view.clear()
        for key, item in self.step_items.items():
            item.setIcon(style.status_icon("pendiente"))
            item.setText(STEPS[key])
        self.bridge = PromptBridge(self)
        operator = AutoOperator() if self.automatic else GuiOperator(self.bridge)
        self.worker = RunWorker(None)
        listener = _Listener(self.worker)
        try:
            self.runner = ProcedureRunner.for_test(self.library, self.record, operator, listener, operator_name)
        except LibraryError as exc:
            QMessageBox.warning(self, "Iniciar ensayo", str(exc))
            return
        self.worker.runner = self.runner
        self.thread = QThread(self)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.step.connect(self._step)
        self.worker.log.connect(self._append_log)
        self.worker.data.connect(self._data)
        self.worker.progress.connect(self._progress)
        self.worker.finished.connect(self._finished)
        self.worker.finished.connect(self.thread.quit)
        self.running = True
        self.start_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self.edit_button.setEnabled(False)
        self.badge.set_state("En curso", style.ACCENT)
        self.progress.setValue(0)
        self._sync_actions()
        self.thread.start()

    def stop(self) -> None:
        if self.runner is not None and self.running:
            self.runner.stop()
            self.badge.set_state("Deteniendo…", style.WARNING)

    def wait(self, timeout_ms: int = 30_000) -> None:
        if self.thread is not None:
            self.thread.wait(timeout_ms)

    def open_folder(self) -> None:
        if self.record.folder is not None:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.record.folder)))

    # ── worker signals ─────────────────────────────────────────────────────
    def _step(self, key: str, status: str, detail: str) -> None:
        item = self.step_items.get(key)
        if item is None:
            return
        item.setIcon(style.status_icon(status))
        text = STEPS[key] + (f"\n{detail}" if detail and status != "en_curso" else "")
        item.setText(text)
        item.setToolTip(detail)
        index = list(STEPS).index(key)
        done = index + (0.5 if status == "en_curso" else 1)
        self.progress.setValue(int(1000 * done / len(STEPS)))
        if status == "en_curso":
            self.progress_label.setText(STEPS[key])
            self.steps.setCurrentItem(item)

    def _append_log(self, level: str, message: str) -> None:
        color = LOG_COLORS.get(level, "#22303f")
        self.log_view.appendHtml(f"<span style='color:#8a94a3'>{datetime.now():%H:%M:%S}</span> <span style='color:{color}'>{html.escape(message)}</span>")

    def _data(self, name: str, frequencies, s) -> None:
        log_x = self.procedure is not None and self.procedure.sweep.mode == "logaritmico"
        self.plot.set_data(name, frequencies, s, log_x=log_x)
        if not name.startswith("Verificación"):
            self.tabs.setCurrentWidget(self.plot)

    def _progress(self, fraction: float, text: str) -> None:
        self.progress_label.setText(text)

    def _finished(self, result: RunResult) -> None:
        self.running = False
        self.result = result
        record = self.record = result.record
        self.start_button.setText("Repetir ensayo")
        self.start_button.setToolTip("Crea un ensayo nuevo con la misma definición, en la misma carpeta")
        self.start_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        self.edit_button.setEnabled(False)
        self.folder_button.setEnabled(record.folder is not None)
        self.progress.setValue(1000 if record.status in ("completado", "verificacion_fallida") else self.progress.value())
        self.progress_label.setText(record.status_name)
        self.badge.set_state(record.status_name, style.status_color(record.status))
        self._set_chips()
        if result.data and result.frequencies is not None:
            self.plot.clear()
            log_x = self.procedure is not None and self.procedure.sweep.mode == "logaritmico"
            for name, s in result.data.items():
                self.plot.set_data(name, result.frequencies, s, log_x=log_x)
            self.plot.set_limits(record_spec_curves(self.library, record))
        self._fill_results(record)
        self._fill_specs(record)
        if record.spec_results:
            self.tabs.setCurrentWidget(self.spec_table if record.verdict == "FALLA" else self.plot)
        self._sync_actions()
        self.finished.emit(self.folder)

    def _fill_specs(self, record: TestRecord) -> None:
        """One row per specification and parameter: result, worst margin and where."""
        self.spec_table.setRowCount(0)
        several = any(entry.get("medida") for entry in record.spec_results)
        self.spec_table.setColumnHidden(1, not several)
        for entry in record.spec_results:
            unit = entry.get("unidad", "dB")
            parameters = entry.get("parametros") or [{"parametro": "—", "resultado": entry.get("resultado"), "margen_minimo": entry.get("margen_minimo")}]
            for parameter in parameters:
                result = parameter.get("resultado") or ""
                margin = parameter.get("margen_minimo")
                evaluated, failed = parameter.get("evaluados"), parameter.get("fallos")
                outside = parameter.get("fuera_de_rango")
                points = "—" if evaluated is None else f"{failed} de {evaluated}" + (f" ({outside} sin especificación)" if outside else "")
                values = [
                    str(entry.get("nombre", "")),
                    str(entry.get("medida", "")),
                    str(parameter.get("parametro", "")),
                    str(entry.get("criterio", "")),
                    VERDICTS.get(result, result or "—"),
                    "—" if margin is None else f"{margin:+.3f} {unit}",
                    str(parameter.get("frecuencia") or "—"),
                    points,
                ]
                row = self.spec_table.rowCount()
                self.spec_table.insertRow(row)
                for column, value in enumerate(values):
                    item = QTableWidgetItem(value)
                    if column in (4, 5):
                        color = style.VERDICT_COLORS.get(result, style.NEUTRAL)
                        item.setForeground(color)
                    if column == 4:
                        name = {"PASA": "ok", "FALLA": "error"}.get(result, "aviso")
                        item.setIcon(icon(name, style.VERDICT_COLORS.get(result, style.WARNING).name()))
                    item.setToolTip("\n".join(entry.get("detalle", [])))
                    self.spec_table.setItem(row, column, item)
        if not record.spec_results:
            self.spec_table.insertRow(0)
            text = "La plantilla no define especificaciones: sin veredicto" if record.verdict == "SIN_ESPECIFICACIONES" else "Sin evaluación"
            self.spec_table.setItem(0, 0, QTableWidgetItem(text))
        self.spec_table.resizeColumnsToContents()
        verdict = record.verdict
        text = "Pasa / falla" + (f" — {VERDICTS[verdict].upper()}" if verdict and verdict != "SIN_ESPECIFICACIONES" else "")
        self.tabs.setTabText(1, text)

    def _fill_results(self, record: TestRecord) -> None:
        rows: list[tuple[str, str]] = [("Ensayo", f"{record.title} [{record.id}]"), ("Estado", record.status_name)]
        if record.verdict:
            rows.append(("Veredicto", VERDICTS.get(record.verdict, record.verdict)))
        if record.message:
            rows.append(("Mensaje", record.message))
        sweep = record.sweep
        if sweep:
            rows.append(("Barrido", f"{sweep.get('modo', '')} {sweep.get('inicio')} – {sweep.get('fin')}, {sweep.get('puntos')} puntos, IFBW {sweep.get('ifbw')}"))
        calibration = record.calibration
        if calibration:
            rows.append(("Calibración", f"{calibration.get('metodo', '—')}" + (f" · kit {calibration['kit']}" if calibration.get("kit") else "")))
        for check in record.verification.get("comprobaciones", []):
            state = "correcto" if check["correcto"] else "FUERA DE TOLERANCIA"
            rows.append((f"Verificación: {check['comprobacion']}", f"{check['valor_db']:g} dB (límite {check['limite_db']:g} dB) — {state}"))

        def flatten(prefix: str, value) -> None:
            if isinstance(value, dict):
                for key, item in value.items():
                    flatten(f"{prefix} · {key}" if prefix else str(key), item)
            else:
                rows.append((prefix.replace("_", " "), str(value)))

        flatten("", record.results)
        if record.files:
            rows.append(("Ficheros", ", ".join(record.files)))
        if record.folder is not None:
            rows.append(("Carpeta", str(record.folder)))
        self.results.setRowCount(0)
        for name, value in rows:
            row = self.results.rowCount()
            self.results.insertRow(row)
            self.results.setItem(row, 0, QTableWidgetItem(name))
            self.results.setItem(row, 1, QTableWidgetItem(value))
        self.results.resizeColumnToContents(0)

    def can_close(self) -> bool:
        if not self.running:
            return True
        answer = QMessageBox.question(self, "Ensayo en curso", "El ensayo está en curso. ¿Detenerlo y cerrar?")
        if answer != QMessageBox.StandardButton.Yes:
            return False
        self.stop()
        self.wait(10_000)
        return True
