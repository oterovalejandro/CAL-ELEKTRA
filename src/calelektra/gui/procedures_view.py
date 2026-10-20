"""Test templates (measurement procedures, the «test templates» of ELEKTRA): list and editor.

A template defines how to measure: algorithm, frequency points (linear by
points or step, logarithmic, CSV list), trace settings (IFBW, averaging,
smoothing for all traces or per trace), calibration, verification, the S2P
specification files that give pass / fail, and export.
"""

from __future__ import annotations

import re

import yaml
from pydantic import ValidationError
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGridLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QScrollArea,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from calelektra.catalog.library import Library, validation_message
from calelektra.gui import style
from calelektra.gui.icons import icon
from calelektra.gui.template_widgets import FrequencyEditor, SpecEditor, TraceEditor
from calelektra.procedures.model import ALGORITHMS, CALIBRATION_METHODS, TRACES, ProcedureTemplate

ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]*$")


class ProcedureEditor(QWidget):
    changed = Signal()

    def __init__(self, library: Library, parent: QWidget | None = None):
        super().__init__(parent)
        self.library = library
        self.original: ProcedureTemplate | None = None
        self._loading = False
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
        self.description.setMaximumHeight(70)
        self.algorithm = QComboBox()
        for key, text in ALGORITHMS.items():
            self.algorithm.addItem(text, key)
        self.templates = QListWidget()
        self.templates.setMaximumHeight(130)
        form.addRow("Identificador *", self.id_edit)
        form.addRow("Nombre *", self.name)
        form.addRow("Versión", self.version)
        form.addRow("Estado", self.status)
        form.addRow("Descripción", self.description)
        form.addRow("Algoritmo", self.algorithm)
        form.addRow("Plantillas compatibles", self.templates)

        frequency = style.Card("Rango de frecuencias", icon_name="grafica")
        self.frequency = FrequencyEditor(
            resolver=lambda reference: self.library.procedure_file(reference),
            importer=lambda path: self.library.copy_procedure_file(path, "listas"),
        )
        frequency.body.addWidget(self.frequency)
        traces = style.Card("Trazas: IFBW, promedios y suavizado", icon_name="ajustes")
        self.traces = TraceEditor()
        traces.body.addWidget(self.traces)
        specs = style.Card("Especificaciones (pasa / falla)", icon_name="ok")
        specs_note = QLabel(
            "Ficheros S2P/S1P con los valores de especificación. Se copian en «procedimientos/especificaciones» "
            "y cada ensayo guarda su propia copia. Al crear un ensayo se pueden cambiar."
        )
        specs_note.setObjectName("pageSubtitle")
        specs_note.setWordWrap(True)
        self.specs = SpecEditor(
            resolver=lambda reference: self.library.procedure_file(reference),
            importer=lambda path: self.library.copy_procedure_file(path, "especificaciones"),
        )
        specs.body.addWidget(specs_note)
        specs.body.addWidget(self.specs)

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

        options = style.Card("Opciones del algoritmo (YAML)", icon_name="ajustes")
        self.options = QPlainTextEdit()
        self.options.setPlaceholderText("lineas: todas\ntolerancia_z_pct: 20")
        self.options.setMaximumHeight(110)
        options.body.addWidget(self.options)

        left_column, right_column = QVBoxLayout(), QVBoxLayout()
        for column, cards in ((left_column, (general, frequency, traces)), (right_column, (calibration, verification, export))):
            column.setSpacing(12)
            for card in cards:
                column.addWidget(card)
            column.addStretch(1)
        grid.addLayout(left_column, 0, 0)
        grid.addLayout(right_column, 0, 1)
        grid.setColumnStretch(0, 3)
        grid.setColumnStretch(1, 2)
        grid.addWidget(specs, 1, 0, 1, 2)
        grid.addWidget(options, 2, 0, 1, 2)
        layout.addLayout(grid)
        layout.addStretch(1)
        for combo in (self.algorithm, self.method, self.data_format, self.unit):
            combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            combo.setMinimumContentsLength(14)

        for widget in (self.id_edit, self.name, self.version, self.status, self.vna_name, self.filename):
            widget.textEdited.connect(self._edited)
        for widget in (self.description, self.options):
            widget.textChanged.connect(self._edited)
        for widget in (self.algorithm, self.method, self.data_format, self.unit):
            widget.currentIndexChanged.connect(self._edited)
        for widget in (self.tolerance, self.s11_max):
            widget.valueChanged.connect(self._edited)
        for widget in (self.save_on_vna, self.verify, self.csv):
            widget.toggled.connect(self._edited)
        for editor in (self.frequency, self.traces, self.specs):
            editor.changed.connect(self._edited)
        self.templates.itemChanged.connect(self._edited)
        self.algorithm.currentIndexChanged.connect(self._algorithm_changed)

    def _algorithm_changed(self, *_args) -> None:
        traces = TRACES[self.algorithm.currentData()]
        self.traces.set_traces(traces)
        self.specs.set_traces(traces)

    def _edited(self, *_args) -> None:
        if not self._loading:
            self.changed.emit()

    def load(self, procedure: ProcedureTemplate, is_new: bool = False) -> None:
        self._loading = True
        self.original = None if is_new else procedure
        self.id_edit.setText(procedure.id)
        self.id_edit.setReadOnly(not is_new)
        self.name.setText(procedure.name)
        self.version.setText(procedure.version)
        self.status.setText(procedure.status or "")
        self.description.setPlainText(procedure.description or "")
        self.algorithm.setCurrentIndex(max(self.algorithm.findData(procedure.algorithm), 0))
        self.templates.clear()
        for template in sorted(self.library.templates.values(), key=lambda t: t.name):
            item = QListWidgetItem(template.name)
            item.setData(Qt.ItemDataRole.UserRole, template.id)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if template.id in procedure.compatible_templates else Qt.CheckState.Unchecked)
            self.templates.addItem(item)
        self._algorithm_changed()
        self.frequency.load(procedure.sweep)
        self.traces.load(procedure.sweep)
        self.specs.load(procedure.specifications)
        calibration = procedure.calibration
        self.method.setCurrentIndex(max(self.method.findData(calibration.method), 0))
        self.save_on_vna.setChecked(calibration.save_on_vna)
        self.vna_name.setText(calibration.vna_cal_name)
        verification = procedure.verification
        self.verify.setChecked(verification.active)
        self.tolerance.setValue(verification.s21_tolerance_db)
        self.s11_max.setValue(verification.s11_max_db)
        export = procedure.export
        self.data_format.setCurrentIndex(max(self.data_format.findData(export.data_format), 0))
        self.unit.setCurrentText(export.frequency_unit)
        self.filename.setText(export.filename)
        self.csv.setChecked(export.csv)
        self.options.setPlainText(yaml.safe_dump(procedure.options, allow_unicode=True, sort_keys=False) if procedure.options else "")
        self._loading = False

    def build(self) -> ProcedureTemplate:
        procedure_id = self.id_edit.text().strip()
        if not ID_PATTERN.match(procedure_id):
            raise ValueError("Identificador no válido (letras, números, «-», «_» o «.»)")
        sweep = {**self.frequency.values(), **self.traces.values()}
        specifications = self.specs.values()
        try:
            options = yaml.safe_load(self.options.toPlainText()) or {}
        except yaml.YAMLError as exc:
            raise ValueError(f"Opciones: YAML no válido ({exc})") from exc
        if not isinstance(options, dict):
            raise ValueError("Opciones: se espera un diccionario YAML (clave: valor)")
        templates = [
            self.templates.item(i).data(Qt.ItemDataRole.UserRole)
            for i in range(self.templates.count())
            if self.templates.item(i).checkState() == Qt.CheckState.Checked
        ]
        data = {
            "formato": "calelektra/procedimiento@0.3",
            "id": procedure_id,
            "nombre": self.name.text().strip(),
            "version": self.version.text().strip() or "1.0",
            "estado": self.status.text().strip() or None,
            "descripcion": self.description.toPlainText().strip() or None,
            "algoritmo": self.algorithm.currentData(),
            "plantillas_compatibles": templates,
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
            "especificaciones": specifications,
            "exportacion": {
                "formato": self.data_format.currentData(),
                "unidad_frecuencia": self.unit.currentText(),
                "nombre_fichero": self.filename.text().strip() or "{dut}_{fecha}",
                "csv": self.csv.isChecked(),
            },
            "opciones": options,
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
    new_test = Signal(str)
    procedures_changed = Signal()
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
            "Las «test templates» de ELEKTRA: algoritmo, rango de frecuencias, trazas, calibración, verificación, "
            "especificaciones (pasa / falla) y exportación. Un ensayo se lanza en una carpeta con una plantilla, "
            "un hardware setup compatible y el equipo a calibrar.",
        )
        layout.addWidget(self.header)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.list = QListWidget()
        self.list.setMinimumWidth(280)
        self.list.currentItemChanged.connect(self._selected)
        self.editor = ProcedureEditor(library)
        self.editor.changed.connect(self._changed)
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
        self.action_test = QAction(icon("ejecutar", style.OK.name()), "Nuevo\nensayo", self)
        self.action_new.triggered.connect(self.new_procedure)
        self.action_copy.triggered.connect(self.duplicate)
        self.action_save.triggered.connect(self.save)
        self.action_delete.triggered.connect(self.delete)
        self.action_test.triggered.connect(lambda: self._current_id() and self.new_test.emit(self._current_id()))
        self.reload()

    def ribbon_actions(self) -> list[QAction | None]:
        return [self.action_new, self.action_copy, self.action_save, self.action_delete, None, self.action_test]

    def set_library(self, library: Library) -> None:
        self.library = library
        self.editor.library = library
        self.reload()

    def reload(self, select: str | None = None) -> None:
        current = select or self._current_id()
        self.list.blockSignals(True)
        self.list.clear()
        for procedure in sorted(self.library.procedures.values(), key=lambda p: p.name):
            item = QListWidgetItem(icon("procedimientos", style.PRIMARY.name()), f"{procedure.name}\n{ALGORITHMS[procedure.algorithm].split(' (')[0]}")
            item.setData(Qt.ItemDataRole.UserRole, procedure.id)
            item.setToolTip(str(procedure.source_path or ""))
            self.list.addItem(item)
        row = next((i for i in range(self.list.count()) if self.list.item(i).data(Qt.ItemDataRole.UserRole) == current), 0)
        if self.list.count():
            self.list.setCurrentRow(row)
        self.list.blockSignals(False)
        if not self.dirty and self.list.currentItem() is not None:
            self._selected(self.list.currentItem())  # a form being edited is never overwritten by a reload
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
        self.action_test.setEnabled(has and not self.dirty)

    def new_procedure(self) -> None:
        if self.dirty and not self._confirm_discard():
            return
        base = ProcedureTemplate(id="nueva_plantilla", nombre="Nueva plantilla de ensayo", algoritmo="vna_2_puertos")
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
        answer = QMessageBox.question(self, "Eliminar plantilla", f"¿Eliminar la plantilla de ensayo «{self.library.procedures[procedure_id].name}»?")
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.library.delete_procedure(procedure_id)
        self._set_dirty(False)
        self.reload()
        self.procedures_changed.emit()
