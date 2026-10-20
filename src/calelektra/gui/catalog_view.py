"""Catalogue: equipment categories (their typed fields) and the models of each category."""

from __future__ import annotations

import html

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QLabel,
    QScrollArea,
    QSplitter,
    QTableWidgetItem,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from calelektra.catalog.fields import LEVEL_NAMES, TYPE_NAMES, resolve
from calelektra.catalog.library import Library
from calelektra.gui import style
from calelektra.gui.equipment_dialogs import ModelDialog
from calelektra.gui.icons import icon
from calelektra.instruments.factory import DRIVERS

KIND_NAMES = {"instrumento": "Instrumento", "pasivo": "Pasivo", "posicionador": "Posicionador", "recinto": "Recinto"}
KIND_ICONS = {"instrumento": "vna", "pasivo": "cable", "posicionador": "setups", "recinto": "inicio"}


class CatalogView(QWidget):
    catalog_changed = Signal()
    new_equipment_requested = Signal(str)  # model id
    title = "Catálogo"

    def __init__(self, library: Library, parent: QWidget | None = None):
        super().__init__(parent)
        self.library = library
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 10)
        layout.addWidget(
            style.PageHeader(
                "Catálogo de equipos",
                "Las categorías definen los campos de cada tipo de equipo (con su tipo, unidad y nivel); "
                "los modelos dan los valores comunes y cada equipo los suyos.",
            )
        )
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Categorías y modelos", "Equipos"])
        self.tree.setMinimumWidth(380)
        self.tree.currentItemChanged.connect(self._selected)

        self.detail = QWidget()
        detail_layout = QVBoxLayout(self.detail)
        detail_layout.setContentsMargins(4, 0, 4, 4)
        self.info_card = style.Card()
        self.info_title = QLabel()
        self.info_title.setObjectName("pageTitle")
        self.info_text = QLabel()
        self.info_text.setWordWrap(True)
        self.info_text.setTextFormat(Qt.TextFormat.RichText)
        self.info_card.body.addWidget(self.info_title)
        self.info_card.body.addWidget(self.info_text)
        self.fields_card = style.Card("Campos", icon_name="ajustes")
        self.fields = style.make_table(["Campo", "Etiqueta", "Tipo", "Se define en", "Oblig.", "Unidad / opciones", "Grupo"], stretch_column=5)
        self.fields.setMinimumHeight(320)
        self.fields_card.body.addWidget(self.fields)
        self.extra_card = style.Card("Más información", icon_name="info")
        self.extra_text = QLabel()
        self.extra_text.setWordWrap(True)
        self.extra_text.setTextFormat(Qt.TextFormat.RichText)
        self.extra_card.body.addWidget(self.extra_text)
        detail_layout.addWidget(self.info_card)
        detail_layout.addWidget(self.fields_card, 1)
        detail_layout.addWidget(self.extra_card)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.detail)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self.tree)
        splitter.addWidget(scroll)
        splitter.setStretchFactor(1, 3)
        layout.addWidget(splitter, 1)

        self.action_new_model = QAction(icon("nuevo", style.PRIMARY.name()), "Nuevo\nmodelo", self)
        self.action_edit_model = QAction(icon("editar", style.PRIMARY.name()), "Editar\nmodelo", self)
        self.action_new_equipment = QAction(icon("equipos", style.PRIMARY.name()), "Nuevo equipo\nde este modelo", self)
        self.action_new_model.triggered.connect(self.new_model)
        self.action_edit_model.triggered.connect(self.edit_model)
        self.action_new_equipment.triggered.connect(self._new_equipment)
        self.reload()

    def ribbon_actions(self) -> list[QAction | None]:
        return [self.action_new_model, self.action_edit_model, None, self.action_new_equipment]

    def set_library(self, library: Library) -> None:
        self.library = library
        self.reload()

    def reload(self, select: tuple[str, str] | None = None) -> None:
        current = select or self._current()
        self.tree.clear()
        counts: dict[str, int] = {}
        for equipment in self.library.equipment.values():
            counts[equipment.model_id] = counts.get(equipment.model_id, 0) + 1
        for category in sorted(self.library.categories.values(), key=lambda c: c.name):
            color = style.KIND_COLORS.get(category.kind, style.ACCENT).name()
            models = sorted((m for m in self.library.models.values() if m.category == category.id), key=lambda m: m.display_name)
            item = QTreeWidgetItem([category.name, str(sum(counts.get(m.id, 0) for m in models))])
            item.setIcon(0, icon(KIND_ICONS.get(category.kind, "info"), color, 18))
            item.setData(0, Qt.ItemDataRole.UserRole, ("categoria", category.id))
            font = item.font(0)
            font.setBold(True)
            item.setFont(0, font)
            self.tree.addTopLevelItem(item)
            for model in models:
                child = QTreeWidgetItem([model.display_name, str(counts.get(model.id, 0))])
                child.setData(0, Qt.ItemDataRole.UserRole, ("modelo", model.id))
                if model.status:
                    child.setToolTip(0, model.status)
                item.addChild(child)
        self.tree.expandAll()
        self.tree.resizeColumnToContents(0)
        if current:
            self._select(current)
        if self.tree.currentItem() is None and self.tree.topLevelItemCount():
            self.tree.setCurrentItem(self.tree.topLevelItem(0))

    def _current(self) -> tuple[str, str] | None:
        item = self.tree.currentItem()
        return item.data(0, Qt.ItemDataRole.UserRole) if item else None

    def _select(self, key: tuple[str, str]) -> None:
        for i in range(self.tree.topLevelItemCount()):
            top = self.tree.topLevelItem(i)
            for item in [top] + [top.child(j) for j in range(top.childCount())]:
                if tuple(item.data(0, Qt.ItemDataRole.UserRole)) == tuple(key):
                    self.tree.setCurrentItem(item)
                    return

    def _selected(self, item: QTreeWidgetItem | None) -> None:
        if item is None:
            return
        kind, key = item.data(0, Qt.ItemDataRole.UserRole)
        if kind == "categoria":
            self._show_category(key)
        else:
            self._show_model(key)
        self.action_edit_model.setEnabled(kind == "modelo")
        self.action_new_equipment.setEnabled(kind == "modelo")

    def _fill_fields(self, rows: list[list[str]]) -> None:
        self.fields.setRowCount(0)
        for values in rows:
            row = self.fields.rowCount()
            self.fields.insertRow(row)
            for column, value in enumerate(values):
                self.fields.setItem(row, column, QTableWidgetItem(value))
        self.fields.resizeColumnsToContents()
        for column in range(self.fields.columnCount() - 2):
            self.fields.setColumnWidth(column, min(self.fields.columnWidth(column) + 10, 230))

    def _show_category(self, category_id: str) -> None:
        category = self.library.categories[category_id]
        self.info_title.setText(category.name)
        flags = [KIND_NAMES.get(category.kind, category.kind)]
        if category.rf:
            flags.append("participa en el rango del setup")
        if category.requires_calibration:
            flags.append("requiere calibración")
        if category.requires_s_parameters:
            flags.append(f"requiere datos S ({category.s_parameter_ports or '?'} puertos)")
        ports = category.ports
        if isinstance(ports, list):
            ports_text = ", ".join(f"{p.name}" + (f" (conector: {p.connector_field})" if p.connector_field else "") for p in ports) or "sin puertos"
        else:
            ports_text = f"{ports.template} × campo «{ports.count_field}» (conector: {ports.connector_field})"
        description = f"{html.escape(category.description)}<br>" if category.description else ""
        self.info_text.setText(
            f"{description}<b>Clase:</b> {' · '.join(flags)}<br><b>Puertos:</b> {html.escape(ports_text)}"
            f"<br><small>{html.escape(str(category.source_path or ''))}</small>"
        )
        self.fields_card.set_title(f"Campos de la categoría ({len(category.fields)})")
        self.fields.setHorizontalHeaderLabels(["Campo", "Etiqueta", "Tipo", "Se define en", "Oblig.", "Unidad / opciones", "Grupo"])
        rows = []
        for key, spec in category.fields.items():
            extra = spec.unit or ""
            if spec.options:
                extra = (extra + " · " if extra else "") + ", ".join(spec.options)
            if spec.default is not None:
                extra = (extra + " · " if extra else "") + f"por defecto: {spec.default}"
            rows.append([key, spec.label, TYPE_NAMES.get(spec.type, spec.type), LEVEL_NAMES[spec.level], "sí" if spec.required else "", extra, spec.group])
        self._fill_fields(rows)
        operations = category.operations if isinstance(category.operations, list) else []
        models = [m for m in self.library.models.values() if m.category == category_id]
        text = f"<b>Modelos:</b> {len(models)}"
        if operations:
            text += f"<br><b>Operaciones de alto nivel:</b> {', '.join(map(str, operations))}"
        if category.specifications:
            text += f"<br><b>Especificaciones (vocabulario):</b> {', '.join(category.specifications)}"
        self.extra_text.setText(text)

    def _show_model(self, model_id: str) -> None:
        model = self.library.models[model_id]
        category = self.library.categories.get(model.category)
        self.info_title.setText(model.display_name)
        lines = [f"<b>Categoría:</b> {category.name if category else model.category} · <b>ID:</b> {model.id}"]
        if model.status:
            lines.append(f"<b>Estado:</b> <span style='color:#d98a00'>{html.escape(model.status)}</span>")
        if model.description:
            lines.append(html.escape(model.description))
        if category and category.has_connection:
            lines.append(f"<b>Driver:</b> {html.escape(DRIVERS.get(model.driver or '', model.driver or '—'))}")
        lines.append(f"<small>{html.escape(str(model.source_path or ''))}</small>")
        self.info_text.setText("<br>".join(lines))
        self.fields_card.set_title("Valores del modelo")
        self.fields.setHorizontalHeaderLabels(["Campo", "Etiqueta", "Valor", "Origen", "Oblig.", "Unidad / opciones", "Grupo"])
        rows = []
        if category:
            for key, resolved in resolve(category.fields, model.values).items():
                spec = resolved.spec
                origin = {"modelo": "modelo", "defecto": "por defecto"}.get(resolved.source or "", "—" if spec.level == "equipo" else "")
                if spec.level == "equipo":
                    origin = "cada equipo"
                rows.append([key, spec.label, resolved.text if resolved.value is not None else "", origin, "sí" if spec.required else "", spec.unit or "", spec.group])
        self._fill_fields(rows)
        units = [e.id for e in self.library.equipment.values() if e.model_id == model_id]
        extra = f"<b>Equipos de este modelo:</b> {', '.join(units) if units else 'ninguno'}"
        if model.documents:
            extra += "<br><b>Documentos:</b> " + "; ".join(html.escape(str(d.get('referencia', d)) if isinstance(d, dict) else str(d)) for d in model.documents)
        if model.specifications:
            extra += "<br><b>Especificaciones:</b> " + "; ".join(html.escape(str(s.get('fichero', s)) if isinstance(s, dict) else str(s)) for s in model.specifications)
        extra_values = {k: v for k, v in model.values.items() if not category or k not in category.fields}
        if extra_values:
            extra += "<br><b>Valores no definidos en la categoría:</b> " + html.escape(", ".join(f"{k}={v}" for k, v in extra_values.items()))
        self.extra_text.setText(extra)

    # ── actions ────────────────────────────────────────────────────────────
    def new_model(self) -> None:
        current = self._current()
        category_id = None
        if current:
            category_id = current[1] if current[0] == "categoria" else self.library.models[current[1]].category
        dialog = ModelDialog(self.library, category_id=category_id, parent=self)
        if dialog.exec() and dialog.saved_id:
            self._changed(("modelo", dialog.saved_id))

    def edit_model(self) -> None:
        current = self._current()
        if not current or current[0] != "modelo":
            return
        dialog = ModelDialog(self.library, self.library.models[current[1]], parent=self)
        if dialog.exec() and dialog.saved_id:
            self._changed(("modelo", dialog.saved_id))

    def _new_equipment(self) -> None:
        current = self._current()
        if current and current[0] == "modelo":
            self.new_equipment_requested.emit(current[1])

    def _changed(self, select: tuple[str, str]) -> None:
        self.library.reload()
        self.reload(select)
        self.catalog_changed.emit()

