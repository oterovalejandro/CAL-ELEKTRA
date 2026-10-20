"""Test explorer (ELEKTRA-like): test folders, the tests of the selected folder and the detail of a test.

Each test lives in its own folder (``<carpeta>/<ensayo>/ensayo.yaml``) with
its definition, the specification files it was defined with and, once run,
the result files. A prepared test is opened in a test window to run it.
"""

from __future__ import annotations

import html
from pathlib import Path

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QAction, QDesktopServices
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSplitter,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from calelektra.catalog.library import Library, LibraryError
from calelektra.gui import style
from calelektra.gui.icons import icon
from calelektra.gui.plots import SParameterPlot
from calelektra.gui.test_folders import TestFolderTree
from calelektra.gui.test_window import is_logarithmic, record_datasets, record_spec_curves, verdict_chip
from calelektra.procedures.record import STATUS_NAMES, TestRecord
from calelektra.procedures.specs import VERDICTS

COLUMNS = ["Ensayo", "Carpeta", "Fecha", "Equipo calibrado", "Plantilla", "Setup", "Estado", "Veredicto"]


def record_key(record: TestRecord) -> str:
    """Tests are identified by their folder (ids can repeat in different folders)."""
    return str(record.folder)


class TestDetail(QWidget):
    __test__ = False  # not a pytest test class

    def __init__(self, library: Library, parent: QWidget | None = None):
        super().__init__(parent)
        self.library = library
        self.record: TestRecord | None = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 0, 4, 4)
        self.header = style.Card()
        top = QHBoxLayout()
        self.title = QLabel("Seleccione un ensayo")
        self.title.setObjectName("pageTitle")
        self.title.setWordWrap(True)
        self.chips = QHBoxLayout()
        top.addWidget(self.title, 1)
        top.addLayout(self.chips)
        self.subtitle = QLabel()
        self.subtitle.setObjectName("pageSubtitle")
        self.subtitle.setWordWrap(True)
        self.subtitle.setTextFormat(Qt.TextFormat.RichText)
        self.header.body.addLayout(top)
        self.header.body.addWidget(self.subtitle)
        self.plot_card = style.Card("Resultado", icon_name="grafica")
        self.plot = SParameterPlot()
        self.plot.setMinimumHeight(300)
        self.plot_card.body.addWidget(self.plot)
        self.specs_card = style.Card("Pasa / falla", icon_name="veredicto")
        self.specs = QLabel()
        self.specs.setWordWrap(True)
        self.specs.setTextFormat(Qt.TextFormat.RichText)
        self.specs.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.specs_card.body.addWidget(self.specs)
        self.info_card = style.Card("Definición, calibración y resultados", icon_name="ok")
        self.info = QLabel()
        self.info.setWordWrap(True)
        self.info.setTextFormat(Qt.TextFormat.RichText)
        self.info.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.info_card.body.addWidget(self.info)
        self.files_card = style.Card("Ficheros", icon_name="carpeta")
        self.files = QListWidget()
        self.files.setMaximumHeight(150)
        self.files.itemDoubleClicked.connect(self._open_file)
        self.files_card.body.addWidget(self.files)
        for card in (self.header, self.plot_card, self.specs_card, self.info_card, self.files_card):
            layout.addWidget(card)
        layout.addStretch(1)
        self.show_record(None)

    def show_record(self, record: TestRecord | None) -> None:
        self.record = record
        while self.chips.count():
            widget = self.chips.takeAt(0).widget()
            if widget:
                widget.deleteLater()
        for card in (self.plot_card, self.specs_card, self.info_card, self.files_card):
            card.setVisible(record is not None)
        if record is None:
            self.title.setText("Seleccione un ensayo")
            self.subtitle.setText(
                "Cada ensayo es una carpeta con su definición (ensayo.yaml), sus especificaciones, "
                "los ficheros de resultados y el registro de operaciones."
            )
            return
        self.title.setText(record.title)
        duration = f" · {(record.finished - record.started).total_seconds():.0f} s" if record.finished else ""
        when = f"creado {record.created:%d/%m/%Y %H:%M}" if record.status == "preparado" and record.created else f"{record.started:%d/%m/%Y %H:%M:%S}{duration}"
        self.subtitle.setText(
            f"{html.escape(record.procedure.get('nombre', '—'))} · setup {html.escape(record.setup.get('nombre', '—'))} · "
            f"equipo a calibrar <b>{html.escape(record.dut_id or '—')}</b><br>"
            f"{when} · operador: {html.escape(record.operator or '—')} · {html.escape(str(record.folder))}"
        )
        top = Qt.AlignmentFlag.AlignTop
        self.chips.addWidget(style.chip(record.status_name, style.status_color(record.status)), 0, top)
        chip = verdict_chip(record.verdict)
        if chip is not None:
            self.chips.addWidget(chip, 0, top)
        if record.simulated:
            self.chips.addWidget(style.chip("SIMULADO", style.SIMULATION), 0, top)

        self._show_specs(record)
        self.info.setText(self._info_html(record) or "Sin datos")
        self.files.clear()
        self.plot.clear()
        folder = record.folder
        for name in record.files:
            lower = name.lower()
            kind = "grafica" if lower.endswith((".s1p", ".s2p")) else ("exportar" if lower.endswith(".csv") else "procedimientos")
            item = QListWidgetItem(icon(kind, style.PRIMARY.name()), name)
            item.setData(Qt.ItemDataRole.UserRole, str(folder / name) if folder else name)
            self.files.addItem(item)
        for label, frequencies, s in record_datasets(record):
            self.plot.set_data(label, frequencies, s, log_x=is_logarithmic(record))
        if self.plot.datasets:
            self.plot.set_limits(record_spec_curves(self.library, record))
        self.plot_card.setVisible(bool(self.plot.datasets))
        self.files_card.setVisible(bool(record.files))

    def _show_specs(self, record: TestRecord) -> None:
        if record.spec_results:
            rows = []
            for entry in record.spec_results:
                result = entry.get("resultado", "")
                color = style.VERDICT_COLORS.get(result, style.NEUTRAL).name()
                measured = f" ({html.escape(str(entry['medida']))})" if entry.get("medida") else ""
                details = "".join(f"<li>{html.escape(line)}</li>" for line in entry.get("detalle", []))
                rows.append(
                    f"<b>{html.escape(str(entry.get('nombre', '')))}</b>{measured} — {html.escape(str(entry.get('criterio', '')))}: "
                    f"<b style='color:{color}'>{VERDICTS.get(result, result).upper()}</b>"
                    f"<ul style='margin-top:2px; margin-bottom:4px'>{details}</ul>"
                )
            self.specs.setText("".join(rows))
            self.specs_card.setVisible(True)
            return
        template = None
        if record.definition is not None:
            try:
                template = record.definition.template()
            except ValueError:
                template = None
        if template is not None and template.specifications:
            items = "".join(f"<li>{html.escape(s.name)}: {html.escape(s.describe())} ({html.escape(Path(s.file).name)})</li>" for s in template.specifications)
            note = "Se evaluarán al ejecutar el ensayo." if record.status == "preparado" else "No se han evaluado."
            self.specs.setText(f"{note}<ul style='margin-top:2px'>{items}</ul>")
        else:
            self.specs.setText("La plantilla no define especificaciones: el ensayo no da veredicto.")
        self.specs_card.setVisible(True)

    def _info_html(self, record: TestRecord) -> str:
        parts = []
        if record.message:
            parts.append(html.escape(record.message))
        if record.status == "preparado" and record.definition is not None:
            try:
                sweep = record.definition.template().sweep
                parts.append(f"<b>Frecuencias:</b> {html.escape(sweep.summary())}")
            except ValueError:
                pass
            parts.append("Ensayo preparado: ábralo para revisar la comprobación previa y ejecutarlo.")
        calibration = record.calibration
        if calibration:
            text = f"<b>Calibración:</b> {calibration.get('metodo', '—')}"
            if calibration.get("kit"):
                text += f" · kit {calibration['kit']}" + (f" ({html.escape(str(calibration.get('kit_en_vna')))})" if calibration.get("kit_en_vna") else "")
            if calibration.get("planos"):
                text += " · " + ", ".join(f"{k}: {v}" for k, v in calibration["planos"].items())
            parts.append(text)
        checks = record.verification.get("comprobaciones", [])
        if checks:
            items = "".join(
                f"<li>{html.escape(c['comprobacion'])}: {c['valor_db']:g} dB (límite {c['limite_db']:g} dB) "
                f"<b style='color:{'#2e8b57' if c['correcto'] else '#c0392b'}'>{'OK' if c['correcto'] else 'FUERA'}</b></li>"
                for c in checks
            )
            parts.append(f"<b>Verificación</b><ul style='margin-top:2px'>{items}</ul>")
        if record.results:
            rows = []

            def flatten(prefix: str, value) -> None:
                if isinstance(value, dict):
                    for key, item in value.items():
                        flatten(f"{prefix} · {key}" if prefix else str(key), item)
                else:
                    rows.append(f"<li>{html.escape(prefix.replace('_', ' '))}: <b>{html.escape(str(value))}</b></li>")

            flatten("", record.results)
            parts.append(f"<b>Resultados</b><ul style='margin-top:2px'>{''.join(rows)}</ul>")
        sweep = record.sweep
        if sweep:
            text = (
                f"<b>Barrido:</b> {html.escape(str(sweep.get('modo', sweep.get('tipo', ''))))} {sweep.get('inicio')} – {sweep.get('fin')}, "
                f"{sweep.get('puntos')} puntos, IFBW {sweep.get('ifbw')}, {sweep.get('potencia_dbm')} dBm"
            )
            if sweep.get("por_traza"):
                text += " (ajustes por traza)"
            parts.append(text)
        if record.instrument:
            parts.append(f"<b>VNA:</b> {html.escape(str(record.instrument.get('identificacion', '—')))}")
        if record.equipment:
            equipment = ", ".join(f"{e.get('id')} ({e.get('rol')})" for e in record.equipment)
            parts.append(f"<b>Equipos:</b> {html.escape(equipment)}")
        return "<br>".join(parts)

    def _open_file(self, item: QListWidgetItem) -> None:
        QDesktopServices.openUrl(QUrl.fromLocalFile(item.data(Qt.ItemDataRole.UserRole)))


class TestsView(QWidget):
    __test__ = False  # not a pytest test class

    new_test = Signal(object)  # folder where to launch the test
    open_test = Signal(object)  # folder of the test
    repeat_test = Signal(object)  # folder of the test
    tests_changed = Signal()
    title = "Ensayos"

    def __init__(self, library: Library, parent: QWidget | None = None):
        super().__init__(parent)
        self.library = library
        self.records: list[TestRecord] = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 10)
        layout.addWidget(
            style.PageHeader(
                "Ensayos",
                "Explorador de ensayos: cada ensayo se lanza en una carpeta con una plantilla de ensayo, un hardware setup "
                "y el equipo a calibrar, y guarda su definición, especificaciones y resultados.",
            )
        )
        splitter = QSplitter(Qt.Orientation.Horizontal)

        folders_card = style.Card("Carpetas", icon_name="carpeta")
        self.folders = TestFolderTree(library)
        self.folders.folder_changed.connect(lambda _f: self.reload())
        folders_card.body.addWidget(self.folders, 1)
        buttons = QHBoxLayout()
        new_folder = QPushButton(icon("carpeta_nueva", style.PRIMARY.name()), "Nueva")
        new_folder.setToolTip("Nueva carpeta dentro de la seleccionada")
        external = QPushButton(icon("carpeta_externa", style.PRIMARY.name()), "Externa…")
        external.setToolTip("Añadir una carpeta de ensayos fuera de la biblioteca")
        self.remove_external = QPushButton(icon("eliminar", style.ERROR.name()), "")
        self.remove_external.setToolTip("Quitar la carpeta externa del explorador (no borra los ensayos)")
        new_folder.clicked.connect(self.folders.create_folder)
        external.clicked.connect(self.folders.add_external)
        self.remove_external.clicked.connect(self.folders.remove_external)
        buttons.addWidget(new_folder)
        buttons.addWidget(external)
        buttons.addStretch(1)
        buttons.addWidget(self.remove_external)
        folders_card.body.addLayout(buttons)

        middle = QWidget()
        middle_layout = QVBoxLayout(middle)
        middle_layout.setContentsMargins(0, 0, 0, 0)
        filters = QHBoxLayout()
        self.status_filter = QComboBox()
        self.status_filter.addItem("Todos los estados", None)
        for key, name in STATUS_NAMES.items():
            self.status_filter.addItem(name, key)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Buscar por nombre, equipo, plantilla o setup…")
        self.search.setClearButtonEnabled(True)
        self.search.addAction(icon("buscar", style.NEUTRAL.name()), QLineEdit.ActionPosition.LeadingPosition)
        self.count = QLabel()
        self.count.setObjectName("pageSubtitle")
        filters.addWidget(self.status_filter)
        filters.addWidget(self.search, 1)
        filters.addWidget(self.count)
        middle_layout.addLayout(filters)
        self.table = style.make_table(COLUMNS)
        self.table.itemSelectionChanged.connect(self._selected)
        self.table.itemDoubleClicked.connect(lambda _item: self._open())
        middle_layout.addWidget(self.table, 1)

        self.detail = TestDetail(library)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.detail)
        scroll.setMinimumWidth(440)
        splitter.addWidget(folders_card)
        splitter.addWidget(middle)
        splitter.addWidget(scroll)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 3)
        splitter.setStretchFactor(2, 2)
        splitter.setSizes([250, 640, 560])
        layout.addWidget(splitter, 1)

        self.action_new = QAction(icon("ejecutar", style.OK.name()), "Nuevo\nensayo", self)
        self.action_new.setToolTip("Lanzar una plantilla de ensayo en la carpeta seleccionada")
        self.action_open = QAction(icon("abrir", style.PRIMARY.name()), "Abrir\nensayo", self)
        self.action_repeat = QAction(icon("recargar", style.PRIMARY.name()), "Repetir", self)
        self.action_new_folder = QAction(icon("carpeta_nueva", style.PRIMARY.name()), "Nueva\ncarpeta", self)
        self.action_external = QAction(icon("carpeta_externa", style.PRIMARY.name()), "Carpeta\nexterna", self)
        self.action_folder = QAction(icon("carpeta", style.PRIMARY.name()), "Abrir\ncarpeta", self)
        self.action_delete = QAction(icon("eliminar", style.ERROR.name()), "Eliminar", self)
        self.action_new.triggered.connect(lambda: self.new_test.emit(self.folders.current_folder() or self.library.tests_folder))
        self.action_open.triggered.connect(self._open)
        self.action_repeat.triggered.connect(self._repeat)
        self.action_new_folder.triggered.connect(self.folders.create_folder)
        self.action_external.triggered.connect(self.folders.add_external)
        self.action_folder.triggered.connect(self._open_folder)
        self.action_delete.triggered.connect(self._delete)
        self.status_filter.currentIndexChanged.connect(lambda _i: self.refresh())
        self.search.textChanged.connect(lambda _t: self.refresh())
        self.reload()

    def ribbon_actions(self) -> list[QAction | None]:
        return [
            self.action_new,
            self.action_open,
            self.action_repeat,
            None,
            self.action_new_folder,
            self.action_external,
            None,
            self.action_folder,
            self.action_delete,
        ]

    def set_library(self, library: Library) -> None:
        self.library = library
        self.detail.library = library
        self.folders.set_library(library)

    def reload(self, select: str | None = None) -> None:
        folder = self.folders.current_folder()
        self.records = self.library.tests(folder) if folder is not None else self.library.tests()
        self.remove_external.setEnabled(self.folders.is_external_root(folder))
        self.refresh(select)

    def show_test(self, test_folder: str | Path) -> None:
        """Select the folder that contains the test and then the test."""
        path = Path(test_folder)
        self.folders.reload(select=path.parent)
        self.reload(select=str(path))

    def refresh(self, select: str | None = None) -> None:
        current = select or (record_key(self.detail.record) if self.detail.record else None)
        status = self.status_filter.currentData()
        text = self.search.text().strip().lower()
        base = self.folders.current_folder()
        self.table.setRowCount(0)
        shown = 0
        for record in self.records:
            if status and record.status != status:
                continue
            relative = "—"
            if base is not None and record.folder is not None:
                try:
                    parent = record.folder.parent.relative_to(base)
                    relative = "—" if str(parent) == "." else parent.as_posix()
                except ValueError:
                    relative = str(record.folder.parent)
            when = record.created if record.status == "preparado" and record.created else record.started
            values = [
                record.title,
                relative,
                f"{when:%Y-%m-%d %H:%M}",
                record.dut_id or "—",
                record.procedure.get("nombre", "—"),
                record.setup.get("nombre", "—"),
                record.status_name + (" · simulado" if record.simulated else ""),
                VERDICTS.get(record.verdict, "—") if record.verdict else "—",
            ]
            if text and text not in " ".join(values).lower():
                continue
            row = self.table.rowCount()
            self.table.insertRow(row)
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setData(Qt.ItemDataRole.UserRole, record_key(record))
                if column == 0:
                    item.setIcon(icon("ensayos", style.PRIMARY.name()))
                    item.setToolTip(str(record.folder))
                if column == 6:
                    item.setIcon(style.status_icon(record.status))
                    item.setForeground(style.status_color(record.status))
                if column == 7 and record.verdict in style.VERDICT_COLORS:
                    item.setForeground(style.VERDICT_COLORS[record.verdict])
                self.table.setItem(row, column, item)
            shown += 1
        self.table.resizeColumnsToContents()
        self.count.setText(f"{shown} de {len(self.records)} ensayos")
        for row in range(self.table.rowCount()):
            if self.table.item(row, 0).data(Qt.ItemDataRole.UserRole) == current:
                self.table.selectRow(row)
                break
        else:
            if self.table.rowCount():
                self.table.selectRow(0)
            else:
                self.detail.show_record(None)
        self._update_actions()

    def _record(self) -> TestRecord | None:
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return None
        key = self.table.item(rows[0].row(), 0).data(Qt.ItemDataRole.UserRole)
        return next((r for r in self.records if record_key(r) == key), None)

    def _selected(self) -> None:
        self.detail.show_record(self._record())
        self._update_actions()

    def _update_actions(self) -> None:
        record = self._record()
        has = record is not None
        self.action_open.setEnabled(has)
        self.action_open.setText("Ejecutar\nensayo" if has and record.status == "preparado" else "Abrir\nensayo")
        self.action_folder.setEnabled(has)
        self.action_delete.setEnabled(has)
        self.action_repeat.setEnabled(has and record.definition is not None and record.status != "preparado")

    def _open(self) -> None:
        record = self._record()
        if record is not None:
            self.open_test.emit(record.folder)

    def _repeat(self) -> None:
        record = self._record()
        if record is not None:
            self.repeat_test.emit(record.folder)

    def _open_folder(self) -> None:
        record = self._record()
        if record is not None and record.folder is not None:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(record.folder)))

    def _delete(self) -> None:
        record = self._record()
        if record is None:
            return
        answer = QMessageBox.question(self, "Eliminar ensayo", f"¿Eliminar el ensayo «{record.title}» y todos sus ficheros?\n{record.folder}")
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            self.library.delete_test(record)
        except (LibraryError, OSError) as exc:
            QMessageBox.warning(self, "Eliminar ensayo", str(exc))
            return
        self.detail.show_record(None)
        self.folders.reload()
        self.tests_changed.emit()
