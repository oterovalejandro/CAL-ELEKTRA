"""Test execution (ELEKTRA-like): new-test dialog, run window and operator dialogs.

The procedure runs in a worker thread. Operator requests are shown in the GUI
thread through a blocking queued signal, so the run waits for the answer just
like the real measurement waits for the operator.
"""

from __future__ import annotations

import html
from datetime import datetime

from PySide6.QtCore import QObject, QSettings, Qt, QThread, QUrl, Signal, Slot
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

from calelektra.catalog.library import Library
from calelektra.core.issues import Severity
from calelektra.gui import style
from calelektra.gui.icons import icon
from calelektra.gui.illustrations import illustration
from calelektra.gui.plots import SParameterPlot
from calelektra.instruments.factory import is_simulated
from calelektra.procedures.engine import (
    STEPS,
    AutoOperator,
    ProcedureRunner,
    PromptRequest,
    RunListener,
    RunPlan,
    RunResult,
    prepare_run,
)
from calelektra.procedures.model import ALGORITHMS, ProcedureTemplate
from calelektra.setups.model import ConfiguredSetup


def compatible_setups(library: Library, procedure: ProcedureTemplate) -> list[ConfiguredSetup]:
    """Setups whose template (or an ancestor) is in the procedure's compatible templates."""
    result = []
    for setup in library.setups.values():
        chain, current = [], setup.template
        while current and current not in chain:
            chain.append(current)
            template = library.templates.get(current)
            current = template.extends if template else None
        if not procedure.compatible_templates or set(chain) & set(procedure.compatible_templates):
            result.append(setup)
    return sorted(result, key=lambda s: s.name)


def plan_html(library: Library, plan: RunPlan) -> str:
    procedure, setup = plan.procedure, plan.setup
    sweep = procedure.sweep
    lines = [
        f"<b>Algoritmo:</b> {html.escape(ALGORITHMS[procedure.algorithm])}",
        f"<b>Rango:</b> {plan.frequency_range} · {sweep.points} puntos {sweep.sweep_type} · IFBW {sweep.if_bandwidth:g} Hz · {sweep.power_dbm:g} dBm",
    ]
    if plan.vna is not None:
        simulated = " <span style='color:#6c3483'><b>(SIMULADO)</b></span>" if is_simulated(plan.vna, library) else ""
        lines.append(f"<b>VNA:</b> {plan.vna.id}{simulated}")
    if plan.dut is not None:
        lines.append(f"<b>DUT:</b> {plan.dut.id}")
    design = plan.design
    if procedure.calibration.method == "existente":
        lines.append("<b>Calibración:</b> se usa la calibración activa del VNA")
    elif design is not None and design.method:
        planes = ", ".join(f"{p.name} → P{p.vna_port} ({p.connector.label() if p.connector else '?'})" for p in design.planes)
        kit = f" · kit {design.kit.id}" if design.kit else " · kit del VNA"
        lines.append(f"<b>Calibración:</b> {design.method}{kit}<br>&nbsp;&nbsp;{html.escape(design.reason)}<br>&nbsp;&nbsp;Planos: {html.escape(planes)}")
    if plan.lines != ["—"]:
        lines.append(f"<b>Líneas:</b> {', '.join(plan.lines)}")
    return "<br>".join(lines)


class NewTestDialog(QDialog):
    """Procedure + compatible setup + operator, with the pre-run check."""

    def __init__(self, library: Library, procedure_id: str | None = None, setup_id: str | None = None, parent: QWidget | None = None):
        super().__init__(parent)
        self.library = library
        self.plan: RunPlan | None = None
        self.setWindowTitle("Nuevo ensayo")
        self.resize(940, 640)
        layout = QVBoxLayout(self)
        layout.addWidget(style.PageHeader("Nuevo ensayo", "Elija el procedimiento y un hardware setup compatible. La comprobación previa usa los campos de los equipos."))
        body = QHBoxLayout()
        left = style.Card("Selección", icon_name="ensayos")
        form = QFormLayout()
        left.body.addLayout(form)
        self.procedure = QComboBox()
        for procedure in sorted(library.procedures.values(), key=lambda p: p.name):
            self.procedure.addItem(icon("procedimientos", style.PRIMARY.name()), procedure.name, procedure.id)
        self.setups = QListWidget()
        self.operator = QLineEdit()
        self.operator.setPlaceholderText("Nombre del operador")
        self.operator.setText(str(QSettings("CAL-ELEKTRA", "CAL-ELEKTRA").value("operador", "")))
        self.automatic = QCheckBox("Simulación automática (sin diálogos al operador)")
        self.automatic.setToolTip("Solo con el VNA simulado: el banco simulado conecta los estándares y el DUT sin preguntar.")
        form.addRow("Procedimiento", self.procedure)
        form.addRow("Hardware setup", self.setups)
        form.addRow("Operador", self.operator)
        form.addRow(self.automatic)
        right = style.Card("Comprobación previa", icon_name="ok")
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        self.summary.setTextFormat(Qt.TextFormat.RichText)
        self.issues = QListWidget()
        self.issues.setWordWrap(True)
        right.body.addWidget(self.summary)
        right.body.addWidget(self.issues, 1)
        body.addWidget(left, 2)
        body.addWidget(right, 3)
        layout.addLayout(body, 1)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("Cancelar")
        cancel.clicked.connect(self.reject)
        self.start = QPushButton(icon("ejecutar", "#ffffff"), "Abrir ensayo")
        self.start.setObjectName("primary")
        self.start.clicked.connect(self._accept)
        buttons.addWidget(cancel)
        buttons.addWidget(self.start)
        layout.addLayout(buttons)
        self.procedure.currentIndexChanged.connect(lambda _i: self._fill_setups(setup_id))
        self.setups.currentItemChanged.connect(lambda *_: self._check())
        if procedure_id:
            self.procedure.setCurrentIndex(max(self.procedure.findData(procedure_id), 0))
        elif setup_id and setup_id in library.setups:
            for index in range(self.procedure.count()):
                procedure = library.procedures[self.procedure.itemData(index)]
                if any(s.id == setup_id for s in compatible_setups(library, procedure)):
                    self.procedure.setCurrentIndex(index)
                    break
        self._fill_setups(setup_id)

    def _fill_setups(self, preferred: str | None = None) -> None:
        procedure = self.library.procedures.get(self.procedure.currentData())
        self.setups.clear()
        if procedure is None:
            self._check()
            return
        for setup in compatible_setups(self.library, procedure):
            item = QListWidgetItem(icon("setups", style.PRIMARY.name()), f"{setup.name}\n{setup.required_range}")
            item.setData(Qt.ItemDataRole.UserRole, setup.id)
            self.setups.addItem(item)
            if setup.id == preferred:
                self.setups.setCurrentItem(item)
        if self.setups.currentItem() is None and self.setups.count():
            self.setups.setCurrentRow(0)
        if not self.setups.count():
            self._check()

    def _check(self) -> None:
        self.issues.clear()
        procedure = self.library.procedures.get(self.procedure.currentData())
        item = self.setups.currentItem()
        if procedure is None or item is None:
            self.plan = None
            self.summary.setText("No hay ningún hardware setup compatible con este procedimiento.")
            self.start.setEnabled(False)
            return
        setup = self.library.setups[item.data(Qt.ItemDataRole.UserRole)]
        self.plan = prepare_run(self.library, procedure, setup)
        self.summary.setText(plan_html(self.library, self.plan))
        for issue in sorted(self.plan.issues, key=lambda i: -int(i.severity)):
            self.issues.addItem(QListWidgetItem(style.severity_icon(self, issue.severity), issue.message))
        if not self.plan.issues:
            self.issues.addItem(QListWidgetItem(style.severity_icon(self, None), "Todo listo: setup y procedimiento compatibles"))
        simulated = self.plan.vna is not None and is_simulated(self.plan.vna, self.library)
        self.automatic.setEnabled(simulated)
        if not simulated:
            self.automatic.setChecked(False)
        self.start.setEnabled(self.plan.ok)

    def _accept(self) -> None:
        if self.plan is None or not self.plan.ok:
            return
        QSettings("CAL-ELEKTRA", "CAL-ELEKTRA").setValue("operador", self.operator.text().strip())
        self.accept()


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

    def __init__(self, runner: ProcedureRunner):
        super().__init__()
        self.runner = runner

    @Slot()
    def run(self) -> None:
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


class TestWindow(QWidget):
    """Run of one procedure on one setup: steps, live chart, results and log."""

    __test__ = False  # not a pytest test class

    finished = Signal(str)  # test id

    def __init__(self, library: Library, procedure: ProcedureTemplate, setup: ConfiguredSetup, operator_name: str = "", automatic: bool = False, parent: QWidget | None = None):
        super().__init__(parent)
        self.library = library
        self.procedure = procedure
        self.setup = setup
        self.operator_name = operator_name
        self.automatic = automatic
        self.result: RunResult | None = None
        self.thread: QThread | None = None
        self.worker: RunWorker | None = None
        self.runner: ProcedureRunner | None = None
        self.running = False
        plan = prepare_run(library, procedure, setup)
        self.plan = plan
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 8)

        header = QHBoxLayout()
        texts = QVBoxLayout()
        title = QLabel(procedure.name)
        title.setObjectName("pageTitle")
        subtitle = QLabel(f"Setup: {setup.name} · DUT: {plan.dut.id if plan.dut else '—'} · VNA: {plan.vna.id if plan.vna else '—'}")
        subtitle.setObjectName("pageSubtitle")
        texts.addWidget(title)
        texts.addWidget(subtitle)
        header.addLayout(texts, 1)
        if plan.vna is not None and is_simulated(plan.vna, library):
            header.addWidget(style.chip("SIMULACIÓN" + (" AUTOMÁTICA" if automatic else ""), style.SIMULATION))
        self.badge = style.StatusBadge()
        self.badge.set_state("Preparado", style.NEUTRAL)
        header.addWidget(self.badge)
        layout.addLayout(header)

        controls = QHBoxLayout()
        self.start_button = QPushButton(icon("ejecutar", "#ffffff"), "Iniciar ensayo")
        self.start_button.setObjectName("primary")
        self.start_button.clicked.connect(self.start)
        self.stop_button = QPushButton(icon("detener", style.ERROR.name()), "Detener")
        self.stop_button.setObjectName("danger")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self.stop)
        self.folder_button = QPushButton(icon("carpeta", style.PRIMARY.name()), "Abrir carpeta de resultados")
        self.folder_button.setEnabled(False)
        self.folder_button.clicked.connect(self.open_folder)
        self.action_start = QAction(icon("ejecutar", style.OK.name()), "Iniciar", self)
        self.action_start.triggered.connect(self.start)
        self.action_stop = QAction(icon("detener", style.ERROR.name()), "Detener", self)
        self.action_stop.setEnabled(False)
        self.action_stop.triggered.connect(self.stop)
        self.action_folder = QAction(icon("carpeta", style.PRIMARY.name()), "Abrir\ncarpeta", self)
        self.action_folder.setEnabled(False)
        self.action_folder.triggered.connect(self.open_folder)
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
        controls.addWidget(self.folder_button)
        layout.addLayout(controls)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        steps_card = style.Card("Flujo del ensayo", icon_name="procedimientos")
        self.steps = QListWidget()
        self.steps.setMinimumWidth(320)
        self.steps.setWordWrap(True)
        self.step_items: dict[str, QListWidgetItem] = {}
        for key, name in STEPS.items():
            item = QListWidgetItem(style.status_icon("pendiente"), name)
            item.setData(Qt.ItemDataRole.UserRole, key)
            self.steps.addItem(item)
            self.step_items[key] = item
        steps_card.body.addWidget(self.steps)
        plan_card = style.Card("Plan", icon_name="info")
        self.plan_label = QLabel(plan_html(library, plan))
        self.plan_label.setWordWrap(True)
        self.plan_label.setTextFormat(Qt.TextFormat.RichText)
        plan_card.body.addWidget(self.plan_label)
        left_layout.addWidget(steps_card, 3)
        left_layout.addWidget(plan_card, 2)

        self.tabs = QTabWidget()
        self.plot = SParameterPlot()
        self.results = style.make_table(["Resultado", "Valor"], stretch_column=1)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.tabs.addTab(self.plot, icon("grafica", style.PRIMARY.name()), "Gráfica")
        self.tabs.addTab(self.results, icon("ok", style.PRIMARY.name()), "Resultados")
        self.tabs.addTab(self.log_view, icon("procedimientos", style.PRIMARY.name()), "Registro")
        splitter.addWidget(left)
        splitter.addWidget(self.tabs)
        splitter.setStretchFactor(1, 3)
        layout.addWidget(splitter, 1)

        for issue in plan.issues:
            self._append_log("error" if issue.severity is Severity.ERROR else "aviso", issue.message)
        if not plan.ok:
            self.start_button.setEnabled(False)
            self.badge.set_state("No ejecutable", style.ERROR)
        self._sync_actions()

    def ribbon_actions(self) -> list[QAction | None]:
        return [self.action_start, self.action_stop, None, self.action_folder]

    def _sync_actions(self) -> None:
        self.action_start.setEnabled(self.start_button.isEnabled())
        self.action_stop.setEnabled(self.stop_button.isEnabled())
        self.action_folder.setEnabled(self.folder_button.isEnabled())

    @property
    def title(self) -> str:
        return f"Ensayo: {self.plan.dut.id if self.plan.dut else self.procedure.name}"

    # ── run control ────────────────────────────────────────────────────────
    def start(self) -> None:
        if self.running:
            return
        self.result = None
        self.plot.clear()
        self.results.setRowCount(0)
        for key, item in self.step_items.items():
            item.setIcon(style.status_icon("pendiente"))
            item.setText(STEPS[key])
        bridge = PromptBridge(self)
        self.bridge = bridge
        operator = AutoOperator() if self.automatic else GuiOperator(bridge)
        self.worker = RunWorker(None)  # type: ignore[arg-type]
        listener = _Listener(self.worker)
        self.runner = ProcedureRunner(self.library, self.procedure, self.setup, operator, listener, self.operator_name or None)
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
        self.folder_button.setEnabled(False)
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
        if self.result is not None and self.result.record.folder is not None:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.result.record.folder)))

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
        self.plot.set_data(name, frequencies, s, log_x=self.procedure.sweep.sweep_type == "logaritmico")
        if not name.startswith("Verificación"):
            self.tabs.setCurrentWidget(self.plot)

    def _progress(self, fraction: float, text: str) -> None:
        self.progress_label.setText(text)

    def _finished(self, result: RunResult) -> None:
        self.running = False
        self.result = result
        record = result.record
        self.start_button.setEnabled(True)
        self.start_button.setText("Repetir ensayo")
        self.stop_button.setEnabled(False)
        self.folder_button.setEnabled(record.folder is not None)
        self.progress.setValue(1000 if record.status in ("completado", "verificacion_fallida") else self.progress.value())
        self.progress_label.setText(record.status_name)
        self.badge.set_state(record.status_name, style.status_color(record.status))
        if "DUT" in result.data or len(result.data) > 0:
            self.plot.clear()
            for name, s in result.data.items():
                if result.frequencies is not None:
                    self.plot.set_data(name, result.frequencies, s, log_x=self.procedure.sweep.sweep_type == "logaritmico")
        self._fill_results(result)
        self._sync_actions()
        self.finished.emit(record.id)

    def _fill_results(self, result: RunResult) -> None:
        record = result.record
        rows: list[tuple[str, str]] = [("Ensayo", record.id), ("Estado", record.status_name)]
        if record.message:
            rows.append(("Mensaje", record.message))
        calibration = record.calibration
        if calibration:
            rows.append(("Calibración", f"{calibration.get('metodo', '—')}" + (f" · kit {calibration['kit']}" if calibration.get("kit") else "")))
        verification = record.verification
        for check in verification.get("comprobaciones", []):
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

