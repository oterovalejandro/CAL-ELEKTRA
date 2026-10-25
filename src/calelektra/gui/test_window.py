"""Tests (ELEKTRA-like): launch dialog, test window and operator dialogs.

A test is the record of a measurement launched from a test template: the
launch dialog chooses the folder (test explorer) and the template, shows what
the template will do (its hardware setup, frequencies, EUT, specifications)
and checks it; the test window runs it and then shows the record (steps,
chart with the specifications, pass / fail, setup, results and log). Opening
an existing test shows its record; «Repetir» launches the same template again
in the same folder.

The run is done in a worker thread. Operator requests are shown in the GUI
thread through a blocking queued signal, so the run waits for the answer just
like the real measurement waits for the operator.
"""

from __future__ import annotations

import html
from datetime import date, datetime
from pathlib import Path

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
from calelektra.core.issues import Severity
from calelektra.gui import style
from calelektra.gui.icons import icon
from calelektra.gui.illustrations import illustration
from calelektra.gui.plots import SParameterPlot
from calelektra.gui.setup_diagram import SetupDiagramView
from calelektra.gui.test_common import (
    SETTINGS,
    is_logarithmic,
    load_conditions,
    pending_chip,
    plan_html,
    record_coverage_factor,
    record_datasets,
    uncertainty_rows,
    record_uncertainty,
    record_smoothing,
    record_spec_curves,
    save_package,
    setup_rows,
    setup_state,
    verdict_chip,
)
from calelektra.gui.test_folders import TestFolderTree
from calelektra.instruments.factory import is_simulated
from calelektra.procedures.engine import STEPS, AutoOperator, ProcedureRunner, PromptRequest, RunListener, RunPlan, RunResult, prepare_run
from calelektra.procedures.model import ProcedureTemplate
from calelektra.procedures.record import TestRecord
from calelektra.procedures.specs import VERDICTS
from calelektra.setups.connections import method_name


def default_test_name(procedure: ProcedureTemplate) -> str:
    return f"{procedure.eut_id or procedure.id} {date.today():%Y-%m-%d}"


# ── launch ─────────────────────────────────────────────────────────────────
class LaunchTestDialog(QDialog):
    """Launch a test template in a folder: the run creates the test (the record of the measurement)."""

    def __init__(self, library: Library, procedure_id: str | None = None, folder: Path | None = None, parent: QWidget | None = None):
        super().__init__(parent)
        self.library = library
        self.plan: RunPlan | None = None
        self._name_edited = False
        self.setWindowTitle("Lanzar ensayo")
        self.resize(1280, 800)
        layout = QVBoxLayout(self)
        layout.addWidget(
            style.PageHeader(
                "Lanzar ensayo",
                "Elija la carpeta y la plantilla de ensayo. La plantilla define el hardware setup (y con él las frecuencias), "
                "el EUT y, por su modelo, las especificaciones. Cada lanzamiento crea un ensayo: el registro de la medida.",
            )
        )
        splitter = QSplitter(Qt.Orientation.Horizontal)

        left = style.Card("Dónde y qué", icon_name="ensayos")
        folder_title = QLabel("Carpeta del ensayo")
        folder_title.setObjectName("cardTitle")
        self.folders = TestFolderTree(library)
        self.folders.setMinimumHeight(160)
        folder_buttons = QHBoxLayout()
        new_folder = QPushButton(icon("carpeta_nueva", style.PRIMARY.name()), "Nueva carpeta…")
        external = QPushButton(icon("carpeta_externa", style.PRIMARY.name()), "Carpeta externa…")
        new_folder.clicked.connect(self.folders.create_folder)
        external.clicked.connect(self.folders.add_external)
        folder_buttons.addWidget(new_folder)
        folder_buttons.addWidget(external)
        folder_buttons.addStretch(1)
        left.body.addWidget(folder_title)
        left.body.addWidget(self.folders, 1)
        left.body.addLayout(folder_buttons)
        left.body.addWidget(style.separator())
        form = QFormLayout()
        self.procedure = QComboBox()
        for procedure in sorted(library.procedures.values(), key=lambda p: p.name):
            self.procedure.addItem(icon("procedimientos", style.PRIMARY.name()), procedure.name, procedure.id)
        self.procedure.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.procedure.setMinimumContentsLength(24)
        self.name = QLineEdit()
        self.name.setPlaceholderText("Nombre del ensayo")
        self.operator = QLineEdit()
        self.operator.setPlaceholderText("Nombre del operador")
        self.operator.setText(str(QSettings(*SETTINGS).value("operador", "")))
        self.automatic = QCheckBox("Simulación automática (sin diálogos al operador)")
        self.automatic.setToolTip("Solo con el VNA simulado: el banco simulado conecta los estándares y el EUT sin preguntar.")
        form.addRow("Plantilla de ensayo", self.procedure)
        form.addRow("Nombre del ensayo", self.name)
        form.addRow("Operador", self.operator)
        form.addRow(self.automatic)
        left.body.addLayout(form)

        right = QSplitter(Qt.Orientation.Vertical)
        setup_card = style.Card("Hardware setup de la plantilla", icon_name="setups")
        self.diagram = SetupDiagramView()
        self.diagram.setMinimumHeight(220)
        setup_card.body.addWidget(self.diagram, 1)
        check_card = style.Card("Comprobación previa", icon_name="ok")
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        self.summary.setTextFormat(Qt.TextFormat.RichText)
        self.issues = QListWidget()
        self.issues.setWordWrap(True)
        check_card.body.addWidget(self.summary)
        check_card.body.addWidget(self.issues, 1)
        right.addWidget(setup_card)
        right.addWidget(check_card)
        right.setSizes([300, 420])
        splitter.addWidget(left)
        splitter.addWidget(right)
        splitter.setSizes([460, 820])
        layout.addWidget(splitter, 1)

        bottom = QHBoxLayout()
        self.status = QLabel()
        self.status.setWordWrap(True)
        cancel = QPushButton("Cancelar")
        cancel.clicked.connect(self.reject)
        self.accept_button = QPushButton(icon("ejecutar", "#ffffff"), "Lanzar ensayo")
        self.accept_button.setObjectName("primary")
        self.accept_button.clicked.connect(self._accept)
        bottom.addWidget(self.status, 1)
        bottom.addWidget(cancel)
        bottom.addWidget(self.accept_button)
        layout.addLayout(bottom)

        if folder is not None:
            self.folders.select_folder(Path(folder))
        if procedure_id:
            self.procedure.setCurrentIndex(max(self.procedure.findData(procedure_id), 0))
        self.procedure.currentIndexChanged.connect(lambda _i: self._procedure_changed())
        self.name.textEdited.connect(self._name_typed)
        self.folders.folder_changed.connect(lambda _f: self._update_status())
        self._procedure_changed()

    def current_procedure(self) -> ProcedureTemplate | None:
        return self.library.procedures.get(self.procedure.currentData())

    def current_folder(self) -> Path | None:
        return self.folders.current_folder()

    def _name_typed(self, _text: str) -> None:
        self._name_edited = True
        self._update_status()

    def _procedure_changed(self) -> None:
        procedure = self.current_procedure()
        self.issues.clear()
        if procedure is None:
            self.plan = None
            self.summary.setText("La biblioteca no tiene plantillas de ensayo.")
            self.diagram.scene().clear()
            self._update_status()
            return
        if not self._name_edited:
            self.name.setText(default_test_name(procedure))
        self.plan = prepare_run(self.library, procedure)
        self.summary.setText(plan_html(self.library, self.plan))
        for issue in sorted(self.plan.issues, key=lambda i: -int(i.severity)):
            self.issues.addItem(QListWidgetItem(style.severity_icon(self, issue.severity), issue.message))
        if not self.plan.issues:
            self.issues.addItem(QListWidgetItem(style.severity_icon(self, None), "Todo listo: hardware setup, EUT y especificaciones compatibles"))
        if self.plan.setup is not None and self.plan.template is not None:
            self.diagram.show_setup(self.plan.template, self.plan.setup, self.library, self.plan.analysis, setup_state(self.plan.template))
            QTimer.singleShot(0, self.diagram.fit)
        else:
            self.diagram.scene().clear()
        simulated = self.plan.vna is not None and is_simulated(self.plan.vna, self.library)
        self.automatic.setEnabled(simulated)
        if not simulated:
            self.automatic.setChecked(False)
        self._update_status()

    def _update_status(self) -> None:
        folder = self.current_folder()
        problem = None
        if folder is None:
            problem = "Elija la carpeta del ensayo"
        elif not self.name.text().strip():
            problem = "Indique el nombre del ensayo"
        elif self.plan is None:
            problem = "Elija la plantilla de ensayo"
        elif not self.plan.ok:
            errors = [i.message for i in self.plan.issues if i.severity is Severity.ERROR]
            problem = f"{len(errors)} error(es): {errors[0]}"
        if problem:
            self.status.setText(f"<span style='color:{style.ERROR.name()}'>✖ {html.escape(problem)}</span>")
            self.accept_button.setEnabled(False)
            return
        warnings = sum(1 for i in self.plan.issues if i.severity is Severity.WARNING)
        extra = f" ({warnings} avisos)" if warnings else ""
        self.status.setText(f"<span style='color:{style.OK.name()}'>✔ Listo{extra}.</span> El ensayo se guardará en {html.escape(str(folder))}")
        self.accept_button.setEnabled(True)

    def _accept(self) -> None:
        if self.plan is None or not self.plan.ok or self.current_folder() is None:
            return
        QSettings(*SETTINGS).setValue("operador", self.operator.text().strip())
        self.accept()


# ── operator dialogs ───────────────────────────────────────────────────────
class PromptDialog(QDialog):
    """Request to the operator: connect a standard, the EUT… or confirm."""

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
    uncertainty = Signal(str, object)
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

    def uncertainty(self, name: str, frequencies, traces) -> None:
        self.worker.uncertainty.emit(name, traces)

    def progress(self, fraction: float, text: str = "") -> None:
        self.worker.progress.emit(fraction, text)


LOG_COLORS = {"error": "#c0392b", "aviso": "#d98a00", "operador": "#2b7bb9"}
SPEC_COLUMNS = ["Especificación", "Medida", "Parámetro", "Criterio", "Resultado", "Margen mínimo", "A la frecuencia", "Puntos fuera de especificación"]


# ── test window ────────────────────────────────────────────────────────────
class TestWindow(QWidget):
    """A launch of a test template (and then its record), or an existing test record.

    ``TestWindow(library, procedure, folder, name)`` prepares a launch;
    ``TestWindow(library, record=…)`` shows a test already made.
    """

    __test__ = False  # not a pytest test class

    finished = Signal(str)  # folder of the test
    record_changed = Signal(str)  # folder of the test: its record was changed (ambient conditions)

    def __init__(
        self,
        library: Library,
        procedure: ProcedureTemplate | None = None,
        folder: Path | None = None,
        name: str | None = None,
        operator_name: str = "",
        automatic: bool = False,
        record: TestRecord | None = None,
        parent: QWidget | None = None,
        today: date | None = None,
    ):
        super().__init__(parent)
        self.library = library
        self.procedure = procedure
        self.folder = Path(folder) if folder is not None else (record.folder.parent if record is not None and record.folder else library.tests_folder)
        self.name = name
        self.operator_name = operator_name or (record.operator if record is not None else "") or ""
        self.automatic = automatic
        self.today = today  # date of the checks (calibrations); None: today
        self.record = record
        self.result: RunResult | None = None
        self.thread: QThread | None = None
        self.worker: RunWorker | None = None
        self.runner: ProcedureRunner | None = None
        self.running = False
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
        self.folder_button = QPushButton(icon("carpeta", style.PRIMARY.name()), "Abrir carpeta")
        self.folder_button.clicked.connect(self.open_folder)
        self.conditions_button = QPushButton(icon("condiciones", style.PRIMARY.name()), "Condiciones ambientales…")
        self.conditions_button.setToolTip(
            "Cargar las condiciones ambientales del ensayo (fichero .xlsx de la sonda TPH) y completar la incertidumbre"
        )
        self.conditions_button.clicked.connect(lambda: self.load_conditions())
        self.save_button = QPushButton(icon("guardar", style.PRIMARY.name()), "Guardar ensayo…")
        self.save_button.setToolTip(
            "Guardar toda la información del ensayo en un .zip: hardware setup, plantilla, horas de inicio y fin, "
            "condiciones ambientales, resultados sin suavizado e incertidumbre"
        )
        self.save_button.clicked.connect(lambda: self.save_package())
        self.action_start = QAction(icon("ejecutar", style.OK.name()), "Iniciar", self)
        self.action_start.triggered.connect(self.start)
        self.action_stop = QAction(icon("detener", style.ERROR.name()), "Detener", self)
        self.action_stop.triggered.connect(self.stop)
        self.action_folder = QAction(icon("carpeta", style.PRIMARY.name()), "Abrir\ncarpeta", self)
        self.action_folder.triggered.connect(self.open_folder)
        self.action_conditions = QAction(icon("condiciones", style.PRIMARY.name()), "Condiciones\nambientales", self)
        self.action_conditions.setToolTip(self.conditions_button.toolTip())
        self.action_conditions.triggered.connect(lambda: self.load_conditions())
        self.action_save = QAction(icon("guardar", style.PRIMARY.name()), "Guardar\nensayo", self)
        self.action_save.setToolTip(self.save_button.toolTip())
        self.action_save.triggered.connect(lambda: self.save_package())
        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.setTextVisible(False)
        self.progress_label = QLabel()
        self.progress_label.setObjectName("pageSubtitle")
        controls.addWidget(self.start_button)
        controls.addWidget(self.stop_button)
        controls.addSpacing(10)
        controls.addWidget(self.progress, 1)
        controls.addWidget(self.progress_label)
        controls.addSpacing(10)
        controls.addWidget(self.conditions_button)
        controls.addWidget(self.save_button)
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
        for key, step_name in STEPS.items():
            item = QListWidgetItem(style.status_icon("pendiente"), step_name)
            item.setData(Qt.ItemDataRole.UserRole, key)
            self.steps.addItem(item)
            self.step_items[key] = item
        steps_card.body.addWidget(self.steps)
        plan_card = style.Card("Plantilla de ensayo", icon_name="info")
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
        if record is not None:
            self.show_record(record)
        else:
            self.show_launch()

    # ── state ──────────────────────────────────────────────────────────────
    @property
    def title(self) -> str:
        if self.record is not None:
            return f"Ensayo: {self.record.title}"
        return f"Ensayo: {self.name or (self.procedure.name if self.procedure else '')}"

    @property
    def test_folder(self) -> str | None:
        return str(self.record.folder) if self.record is not None and self.record.folder else None

    def ribbon_actions(self) -> list[QAction | None]:
        return [self.action_start, self.action_stop, None, self.action_conditions, self.action_save, None, self.action_folder]

    def _reset_steps(self) -> None:
        for key, item in self.step_items.items():
            item.setIcon(style.status_icon("pendiente"))
            item.setText(STEPS[key])

    def show_launch(self) -> None:
        """Before the run: what the template will do and its check."""
        assert self.procedure is not None
        self.record = None
        self.plan = prepare_run(self.library, self.procedure, self.today)
        self.title_label.setText(self.name or self.procedure.name)
        self.subtitle.setText(f"{self.procedure.name} · se guardará en {self.folder}")
        self._set_chips(None)
        self.plan_label.setText(plan_html(self.library, self.plan))
        self._show_diagram()
        self.plot.clear()
        self.results.setRowCount(0)
        self.spec_table.setRowCount(0)
        self.log_view.clear()
        self._reset_steps()
        for issue in self.plan.issues:
            self._append_log("error" if issue.severity is Severity.ERROR else "aviso", issue.message)
        runnable = self.plan.ok
        self.badge.set_state("Listo para lanzar" if runnable else "No ejecutable", style.INDIGO if runnable else style.ERROR)
        self.progress.setValue(0)
        self.progress_label.setText("")
        self.start_button.setText("Iniciar ensayo")
        self.start_button.setEnabled(runnable)
        self.stop_button.setEnabled(False)
        self.folder_button.setEnabled(self.folder.is_dir())
        self.conditions_button.setEnabled(False)
        self.save_button.setEnabled(False)
        self._sync_actions()

    def show_record(self, record: TestRecord) -> None:
        """A test already made: its record and files."""
        self.record = record
        template = record.launched_template()
        self.title_label.setText(record.title)
        setup_name = record.setup.get("nombre", record.setup.get("id", "—"))
        self.subtitle.setText(
            f"{record.procedure.get('nombre', '—')} · setup {setup_name} · EUT {record.dut_id or '—'} · {record.folder}"
        )
        self._set_chips(record)
        current = self.library.procedures.get(record.template_id or "")
        self.procedure = current
        self.plan = None
        summary = []
        if template is not None:
            summary.append(f"<b>Plantilla</b> (como se lanzó): {html.escape(template.name)} v{html.escape(template.version)}")
        frequencies = record.sweep
        if frequencies:
            summary.append(
                f"<b>Frecuencias</b> (del setup): {html.escape(str(frequencies.get('modo', '')))} {frequencies.get('inicio')} – "
                f"{frequencies.get('fin')}, {frequencies.get('puntos')} puntos"
            )
        if record.dut:
            summary.append(f"<b>EUT:</b> {html.escape(str(record.dut.get('id')))} — {html.escape(str(record.dut.get('modelo', '')))}")
        if record.specifications:
            summary.append("<b>Especificaciones:</b> " + html.escape("; ".join(str(s.get("nombre")) for s in record.specifications)))
        if record.uncertainty:
            k = record_coverage_factor(record)
            summary.append(f"<b>Incertidumbre</b> (k={k:g}): {html.escape(str(record.uncertainty.get('documento', '—')))}")
        self.plan_label.setText("<br>".join(summary))
        self._show_diagram()
        self.log_view.clear()
        self._reset_steps()
        for step in record.steps:
            self._step(step.key, step.status, step.detail)
        self.progress.setValue(1000 if record.status in ("completado", "verificacion_fallida") else self.progress.value())
        self.progress_label.setText(record.status_name)
        self.badge.set_state(record.status_name, style.status_color(record.status))
        self.plot.clear()
        for label, frequencies_hz, s in record_datasets(record):
            self.plot.set_data(label, frequencies_hz, s, log_x=is_logarithmic(record))
        for label, bands in record_uncertainty(record).items():
            self.plot.set_uncertainty(label, bands, record_coverage_factor(record))
        self.plot.set_limits(record_spec_curves(record))
        self.plot.set_smoothing(record_smoothing(record))
        self._fill_results(record)
        self._fill_specs(record)
        log = record.folder / "registro.txt" if record.folder else None
        if log is not None and log.is_file():
            self.log_view.setPlainText(log.read_text(encoding="utf-8", errors="replace"))
        self.start_button.setText("Repetir ensayo")
        self.start_button.setToolTip("Lanza otra vez la plantilla en la misma carpeta (un ensayo nuevo)")
        self.start_button.setEnabled(current is not None)
        self.stop_button.setEnabled(False)
        self.folder_button.setEnabled(record.folder is not None)
        done = record.folder is not None and record.status != "en_curso"
        self.conditions_button.setEnabled(done)
        self.save_button.setEnabled(done)
        self._sync_actions()

    def _set_chips(self, record: TestRecord | None) -> None:
        while self.chips.count():
            widget = self.chips.takeAt(0).widget()
            if widget:
                widget.deleteLater()
        simulated = record.simulated if record is not None else (self.plan is not None and self.plan.vna is not None and is_simulated(self.plan.vna, self.library))
        if simulated:
            self.chips.addWidget(style.chip("SIMULACIÓN" + (" AUTOMÁTICA" if self.automatic else ""), style.SIMULATION), 0, Qt.AlignmentFlag.AlignVCenter)
        chip = verdict_chip(record.verdict if record is not None else None)
        if chip is not None:
            self.chips.addWidget(chip, 0, Qt.AlignmentFlag.AlignVCenter)
        chip = pending_chip(record)
        if chip is not None:
            self.chips.addWidget(chip, 0, Qt.AlignmentFlag.AlignVCenter)

    def _show_diagram(self) -> None:
        plan = self.plan
        if plan is None and self.procedure is not None:
            plan = prepare_run(self.library, self.procedure, self.today)
        if plan is None or plan.setup is None or plan.template is None:
            self.diagram.scene().clear()
            return
        self.diagram.show_setup(plan.template, plan.setup, self.library, plan.analysis, setup_state(plan.template))
        QTimer.singleShot(0, self.diagram.fit)

    def _sync_actions(self) -> None:
        self.action_start.setText("Repetir" if self.record is not None and not self.running else "Iniciar")
        self.action_start.setEnabled(self.start_button.isEnabled())
        self.action_stop.setEnabled(self.stop_button.isEnabled())
        self.action_folder.setEnabled(self.folder_button.isEnabled())
        self.action_conditions.setEnabled(self.conditions_button.isEnabled())
        self.action_save.setEnabled(self.save_button.isEnabled())

    # ── actions ────────────────────────────────────────────────────────────
    def start(self) -> None:
        """Launch the template (again, after a test: a new test in the same folder)."""
        if self.running:
            return
        if self.record is not None:
            if self.procedure is None:
                QMessageBox.warning(self, "Repetir ensayo", "La plantilla de ensayo de este ensayo ya no existe en la biblioteca.")
                return
            self.name = f"{self.record.title} (repetición)"
            self.show_launch()
            if self.plan is None or not self.plan.ok:
                return
        assert self.procedure is not None
        self.result = None
        self.plot.clear()
        self.plot.set_smoothing(self.procedure.sweep.smoothing(self.procedure.traces))
        self.results.setRowCount(0)
        self.spec_table.setRowCount(0)
        self.log_view.clear()
        self._reset_steps()
        self.bridge = PromptBridge(self)
        operator = AutoOperator() if self.automatic else GuiOperator(self.bridge)
        self.worker = RunWorker(None)
        listener = _Listener(self.worker)
        self.runner = ProcedureRunner(
            self.library,
            self.procedure,
            operator,
            listener,
            self.operator_name or None,
            folder=self.folder,
            name=self.name or default_test_name(self.procedure),
            today=self.today,
        )
        self.worker.runner = self.runner
        self.thread = QThread(self)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.step.connect(self._step)
        self.worker.log.connect(self._append_log)
        self.worker.data.connect(self._data)
        self.worker.uncertainty.connect(self._uncertainty)
        self.worker.progress.connect(self._progress)
        self.worker.finished.connect(self._finished)
        self.worker.finished.connect(self.thread.quit)
        self.running = True
        self.start_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self.conditions_button.setEnabled(False)
        self.save_button.setEnabled(False)
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
        folder = self.record.folder if self.record is not None and self.record.folder else self.folder
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    def load_conditions(self, path: str | Path | None = None) -> bool:
        """Ambient conditions of the test (workbook of the TPH probe): the uncertainty is completed with them."""
        if self.record is None or self.running:
            return False
        if not load_conditions(self, self.library, self.record, path):
            return False
        self.show_record(self.record)
        self._append_log("info", "Condiciones ambientales cargadas" + (": incertidumbre recalculada" if self.record.uncertainty else ""))
        self.record_changed.emit(str(self.record.folder))
        return True

    def save_package(self, path: str | Path | None = None) -> Path | None:
        """All the information of the test in a .zip."""
        if self.record is None or self.running:
            return None
        return save_package(self, self.record, path)

    def reload_record(self) -> None:
        """Read the record again (it was changed elsewhere)."""
        if self.record is None or self.record.folder is None or self.running:
            return
        try:
            self.show_record(self.library.load_test(self.record.folder))
        except LibraryError:
            return

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
        log_x = self.plan is not None and self.plan.frequency_plan is not None and self.plan.frequency_plan.mode == "logaritmico"
        self.plot.set_data(name, frequencies, s, log_x=log_x)
        if not name.startswith("Verificación"):
            self.tabs.setCurrentWidget(self.plot)

    def _uncertainty(self, name: str, traces) -> None:
        self.plot.set_uncertainty(name, traces, self.procedure.uncertainty.coverage_factor if self.procedure else 2.0)

    def _progress(self, fraction: float, text: str) -> None:
        self.progress_label.setText(text)

    def _finished(self, result: RunResult) -> None:
        self.running = False
        self.result = result
        record = result.record
        self.show_record(record)
        if result.data and result.frequencies is not None:
            self.plot.clear()
            log_x = is_logarithmic(record)
            for data_name, s in result.data.items():
                self.plot.set_data(data_name, result.frequencies, s, log_x=log_x)
            for data_name, traces in result.uncertainty.items():
                self.plot.set_uncertainty(data_name, traces, record_coverage_factor(record))
            self.plot.set_limits(record_spec_curves(record))
            self.plot.set_smoothing(record_smoothing(record))
        if record.spec_results:
            self.tabs.setCurrentWidget(self.spec_table if record.verdict == "FALLA" else self.plot)
        self.finished.emit(str(record.folder))

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
                        item.setForeground(style.VERDICT_COLORS.get(result, style.NEUTRAL))
                    if column == 4:
                        name = {"PASA": "ok", "FALLA": "error"}.get(result, "aviso")
                        item.setIcon(icon(name, style.VERDICT_COLORS.get(result, style.WARNING).name()))
                    item.setToolTip("\n".join(entry.get("detalle", [])))
                    self.spec_table.setItem(row, column, item)
        if not record.spec_results:
            self.spec_table.insertRow(0)
            text = "El modelo del EUT no tiene especificaciones S: sin veredicto" if record.verdict == "SIN_ESPECIFICACIONES" else "Sin evaluación"
            self.spec_table.setItem(0, 0, QTableWidgetItem(text))
        self.spec_table.resizeColumnsToContents()
        verdict = record.verdict
        self.tabs.setTabText(1, "Pasa / falla" + (f" — {VERDICTS[verdict].upper()}" if verdict and verdict != "SIN_ESPECIFICACIONES" else ""))

    def _fill_results(self, record: TestRecord) -> None:
        rows: list[tuple[str, str]] = [("Ensayo", f"{record.title} [{record.id}]"), ("Estado", record.status_name)]
        if record.verdict:
            rows.append(("Veredicto", VERDICTS.get(record.verdict, record.verdict)))
        if record.message:
            rows.append(("Mensaje", record.message))
        sweep = record.sweep
        if sweep:
            rows.append(("Barrido", f"{sweep.get('modo', '')} {sweep.get('inicio')} – {sweep.get('fin')}, {sweep.get('puntos')} puntos, IFBW {sweep.get('ifbw')}"))
        rows += setup_rows(record)
        calibration = record.calibration
        if calibration:
            text = method_name(calibration.get("metodo")) + (f" · kit {calibration['kit']}" if calibration.get("kit") else "")
            if calibration.get("through"):
                text += f" · through {calibration['through']}"
            rows.append(("Calibración", text))
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
        rows += uncertainty_rows(record)
        if record.files:
            rows.append(("Ficheros", ", ".join(record.files)))
        if record.folder is not None:
            rows.append(("Carpeta", str(record.folder)))
        self.results.setRowCount(0)
        for row_name, value in rows:
            row = self.results.rowCount()
            self.results.insertRow(row)
            self.results.setItem(row, 0, QTableWidgetItem(row_name))
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
