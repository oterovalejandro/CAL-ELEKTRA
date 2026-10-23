"""Dialogs to create and edit equipment and models; the fields come from the category."""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from calelektra.catalog.fields import FieldError, resolve
from calelektra.catalog.library import Library, LibraryError
from calelektra.catalog.model import (
    STATUS_NAMES,
    Calibration,
    ConnectionSettings,
    Equipment,
    EquipmentModel,
    SParameterData,
)
from calelektra.core.touchstone import TouchstoneError, read_touchstone
from calelektra.gui import style
from calelektra.gui.field_form import FieldForm
from calelektra.gui.template_widgets import SpecEditor
from calelektra.instruments.factory import DRIVERS
from calelektra.uncertainty.engine import UncertaintyCalculationError, UncertaintyEngine

ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]*$")


def _parse_date(text: str, label: str) -> date | None:
    text = text.strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise FieldError(f"«{label}»: fecha no válida «{text}» (use AAAA-MM-DD)") from exc


def _scrolling(widget: QWidget) -> QScrollArea:
    area = QScrollArea()
    area.setWidgetResizable(True)
    area.setWidget(widget)
    return area


class EquipmentDialog(QDialog):
    """New equipment (category → model → own fields) or edition of an existing one."""

    def __init__(
        self,
        library: Library,
        equipment: Equipment | None = None,
        parent: QWidget | None = None,
        copy_of: Equipment | None = None,
        model_id: str | None = None,
    ):
        super().__init__(parent)
        self.library = library
        self.editing = equipment is not None
        self.original = equipment
        source = equipment or copy_of
        self.saved_id: str | None = None
        self.setWindowTitle(f"Editar equipo «{equipment.id}»" if equipment else "Nuevo equipo")
        self.resize(760, 720)
        layout = QVBoxLayout(self)
        header = style.PageHeader(
            self.windowTitle(),
            "Los campos dependen de la categoría. Vacío = se hereda del modelo (se muestra en gris).",
        )
        layout.addWidget(header)

        identity = style.Card("Identificación", icon_name="equipos")
        form = QFormLayout()
        identity.body.addLayout(form)
        self.category = QComboBox()
        for category in sorted(library.categories.values(), key=lambda c: c.name):
            self.category.addItem(category.name, category.id)
        self.model = QComboBox()
        self.id_edit = QLineEdit()
        self.id_edit.setPlaceholderText("p. ej. CAB-DUT-0004")
        self.serial = QLineEdit()
        self.status = QComboBox()
        for key, name in STATUS_NAMES.items():
            self.status.addItem(name, key)
        self.location = QLineEdit()
        self.description = QLineEdit()
        form.addRow("Categoría", self.category)
        form.addRow("Modelo", self.model)
        form.addRow("Identificador *", self.id_edit)
        form.addRow("Nº de serie", self.serial)
        form.addRow("Estado", self.status)
        form.addRow("Ubicación", self.location)
        form.addRow("Descripción", self.description)
        layout.addWidget(identity)

        self.tabs = QTabWidget()
        self.fields_holder = QWidget()
        self.fields_layout = QVBoxLayout(self.fields_holder)
        self.fields_layout.setContentsMargins(4, 4, 4, 4)
        self.tabs.addTab(_scrolling(self.fields_holder), "Campos")

        calibration = QWidget()
        cal_form = QFormLayout(calibration)
        self.certificate = QLineEdit()
        self.laboratory = QLineEdit()
        self.issued = QLineEdit()
        self.issued.setPlaceholderText("AAAA-MM-DD")
        self.due = QLineEdit()
        self.due.setPlaceholderText("AAAA-MM-DD")
        cal_form.addRow("Certificado", self.certificate)
        cal_form.addRow("Laboratorio", self.laboratory)
        cal_form.addRow("Fecha de calibración", self.issued)
        cal_form.addRow("Próxima calibración", self.due)
        self.uncertainty_file = QLineEdit()
        self.uncertainty_file.setPlaceholderText("Fichero YAML de incertidumbres de la unidad (datasheet y certificado)")
        browse_uncertainty = QPushButton("Examinar…")
        browse_uncertainty.clicked.connect(self._browse_uncertainty_file)
        self.uncertainty_row = QWidget()
        uncertainty_row = QHBoxLayout(self.uncertainty_row)
        uncertainty_row.setContentsMargins(0, 0, 0, 0)
        uncertainty_row.addWidget(self.uncertainty_file, 1)
        uncertainty_row.addWidget(browse_uncertainty)
        self.uncertainty_info = QLabel()
        self.uncertainty_info.setWordWrap(True)
        self.uncertainty_info.setStyleSheet("color: #5b6b80;")
        cal_form.addRow("Incertidumbres", self.uncertainty_row)
        cal_form.addRow(self.uncertainty_info)
        self.calibration_form = cal_form
        self.tabs.addTab(calibration, "Calibración")

        s_data = QWidget()
        s_form = QFormLayout(s_data)
        self.s_file = QLineEdit()
        self.s_file.setPlaceholderText("Fichero .sNp con los parámetros S calibrados")
        browse = QPushButton("Examinar…")
        browse.clicked.connect(self._browse_s_file)
        row = QHBoxLayout()
        row.addWidget(self.s_file, 1)
        row.addWidget(browse)
        self.s_reference = QLineEdit()
        self.s_reference.setPlaceholderText("Certificado o medida de la que proceden los datos")
        self.s_date = QLineEdit()
        self.s_date.setPlaceholderText("AAAA-MM-DD")
        self.s_info = QLabel()
        self.s_info.setStyleSheet("color: #5b6b80;")
        s_form.addRow("Fichero", row)
        s_form.addRow("Referencia", self.s_reference)
        s_form.addRow("Fecha de los datos", self.s_date)
        s_form.addRow(self.s_info)
        self.s_tab_index = self.tabs.addTab(s_data, "Datos S")

        connection = QWidget()
        con_form = QFormLayout(connection)
        self.simulated = QCheckBox("Usar el instrumento simulado (sin hardware)")
        self.resource = QLineEdit()
        self.resource.setPlaceholderText("TCPIP::192.168.0.50::hislip0::INSTR")
        self.timeout = QSpinBox()
        self.timeout.setRange(0, 600_000)
        self.timeout.setSingleStep(1000)
        self.timeout.setSuffix(" ms")
        self.timeout.setSpecialValueText("por defecto")
        self.driver_label = QLabel()
        con_form.addRow(self.simulated)
        con_form.addRow("Recurso VISA", self.resource)
        con_form.addRow("Tiempo de espera", self.timeout)
        con_form.addRow("Driver del modelo", self.driver_label)
        self.connection_tab_index = self.tabs.addTab(connection, "Conexión")

        self.notes = QPlainTextEdit()
        self.tabs.addTab(self.notes, "Notas")
        layout.addWidget(self.tabs, 1)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("Cancelar")
        cancel.clicked.connect(self.reject)
        save = QPushButton("Guardar")
        save.setObjectName("primary")
        save.setDefault(True)
        save.clicked.connect(self._save)
        buttons.addWidget(cancel)
        buttons.addWidget(save)
        layout.addLayout(buttons)

        self.form: FieldForm | None = None
        self.category.currentIndexChanged.connect(self._category_changed)
        self.model.currentIndexChanged.connect(self._model_changed)

        if source is not None:
            model = library.model_of(source)
            if model:
                self.category.setCurrentIndex(max(self.category.findData(model.category), 0))
        elif model_id and model_id in library.models:
            self.category.setCurrentIndex(max(self.category.findData(library.models[model_id].category), 0))
        self._category_changed()
        if source is not None:
            self.model.setCurrentIndex(max(self.model.findData(source.model_id), 0))
            self._load(source, copy=not self.editing)
        elif model_id:
            self.model.setCurrentIndex(max(self.model.findData(model_id), 0))
        if self.editing:
            self.category.setEnabled(False)
            self.id_edit.setReadOnly(True)

    # ── state ──────────────────────────────────────────────────────────────
    def _category_changed(self) -> None:
        category_id = self.category.currentData()
        self.model.blockSignals(True)
        self.model.clear()
        for model in sorted(self.library.models.values(), key=lambda m: m.display_name):
            if model.category == category_id:
                self.model.addItem(model.display_name, model.id)
        self.model.blockSignals(False)
        category = self.library.categories.get(category_id)
        self.tabs.setTabVisible(self.connection_tab_index, bool(category and category.has_connection))
        is_vna = category_id == "vna"  # the uncertainty engine is for VNA S-parameters
        self.calibration_form.setRowVisible(self.uncertainty_row, is_vna)
        self.calibration_form.setRowVisible(self.uncertainty_info, is_vna)
        self._model_changed()

    def _model_changed(self) -> None:
        current = {}  # what the user already typed survives a change of model
        if self.form is not None:
            for key, editor in self.form.editors.items():
                raw = editor.raw()
                if raw is not None:
                    current[key] = raw
        self._build_fields(current)
        model = self.library.models.get(self.model.currentData())
        self.driver_label.setText(DRIVERS.get(model.driver, model.driver or "—") if model else "—")

    def _build_fields(self, values: dict) -> None:
        if self.form is not None:
            self.form.setParent(None)
            self.form.deleteLater()
            self.form = None
        category = self.library.categories.get(self.category.currentData())
        model = self.library.models.get(self.model.currentData())
        if category is None:
            return
        inherited = resolve(category.fields, model.values if model else {})
        self.form = FieldForm(category.fields, "equipo", values, inherited)
        self.fields_layout.addWidget(self.form)

    def _load(self, equipment: Equipment, copy: bool) -> None:
        self.id_edit.setText("" if copy else equipment.id)
        self.serial.setText("" if copy else (equipment.serial_number or ""))
        self.status.setCurrentIndex(max(self.status.findData(equipment.status), 0))
        self.location.setText(equipment.location or "")
        self.description.setText(equipment.description or "")
        self.notes.setPlainText(equipment.notes or "")
        self._build_fields(dict(equipment.values))
        if equipment.calibration and not copy:
            c = equipment.calibration
            self.certificate.setText(c.certificate or "")
            self.laboratory.setText(c.laboratory or "")
            self.issued.setText(c.issued.isoformat() if c.issued else "")
            self.due.setText(c.due.isoformat() if c.due else "")
        if equipment.uncertainty_file and not copy:
            self.uncertainty_file.setText(equipment.uncertainty_file)
            self._describe_uncertainty(equipment)
        if equipment.s_parameters and not copy:
            s = equipment.s_parameters
            self.s_file.setText(s.file)
            self.s_reference.setText(s.reference)
            self.s_date.setText(s.measured_on.isoformat() if s.measured_on else "")
        if equipment.connection:
            self.simulated.setChecked(equipment.connection.simulated)
            self.resource.setText(equipment.connection.resource or "")
            self.timeout.setValue(equipment.connection.timeout_ms or 0)

    def _describe_uncertainty(self, equipment: Equipment) -> None:
        try:
            engine = self.library.uncertainty_engine(equipment)
        except UncertaintyCalculationError as exc:
            self.uncertainty_info.setText(f"✖ {exc}")
            return
        due, valid, _warnings = engine.calibration_status()
        state = "" if valid is None else (" · calibración vigente" if valid else " · CALIBRACIÓN VENCIDA")
        serial = f" · nº {engine.serial_number}" if engine.serial_number else ""
        self.uncertainty_info.setText(
            f"{engine.document} · {engine.equipment_manufacturer} {engine.equipment_model}{serial} · "
            f"{len(engine.data.get('uncertainty_sources', []))} fuentes · vence {due or '—'}{state}"
        )

    def _browse_uncertainty_file(self) -> None:
        filename, _ = QFileDialog.getOpenFileName(self, "Fichero de incertidumbres del VNA", str(self.library.root), "YAML (*.yaml *.yml)")
        if not filename:
            return
        try:
            UncertaintyEngine(filename)
        except (UncertaintyCalculationError, OSError, ValueError) as exc:
            QMessageBox.warning(self, "Incertidumbres", f"El fichero no es válido: {exc}")
            return
        self.uncertainty_file.setText(self.library.copy_uncertainty_file(Path(filename)))
        probe = Equipment(id="_", model_id=self.model.currentData() or "", uncertainty_file=self.uncertainty_file.text())
        probe.set_source_path(self.original.source_path if self.original else None)
        self._describe_uncertainty(probe)

    def _browse_s_file(self) -> None:
        filename, _ = QFileDialog.getOpenFileName(
            self, "Fichero de parámetros S", str(self.library.root), "Touchstone (*.s1p *.s2p *.s3p *.s4p *.S1P *.S2P)"
        )
        if not filename:
            return
        try:
            data = read_touchstone(filename)
        except TouchstoneError as exc:
            QMessageBox.warning(self, "Datos S", f"El fichero no es válido: {exc}")
            return
        self.s_file.setText(self.library.copy_s_parameter_file(Path(filename)))
        self.s_info.setText(f"{data.n_ports} puertos · {len(data.frequencies)} puntos · {data.frequency_range}")
        if not self.s_date.text():
            self.s_date.setText(date.today().isoformat())

    # ── saving ─────────────────────────────────────────────────────────────
    def build(self) -> Equipment:
        equipment_id = self.id_edit.text().strip()
        if not equipment_id or not ID_PATTERN.match(equipment_id):
            raise FieldError("Indique un identificador (letras, números, «-», «_» o «.»)")
        if not self.editing and equipment_id in self.library.equipment:
            raise FieldError(f"Ya existe un equipo «{equipment_id}»")
        model_id = self.model.currentData()
        if model_id is None:
            raise FieldError("Elija un modelo (si no existe, créelo antes en el Catálogo)")
        assert self.form is not None
        errors = self.form.errors()
        if errors:
            raise FieldError("Campos no válidos:\n" + "\n".join(errors))
        category = self.library.categories[self.category.currentData()]
        model = self.library.models[model_id]
        inherited = resolve(category.fields, model.values)
        missing = self.form.missing_required(inherited)
        if missing:
            raise FieldError("Faltan campos obligatorios: " + ", ".join(missing))
        base = self.original.values if self.original else {}
        values = {k: v for k, v in base.items() if k not in category.fields}  # keep unknown keys
        values.update(self.form.values())
        calibration = None
        if any(edit.text().strip() for edit in (self.certificate, self.laboratory, self.issued, self.due)):
            calibration = Calibration(
                certificate=self.certificate.text().strip() or None,
                laboratory=self.laboratory.text().strip() or None,
                issued=_parse_date(self.issued.text(), "Fecha de calibración"),
                due=_parse_date(self.due.text(), "Próxima calibración"),
            )
        s_parameters = None
        if self.s_file.text().strip():
            s_parameters = SParameterData(
                file=self.s_file.text().strip(),
                reference=self.s_reference.text().strip(),
                measured_on=_parse_date(self.s_date.text(), "Fecha de los datos"),
            )
        elif category.requires_s_parameters:
            raise FieldError(f"La categoría «{category.name}» necesita su fichero de parámetros S (pestaña Datos S)")
        connection = None
        if category.has_connection:
            connection = ConnectionSettings(
                simulated=self.simulated.isChecked(),
                resource=self.resource.text().strip() or None,
                timeout_ms=self.timeout.value() or None,
            )
        equipment = Equipment(
            id=equipment_id,
            model_id=model_id,
            serial_number=self.serial.text().strip() or None,
            status=self.status.currentData(),
            description=self.description.text().strip() or None,
            values=values,
            connection=connection,
            calibration=calibration,
            s_parameters=s_parameters,
            uncertainty_file=(self.uncertainty_file.text().strip() or None) if category.id == "vna" else None,
            location=self.location.text().strip() or None,
            notes=self.notes.toPlainText().strip() or None,
        )
        equipment.set_source_path(self.original.source_path if self.original else None)
        return equipment

    def _save(self) -> None:
        try:
            equipment = self.build()
            self.library.save_equipment(equipment)
        except (FieldError, LibraryError, ValueError, OSError) as exc:
            QMessageBox.warning(self, self.windowTitle(), str(exc))
            return
        self.saved_id = equipment.id
        self.accept()


class ModelDialog(QDialog):
    """New model of a category or edition of an existing one (model-level fields)."""

    def __init__(self, library: Library, model: EquipmentModel | None = None, category_id: str | None = None, parent: QWidget | None = None):
        super().__init__(parent)
        self.library = library
        self.original = model
        self.saved_id: str | None = None
        self.setWindowTitle(f"Editar modelo «{model.display_name}»" if model else "Nuevo modelo")
        self.resize(860, 820)
        layout = QVBoxLayout(self)
        layout.addWidget(
            style.PageHeader(
                self.windowTitle(),
                "Valores comunes a todas las unidades del modelo y sus especificaciones S (pasa / falla al calibrarlas).",
            )
        )
        identity = style.Card("Identificación", icon_name="catalogo")
        form = QFormLayout()
        identity.body.addLayout(form)
        self.category = QComboBox()
        for category in sorted(library.categories.values(), key=lambda c: c.name):
            self.category.addItem(category.name, category.id)
        self.id_edit = QLineEdit()
        self.id_edit.setPlaceholderText("p. ej. rs_znb20")
        self.manufacturer = QLineEdit()
        self.designation = QLineEdit()
        self.variant = QLineEdit()
        self.status = QLineEdit()
        self.status.setPlaceholderText("p. ej. PENDIENTE de verificar con el datasheet")
        self.description = QLineEdit()
        self.driver = QComboBox()
        self.driver.setEditable(True)
        self.driver.addItem("", None)
        for key, text in DRIVERS.items():
            self.driver.addItem(f"{key} — {text}", key)
        form.addRow("Categoría", self.category)
        form.addRow("Identificador *", self.id_edit)
        form.addRow("Fabricante *", self.manufacturer)
        form.addRow("Modelo *", self.designation)
        form.addRow("Variante", self.variant)
        form.addRow("Estado", self.status)
        form.addRow("Descripción", self.description)
        self.driver_row_label = QLabel("Driver")
        form.addRow(self.driver_row_label, self.driver)
        layout.addWidget(identity)
        self.fields_holder = QWidget()
        self.fields_layout = QVBoxLayout(self.fields_holder)
        self.specs_card = style.Card("Especificaciones S (pasa / falla)", icon_name="veredicto")
        specs_note = QLabel(
            "Ficheros S2P/S1P con los valores de especificación del modelo. Al calibrar una unidad de este modelo "
            "(EUT de una plantilla de ensayo), la medida se compara con ellos. Se copian en «catalogo/especificaciones»."
        )
        specs_note.setObjectName("pageSubtitle")
        specs_note.setWordWrap(True)
        self.specs = SpecEditor(resolver=self._spec_path, importer=library.copy_spec_file)
        self.specs.setMinimumHeight(170)
        self.specs_card.body.addWidget(specs_note)
        self.specs_card.body.addWidget(self.specs)
        self.fields_layout.addWidget(self.specs_card)
        layout.addWidget(_scrolling(self.fields_holder), 1)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("Cancelar")
        cancel.clicked.connect(self.reject)
        save = QPushButton("Guardar")
        save.setObjectName("primary")
        save.clicked.connect(self._save)
        buttons.addWidget(cancel)
        buttons.addWidget(save)
        layout.addLayout(buttons)
        self.form: FieldForm | None = None
        self.category.currentIndexChanged.connect(self._category_changed)
        initial = model.category if model else category_id
        if initial:
            self.category.setCurrentIndex(max(self.category.findData(initial), 0))
        self._category_changed()
        if model is not None:
            self.category.setEnabled(False)
            self.id_edit.setText(model.id)
            self.id_edit.setReadOnly(True)
            self.manufacturer.setText(model.manufacturer)
            self.designation.setText(model.designation)
            self.variant.setText(model.variant or "")
            self.status.setText(model.status or "")
            self.description.setText(model.description or "")
            if model.driver:
                index = self.driver.findData(model.driver)
                if index >= 0:
                    self.driver.setCurrentIndex(index)
                else:
                    self.driver.setEditText(model.driver)
            self._build_fields(dict(model.values))
            self.specs.load(model.s_specifications)

    def _spec_path(self, reference: str) -> Path:
        path = Path(reference)
        if path.is_absolute():
            return path
        base = self.original.source_path.parent if self.original and self.original.source_path else self.library.root / "catalogo" / "modelos"
        return (base / path).resolve()

    def _category_changed(self) -> None:
        category = self.library.categories.get(self.category.currentData())
        visible = bool(category and category.has_connection)
        self.driver.setVisible(visible)
        self.driver_row_label.setVisible(visible)
        ports = len(category.ports) if category is not None and isinstance(category.ports, list) else 2
        self.specs.set_traces(("S11", "S21", "S12", "S22") if ports >= 2 else ("S11",))
        self._build_fields({})

    def _build_fields(self, values: dict) -> None:
        if self.form is not None:
            self.form.setParent(None)
            self.form.deleteLater()
        category = self.library.categories.get(self.category.currentData())
        self.form = FieldForm(category.fields if category else {}, "modelo", values)
        self.fields_layout.insertWidget(0, self.form)

    def build(self) -> EquipmentModel:
        model_id = self.id_edit.text().strip()
        if not model_id or not ID_PATTERN.match(model_id):
            raise FieldError("Indique un identificador (letras, números, «-», «_» o «.»)")
        if self.original is None and model_id in self.library.models:
            raise FieldError(f"Ya existe un modelo «{model_id}»")
        if not self.manufacturer.text().strip() or not self.designation.text().strip():
            raise FieldError("Indique fabricante y modelo")
        assert self.form is not None
        errors = self.form.errors()
        if errors:
            raise FieldError("Campos no válidos:\n" + "\n".join(errors))
        missing = [
            editor.spec.label
            for editor in self.form.editors.values()
            if editor.spec.required and editor.spec.level == "modelo" and editor.raw() is None
        ]
        if missing:
            raise FieldError("Faltan campos obligatorios: " + ", ".join(missing))
        category_id = self.category.currentData()
        category = self.library.categories[category_id]
        base = self.original.values if self.original else {}
        values = {k: v for k, v in base.items() if k not in category.fields}
        values.update(self.form.values())
        communication = dict(self.original.communication or {}) if self.original else {}
        driver = self.driver.currentData() if self.driver.currentIndex() > 0 else (self.driver.currentText().split(" — ")[0].strip() or None)
        if category.has_connection:
            if driver:
                communication["driver"] = driver
            else:
                communication.pop("driver", None)
        try:
            s_specifications = self.specs.specs()
        except ValueError as exc:
            raise FieldError(f"Especificaciones S: {exc}") from exc
        update = dict(
            id=model_id,
            category=category_id,
            manufacturer=self.manufacturer.text().strip(),
            designation=self.designation.text().strip(),
            variant=self.variant.text().strip() or None,
            status=self.status.text().strip() or None,
            description=self.description.text().strip() or None,
            values=values,
            communication=communication or None,
            s_specifications=s_specifications,
        )
        if self.original is not None:
            model = self.original.model_copy(update=update)
            model.set_source_path(self.original.source_path)
        else:
            model = EquipmentModel(**update)
        return model

    def _save(self) -> None:
        try:
            model = self.build()
            self.library.save_model(model)
        except (FieldError, LibraryError, ValueError, OSError) as exc:
            QMessageBox.warning(self, self.windowTitle(), str(exc))
            return
        self.saved_id = model.id
        self.accept()


def confirm_delete_equipment(library: Library, equipment_id: str, parent: QWidget) -> bool:
    users = library.references_to(equipment_id)
    if users:
        QMessageBox.warning(
            parent,
            "Eliminar equipo",
            f"«{equipment_id}» se usa en los setups: {', '.join(users)}.\nQuítelo de esos setups antes de eliminarlo.",
        )
        return False
    templates = library.templates_using(equipment_id=equipment_id)
    if templates:
        QMessageBox.warning(
            parent,
            "Eliminar equipo",
            f"«{equipment_id}» es el EUT de las plantillas de ensayo: {', '.join(templates)}.\nCámbielas antes de eliminarlo.",
        )
        return False
    answer = QMessageBox.question(
        parent,
        "Eliminar equipo",
        f"¿Eliminar «{equipment_id}» del inventario? Se borrará su fichero YAML (los datos S se conservan).",
    )
    if answer != QMessageBox.StandardButton.Yes:
        return False
    try:
        library.delete_equipment(equipment_id)
    except (LibraryError, OSError) as exc:
        QMessageBox.warning(parent, "Eliminar equipo", str(exc))
        return False
    return True

