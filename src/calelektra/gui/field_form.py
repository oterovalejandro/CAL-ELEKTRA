"""Form generated from the typed fields of a category (``campos``).

Each field type gets its own editor (text, number, option list, frequency
range, connector…). An empty editor means «not set»: on an equipment it then
inherits the value of its model, which is shown as placeholder.
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from calelektra.catalog.fields import (
    LEVEL_NAMES,
    TYPE_NAMES,
    FieldError,
    FieldSpec,
    NumberRange,
    ResolvedField,
    coerce,
    to_yaml,
)
from calelektra.core.connectors import FAMILIES, GENDERS, NOT_COAXIAL, SEXLESS, Connector
from calelektra.core.frequency import FrequencyRange
from calelektra.core.units import format_frequency

PLACEHOLDERS = {
    "frecuencia": "p. ej. 18 GHz",
    "fecha": "AAAA-MM-DD",
    "lista": "valores separados por comas",
}


def _number_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return f"{value:g}" if isinstance(value, float) else str(value)


def _frequency_text(value: float | None) -> str:
    if value is None:
        return ""
    return "DC" if value == 0 else format_frequency(value, decimal=".")


class MultiSelect(QToolButton):
    """Several options chosen from a menu; shows the selection as text."""

    changed = Signal()

    def __init__(self, options: list[str], parent: QWidget | None = None):
        super().__init__(parent)
        self.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.setMinimumWidth(220)
        self.setStyleSheet("QToolButton { border: 1px solid #c8d1dc; border-radius: 5px; padding: 4px 8px; background: white; text-align: left; }")
        menu = QMenu(self)
        self.actions_by_option: dict[str, QAction] = {}
        for option in options:
            action = QAction(option, menu)
            action.setCheckable(True)
            action.toggled.connect(self._toggled)
            menu.addAction(action)
            self.actions_by_option[option] = action
        self.setMenu(menu)
        self.placeholder = "—"
        self._update_text()

    def _toggled(self, _checked: bool) -> None:
        self._update_text()
        self.changed.emit()

    def _update_text(self) -> None:
        selected = self.selected()
        self.setText(", ".join(selected) if selected else self.placeholder)

    def selected(self) -> list[str]:
        return [option for option, action in self.actions_by_option.items() if action.isChecked()]

    def set_selected(self, values: list[str] | None) -> None:
        for option, action in self.actions_by_option.items():
            action.blockSignals(True)
            action.setChecked(bool(values) and option in values)
            action.blockSignals(False)
        self._update_text()

    def set_placeholder(self, text: str) -> None:
        self.placeholder = text
        self._update_text()


class FieldEditor(QWidget):
    """Editor of one field; ``raw()`` is ``None`` when the field is left empty."""

    changed = Signal()

    def __init__(self, spec: FieldSpec, inherited: str | None = None, parent: QWidget | None = None):
        super().__init__(parent)
        self.spec = spec
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        kind = spec.type
        hint = f"modelo: {inherited}" if inherited else ""
        self.widgets: list[QWidget] = []
        if kind in ("booleano", "opcion"):
            self.combo = QComboBox()
            self.combo.addItem(f"— ({hint})" if hint else "—", None)
            options = [("Sí", True), ("No", False)] if kind == "booleano" else [(o, o) for o in (spec.options or [])]
            for text, value in options:
                self.combo.addItem(text, value)
            self.combo.currentIndexChanged.connect(lambda _i: self.changed.emit())
            self.widgets.append(self.combo)
        elif kind == "multiopcion" and spec.options:
            self.multi = MultiSelect(spec.options)
            if hint:
                self.multi.set_placeholder(f"— ({hint})")
            self.multi.changed.connect(self.changed.emit)
            self.widgets.append(self.multi)
        elif kind in ("rango_frecuencia", "rango_numero"):
            self.low, self.high = QLineEdit(), QLineEdit()
            for edit, text in ((self.low, "mínimo"), (self.high, "máximo")):
                edit.setPlaceholderText(text)
                edit.textEdited.connect(lambda _t: self.changed.emit())
            if hint:
                self.low.setPlaceholderText(hint)
            self.widgets += [self.low, QLabel("–"), self.high]
        elif kind == "conector":
            self.family = QComboBox()
            self.family.addItem(f"— ({hint})" if hint else "—", None)
            for key, name in FAMILIES.items():
                self.family.addItem(name, key)
            self.gender = QComboBox()
            for key, name in GENDERS.items():
                self.gender.addItem(name, key)
            self.family.currentIndexChanged.connect(self._family_changed)
            self.gender.currentIndexChanged.connect(lambda _i: self.changed.emit())
            self.widgets += [self.family, self.gender]
            self._family_changed()
        elif kind == "familia_conector":
            self.combo = QComboBox()
            self.combo.addItem(f"— ({hint})" if hint else "—", None)
            for key, name in FAMILIES.items():
                self.combo.addItem(name, key)
            self.combo.currentIndexChanged.connect(lambda _i: self.changed.emit())
            self.widgets.append(self.combo)
        else:
            self.edit = QLineEdit()
            placeholder = hint or PLACEHOLDERS.get(kind, "")
            if not hint and spec.unit and kind not in ("numero", "entero"):
                placeholder = spec.unit  # numbers show the unit as a label after the editor
            self.edit.setPlaceholderText(placeholder)
            self.edit.textEdited.connect(lambda _t: self.changed.emit())
            self.widgets.append(self.edit)
        for widget in self.widgets:
            layout.addWidget(widget, 1 if not isinstance(widget, QLabel) else 0)
        if spec.unit and kind in ("numero", "entero", "rango_numero"):
            unit = QLabel(spec.unit)
            unit.setStyleSheet("color: #5b6b80;")
            layout.addWidget(unit)

    def _family_changed(self) -> None:
        family = self.family.currentData()
        sexless = family is None or family in SEXLESS or family in NOT_COAXIAL
        self.gender.setEnabled(not sexless)
        index = self.gender.findData("sin_genero" if sexless and family else "macho")
        if sexless and family:
            self.gender.setCurrentIndex(index)
        elif self.gender.currentData() == "sin_genero":
            self.gender.setCurrentIndex(index)
        self.changed.emit()

    # ── values ─────────────────────────────────────────────────────────────
    def set_value(self, value: Any) -> None:
        """Show a YAML or already coerced value."""
        spec = self.spec
        try:
            value = coerce(spec, value) if value is not None else None
        except FieldError:
            pass  # show it as it is; validation reports it
        kind = spec.type
        if kind in ("booleano", "opcion", "familia_conector"):
            index = self.combo.findData(value) if value is not None else 0
            if index < 0 and value is not None:
                self.combo.addItem(f"{value} (no válido)", value)
                index = self.combo.count() - 1
            self.combo.setCurrentIndex(max(index, 0))
        elif kind == "multiopcion" and spec.options:
            self.multi.set_selected(list(value) if isinstance(value, (list, tuple)) else ([value] if value else []))
        elif kind == "rango_frecuencia":
            if isinstance(value, FrequencyRange):
                self.low.setText(_frequency_text(value.start))
                self.high.setText(_frequency_text(value.stop))
            elif isinstance(value, (list, tuple)) and len(value) == 2:
                self.low.setText(str(value[0]))
                self.high.setText(str(value[1]))
        elif kind == "rango_numero":
            if isinstance(value, NumberRange):
                self.low.setText(_number_text(value.low))
                self.high.setText(_number_text(value.high))
            elif isinstance(value, (list, tuple)) and len(value) == 2:
                self.low.setText(str(value[0]))
                self.high.setText(str(value[1]))
        elif kind == "conector":
            if isinstance(value, Connector):
                self.family.setCurrentIndex(max(self.family.findData(value.family), 0))
                self.gender.setCurrentIndex(max(self.gender.findData(value.gender), 0))
            else:
                self.family.setCurrentIndex(0)
        else:
            if value is None:
                text = ""
            elif kind == "frecuencia":
                text = _frequency_text(float(value))
            elif kind in ("lista", "multiopcion"):
                text = ", ".join(str(v) for v in value) if isinstance(value, (list, tuple)) else str(value)
            elif kind in ("numero", "entero"):
                text = _number_text(value)
            elif kind == "fecha":
                text = value.isoformat() if hasattr(value, "isoformat") else str(value)
            else:
                text = str(value)
            self.edit.setText(text)

    def raw(self) -> Any:
        kind = self.spec.type
        if kind in ("booleano", "opcion", "familia_conector"):
            return self.combo.currentData()
        if kind == "multiopcion" and self.spec.options:
            return self.multi.selected() or None
        if kind in ("rango_frecuencia", "rango_numero"):
            low, high = self.low.text().strip(), self.high.text().strip()
            if not low and not high:
                return None
            return [low, high]
        if kind == "conector":
            family = self.family.currentData()
            if family is None:
                return None
            return {"familia": family, "genero": self.gender.currentData()}
        text = self.edit.text().strip()
        if not text:
            return None
        if kind in ("lista", "multiopcion"):
            return [item.strip() for item in text.split(",") if item.strip()]
        return text

    def value(self) -> Any:
        """Coerced value (``FieldError`` if it is not valid)."""
        raw = self.raw()
        return None if raw is None else coerce(self.spec, raw)

    def set_error(self, message: str | None) -> None:
        border = "#c0392b" if message else "#c8d1dc"
        for widget in self.widgets:
            if isinstance(widget, (QLineEdit, QComboBox)):
                widget.setStyleSheet(f"border: 1px solid {border};")
            widget.setToolTip(message or "")


def describe(spec: FieldSpec) -> str:
    parts = [f"Tipo: {TYPE_NAMES.get(spec.type, spec.type)}", f"Se define en: {LEVEL_NAMES[spec.level]}"]
    if spec.unit:
        parts.append(f"Unidad: {spec.unit}")
    if spec.options:
        parts.append(f"Opciones: {', '.join(spec.options)}")
    if spec.minimum is not None or spec.maximum is not None:
        parts.append(f"Límites: {spec.minimum if spec.minimum is not None else '−∞'} … {spec.maximum if spec.maximum is not None else '∞'}")
    if spec.description:
        parts.append(spec.description.strip())
    return "\n".join(parts)


class FieldForm(QWidget):
    """All the fields of a category that can be set at one level (``modelo`` or ``equipo``)."""

    changed = Signal()

    def __init__(
        self,
        specs: dict[str, FieldSpec],
        where: str,
        values: dict[str, Any] | None = None,
        inherited: dict[str, ResolvedField] | None = None,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.specs = {key: spec for key, spec in specs.items() if spec.allowed_in(where)}
        self.where = where
        self.editors: dict[str, FieldEditor] = {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        groups: dict[str, QFormLayout] = {}
        for key, spec in self.specs.items():
            if spec.group not in groups:
                box = QGroupBox(spec.group)
                form = QFormLayout(box)
                form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
                groups[spec.group] = form
                layout.addWidget(box)
            inherited_text = None
            if inherited and key in inherited and inherited[key].value is not None and where == "equipo":
                inherited_text = inherited[key].text
            editor = FieldEditor(spec, inherited_text)
            editor.set_value((values or {}).get(key))
            editor.changed.connect(self.changed.emit)
            required = spec.required and (spec.level == where or (spec.level == "ambos" and not inherited_text))
            label = QLabel(spec.label + (" *" if required else ""))
            label.setToolTip(describe(spec))
            editor.setToolTip(describe(spec))
            groups[spec.group].addRow(label, editor)
            self.editors[key] = editor
        if not self.specs:
            empty = QLabel("La categoría no define campos para este nivel.")
            empty.setStyleSheet("color: #5b6b80;")
            layout.addWidget(empty)
        layout.addStretch(1)

    def errors(self) -> list[str]:
        problems = []
        for key, editor in self.editors.items():
            try:
                editor.value()
            except FieldError as exc:
                editor.set_error(str(exc))
                problems.append(f"«{editor.spec.label}»: {exc}")
            else:
                editor.set_error(None)
        return problems

    def values(self) -> dict[str, Any]:
        """YAML-ready values of the fields that are set (raises ``FieldError``)."""
        result: dict[str, Any] = {}
        for key, editor in self.editors.items():
            try:
                value = editor.value()
            except FieldError as exc:
                raise FieldError(f"«{editor.spec.label}»: {exc}") from exc
            if value is not None:
                result[key] = to_yaml(editor.spec, value)
        return result

    def missing_required(self, inherited: dict[str, ResolvedField] | None = None) -> list[str]:
        missing = []
        for key, editor in self.editors.items():
            spec = editor.spec
            if not spec.required or editor.raw() is not None:
                continue
            if inherited and key in inherited and inherited[key].value is not None:
                continue
            missing.append(spec.label)
        return missing
