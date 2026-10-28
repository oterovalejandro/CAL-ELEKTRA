"""Main window: navigation rail, contextual ribbon, workspace with document tabs, status bar."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QButtonGroup,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QSizePolicy,
    QStackedWidget,
    QTabBar,
    QTabWidget,
    QToolBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from calelektra import __version__
from calelektra.catalog.library import Library, LibraryError
from calelektra.gui import style
from calelektra.gui.catalog_view import CatalogView
from calelektra.gui.equipment_dialogs import ModelDialog
from calelektra.gui.home_view import HomeView
from calelektra.gui.icons import icon
from calelektra.gui.inventory_view import InventoryView
from calelektra.gui.procedures_view import ProceduresView
from calelektra.gui.setup_editor import SetupEditor
from calelektra.gui.setups_view import SetupsView
from calelektra.gui.test_window import LaunchTestDialog, TestWindow
from calelektra.gui.tests_view import TestsView
from calelektra.instruments.factory import is_simulated
from calelektra.procedures.record import TestRecord
from calelektra.setups.model import ConfiguredSetup

SECTIONS = [
    ("inicio", "Inicio", "inicio"),
    ("ensayos", "Ensayos", "ensayos"),
    ("procedimientos", "Plantillas\nde ensayo", "procedimientos"),
    ("setups", "Hardware\nsetups", "setups"),
    ("equipos", "Equipos", "equipos"),
    ("catalogo", "Catálogo", "catalogo"),
]


class MainWindow(QMainWindow):
    def __init__(self, library: Library):
        super().__init__()
        self.library = library
        self.resize(1560, 940)
        self.setMinimumSize(1100, 700)

        self.home = HomeView(library)
        self.tests_view = TestsView(library)
        self.procedures_view = ProceduresView(library)
        self.setups_view = SetupsView(library)
        self.inventory = InventoryView(library)
        self.catalog = CatalogView(library)
        self.pages: dict[str, QWidget] = {
            "inicio": self.home,
            "ensayos": self.tests_view,
            "procedimientos": self.procedures_view,
            "setups": self.setups_view,
            "equipos": self.inventory,
            "catalogo": self.catalog,
        }
        self.stack = QStackedWidget()
        for page in self.pages.values():
            self.stack.addWidget(page)

        self.workspace = QTabWidget()
        self.workspace.setObjectName("workspace")
        self.workspace.setDocumentMode(True)
        self.workspace.setTabsClosable(True)
        self.workspace.setMovable(True)
        self.workspace.tabCloseRequested.connect(self.close_tab)
        self.workspace.addTab(self.stack, icon("inicio", style.PRIMARY.name(), 16), "Inicio")
        self.workspace.tabBar().setTabButton(0, QTabBar.ButtonPosition.RightSide, None)
        self.workspace.tabBar().setTabButton(0, QTabBar.ButtonPosition.LeftSide, None)

        self.rail = self._build_rail()
        self.ribbon = QToolBar("Acciones")
        self.ribbon.setObjectName("ribbon")
        self.ribbon.setMovable(False)
        self.ribbon.setFloatable(False)
        self.ribbon.setIconSize(QSize(22, 22))
        self.ribbon.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
        central = QWidget()
        layout = QHBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        right = QVBoxLayout()
        right.setContentsMargins(0, 0, 0, 0)
        right.setSpacing(0)
        right.addWidget(self.ribbon)
        right.addWidget(self.workspace, 1)
        layout.addWidget(self.rail)
        layout.addLayout(right, 1)
        self.setCentralWidget(central)
        self.workspace.currentChanged.connect(lambda _i: self._update_ribbon())
        self.global_actions = self._global_actions()
        self._build_menu()
        self._build_status_bar()
        self._connect_views()
        self.show_section("inicio")
        self._update_status()
        self._update_title()

    # ── layout pieces ──────────────────────────────────────────────────────
    def _build_rail(self) -> QWidget:
        rail = QWidget()
        rail.setObjectName("rail")
        rail.setFixedWidth(92)
        layout = QVBoxLayout(rail)
        layout.setContentsMargins(0, 0, 0, 8)
        layout.setSpacing(2)
        logo = QLabel("CAL\nELEKTRA")
        logo.setObjectName("railLogo")
        logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(logo)
        self.rail_group = QButtonGroup(self)
        self.rail_group.setExclusive(True)
        self.rail_buttons: dict[str, QToolButton] = {}
        for index, (key, text, icon_name) in enumerate(SECTIONS):
            button = QToolButton()
            button.setObjectName("railButton")
            button.setText(text)
            button.setIcon(icon(icon_name, "#dfe7f1", 26))
            button.setIconSize(QSize(26, 26))
            button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
            button.setCheckable(True)
            button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            button.setToolTip(f"{text.replace(chr(10), ' ')} (Ctrl+{index + 1})")
            button.clicked.connect(lambda _checked, k=key: self.show_section(k))
            self.rail_group.addButton(button)
            self.rail_buttons[key] = button
            layout.addWidget(button)
        layout.addStretch(1)
        library_button = QToolButton()
        library_button.setObjectName("railButton")
        library_button.setText("Biblioteca")
        library_button.setIcon(icon("biblioteca", "#dfe7f1", 22))
        library_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
        library_button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        library_button.setToolTip("Abrir otra biblioteca")
        library_button.clicked.connect(self.choose_library)
        layout.addWidget(library_button)
        return rail

    def _global_actions(self) -> list[QAction]:
        import_action = QAction(icon("importar", style.PRIMARY.name()), "Importar\nYAML", self)
        import_action.triggered.connect(self.import_yaml)
        reload_action = QAction(icon("recargar", style.PRIMARY.name()), "Recargar", self)
        reload_action.setShortcut(QKeySequence.StandardKey.Refresh)
        reload_action.triggered.connect(self.reload_library)
        return [import_action, reload_action]

    def _build_menu(self) -> None:
        menu = self.menuBar().addMenu("&Archivo")
        for text, shortcut, slot in (
            ("Abrir biblioteca…", QKeySequence.StandardKey.Open, self.choose_library),
            ("Importar YAML…", None, self.import_yaml),
            ("Recargar biblioteca", QKeySequence.StandardKey.Refresh, self.reload_library),
            (None, None, None),
            ("Salir", QKeySequence.StandardKey.Quit, self.close),
        ):
            if text is None:
                menu.addSeparator()
                continue
            action = QAction(text, self)
            if shortcut is not None:
                action.setShortcut(shortcut)
            action.triggered.connect(slot)
            menu.addAction(action)
        view = self.menuBar().addMenu("&Ver")
        for index, (key, text, icon_name) in enumerate(SECTIONS):
            action = QAction(icon(icon_name, style.PRIMARY.name()), text.replace("\n", " "), self)
            action.setShortcut(QKeySequence(f"Ctrl+{index + 1}"))
            action.triggered.connect(lambda _checked=False, k=key: self.show_section(k))
            view.addAction(action)
        test_menu = self.menuBar().addMenu("&Ensayo")
        launch = QAction(icon("ejecutar", style.OK.name()), "Lanzar ensayo…", self)
        launch.setShortcut(QKeySequence("Ctrl+R"))
        launch.triggered.connect(lambda: self.launch_test())
        test_menu.addAction(launch)
        self.stop_test_action = QAction(icon("detener", style.ERROR.name()), "Detener ensayo", self)
        self.stop_test_action.triggered.connect(self.stop_test)
        test_menu.addAction(self.stop_test_action)
        test_menu.aboutToShow.connect(lambda: self.stop_test_action.setEnabled(self._test_to_stop() is not None))
        help_menu = self.menuBar().addMenu("A&yuda")
        about = QAction("Acerca de CAL-ELEKTRA", self)
        about.triggered.connect(self._about)
        help_menu.addAction(about)

    def _test_to_stop(self) -> TestWindow | None:
        """The test running in the current tab or, if that one is not running, the only test running."""
        current = self.workspace.currentWidget()
        if isinstance(current, TestWindow) and current.running:
            return current
        running = [window for window in self._documents(TestWindow) if window.running]
        return running[0] if len(running) == 1 else None

    def stop_test(self) -> None:
        window = self._test_to_stop()
        if window is not None:
            window.stop()

    def _build_status_bar(self) -> None:
        self.status_library = QLabel()
        self.status_instruments = QLabel()
        self.statusBar().addPermanentWidget(self.status_instruments)
        self.statusBar().addPermanentWidget(self.status_library)

    def _connect_views(self) -> None:
        self.home.open_setup.connect(self.open_setup)
        self.home.open_test.connect(self.open_test_folder)
        self.home.open_equipment.connect(self.show_equipment)
        self.home.launch_test.connect(lambda: self.launch_test())
        self.home.new_equipment.connect(lambda: (self.show_section("equipos"), self.inventory.new_equipment()))
        self.home.new_setup.connect(lambda: (self.show_section("setups"), self.setups_view.create(None)))
        self.inventory.equipment_changed.connect(lambda _id: self._library_changed(skip=self.inventory))
        self.catalog.catalog_changed.connect(lambda: self._library_changed(skip=self.catalog))
        self.catalog.new_equipment_requested.connect(lambda model_id: (self.show_section("equipos"), self.inventory.new_equipment(model_id)))
        self.setups_view.open_setup.connect(self.open_setup)
        self.setups_view.new_setup.connect(self.open_new_setup)
        self.setups_view.new_template.connect(self.new_template)
        self.setups_view.setups_changed.connect(lambda: self._library_changed(skip=self.setups_view))
        self.procedures_view.launch.connect(lambda procedure_id: self.launch_test(procedure_id=procedure_id))
        self.procedures_view.edit_model.connect(self.edit_model)
        self.procedures_view.open_setup.connect(self.open_setup)
        self.procedures_view.procedures_changed.connect(lambda: self._library_changed(skip=self.procedures_view))
        self.tests_view.launch_test.connect(lambda folder: self.launch_test(folder=folder))
        self.tests_view.open_test.connect(self.open_test_folder)
        self.tests_view.repeat_test.connect(self.repeat_test)
        self.tests_view.tests_changed.connect(self.home.reload)
        self.tests_view.record_changed.connect(self._record_changed)

    # ── sections and ribbon ────────────────────────────────────────────────
    def show_section(self, key: str) -> None:
        page = self.pages[key]
        self.stack.setCurrentWidget(page)
        self.rail_buttons[key].setChecked(True)
        name = next(text for k, text, _ in SECTIONS if k == key).replace("\n", " ")
        icon_name = next(i for k, _, i in SECTIONS if k == key)
        self.workspace.setTabText(0, name)
        self.workspace.setTabIcon(0, icon(icon_name, style.PRIMARY.name(), 16))
        self.workspace.setCurrentIndex(0)
        self._update_ribbon()

    def current_section(self) -> str:
        page = self.stack.currentWidget()
        return next(key for key, widget in self.pages.items() if widget is page)

    def _update_ribbon(self) -> None:
        self.ribbon.clear()
        widget = self.workspace.currentWidget()
        if widget is self.stack:
            page = self.stack.currentWidget()
            title = getattr(page, "title", "")
            actions = page.ribbon_actions() if hasattr(page, "ribbon_actions") else []
            self.rail_buttons[self.current_section()].setChecked(True)
        else:
            title = self.workspace.tabText(self.workspace.currentIndex()).rstrip(" *")
            actions = widget.ribbon_actions() if hasattr(widget, "ribbon_actions") else []
            section = "ensayos" if isinstance(widget, TestWindow) else "setups"
            self.rail_buttons[section].setChecked(True)
        label = QLabel(title)
        label.setObjectName("ribbonSection")
        self.ribbon.addWidget(label)
        self.ribbon.addSeparator()
        for action in actions:
            if action is None:
                self.ribbon.addSeparator()
            else:
                self.ribbon.addAction(action)
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.ribbon.addWidget(spacer)
        for action in self.global_actions:
            self.ribbon.addAction(action)

    # ── documents ──────────────────────────────────────────────────────────
    def _documents(self, kind: type) -> list:
        return [w for i in range(1, self.workspace.count()) if isinstance(w := self.workspace.widget(i), kind)]

    def open_setup(self, setup_id: str) -> SetupEditor | None:
        for editor in self._documents(SetupEditor):
            if editor.setup.id == setup_id:
                self.workspace.setCurrentWidget(editor)
                return editor
        setup = self.library.setups.get(setup_id)
        if setup is None:
            return None
        return self._add_editor(SetupEditor(self.library, setup))

    def open_new_setup(self, setup: ConfiguredSetup) -> SetupEditor:
        return self._add_editor(SetupEditor(self.library, setup, is_new=True))

    def _add_editor(self, editor: SetupEditor) -> SetupEditor:
        index = self.workspace.addTab(editor, icon("setups", style.PRIMARY.name(), 16), self._editor_title(editor))
        editor.dirty_changed.connect(lambda _dirty, e=editor: self._retitle(e))
        editor.saved.connect(lambda _id: self._library_changed())
        editor.new_template.connect(self.new_template)
        self.workspace.setCurrentIndex(index)
        return editor

    def _editor_title(self, editor: SetupEditor) -> str:
        return f"{editor.setup.name}{' *' if editor.dirty else ''}"

    def _retitle(self, editor: SetupEditor) -> None:
        index = self.workspace.indexOf(editor)
        if index >= 0:
            self.workspace.setTabText(index, self._editor_title(editor))

    def launch_test(self, procedure_id: str | None = None, folder: Path | None = None) -> TestWindow | None:
        """Launch a test template in a folder: the run creates the test (the record of the measurement)."""
        if not self.library.procedures:
            QMessageBox.information(self, "Lanzar ensayo", "La biblioteca no tiene plantillas de ensayo.")
            return None
        if folder is None and self.current_section() == "ensayos":
            folder = self.tests_view.folders.current_folder()
        if procedure_id is None and self.current_section() == "procedimientos":
            procedure_id = self.procedures_view._current_id()
        dialog = LaunchTestDialog(self.library, procedure_id, folder, parent=self)
        if not dialog.exec():
            return None
        procedure = dialog.current_procedure()
        folder = dialog.current_folder()
        assert procedure is not None and folder is not None
        window = self._add_test_window(
            TestWindow(
                self.library,
                procedure,
                folder,
                dialog.name.text().strip(),
                dialog.operator.text().strip(),
                dialog.automatic.isChecked(),
            )
        )
        window.start()
        return window

    def _add_test_window(self, window: TestWindow) -> TestWindow:
        window.finished.connect(lambda folder, w=window: (self._retitle_test(w), self._tests_changed(folder)))
        window.record_changed.connect(self._record_changed)
        index = self.workspace.addTab(window, icon("ensayos", style.OK.name(), 16), window.title)
        self.workspace.setCurrentIndex(index)
        return window

    def open_test_record(self, record: TestRecord) -> TestWindow:
        for window in self._documents(TestWindow):
            if window.record is not None and window.record.folder == record.folder:
                self.workspace.setCurrentWidget(window)
                return window
        return self._add_test_window(TestWindow(self.library, record=record))

    def open_test_folder(self, folder: str | Path) -> TestWindow | None:
        try:
            record = self.library.load_test(Path(folder))
        except LibraryError as exc:
            QMessageBox.warning(self, "Abrir ensayo", str(exc))
            return None
        return self.open_test_record(record)

    def repeat_test(self, folder: str | Path) -> TestWindow | None:
        """Launch the template of a test again, in the same folder."""
        try:
            record = self.library.load_test(Path(folder))
        except LibraryError as exc:
            QMessageBox.warning(self, "Repetir ensayo", str(exc))
            return None
        if (record.template_id or "") not in self.library.procedures:
            QMessageBox.warning(self, "Repetir ensayo", f"La plantilla «{record.template_id}» ya no existe en la biblioteca.")
            return None
        return self.launch_test(record.template_id, record.folder.parent if record.folder else None)

    def new_template(self, setup_id: str) -> None:
        """New test template that calls a hardware setup."""
        self.show_section("procedimientos")
        self.procedures_view.new_procedure(setup_id)

    def edit_model(self, model_id: str) -> None:
        model = self.library.models.get(model_id)
        if model is None:
            return
        dialog = ModelDialog(self.library, model, parent=self)
        if dialog.exec():
            self._library_changed()
            self.procedures_view.editor.reload_specs()

    def _retitle_test(self, window: TestWindow) -> None:
        index = self.workspace.indexOf(window)
        if index >= 0:
            self.workspace.setTabText(index, window.title)

    def _tests_changed(self, folder: str) -> None:
        self.tests_view.folders.reload()
        self.tests_view.reload(select=folder)
        self.home.reload()

    def _record_changed(self, folder: str) -> None:
        """A test record was changed (ambient conditions loaded): refresh where it is shown."""
        for window in self._documents(TestWindow):
            if window.record is not None and window.record.folder == Path(folder) and window is not self.sender():
                window.reload_record()
        self.tests_view.reload(select=folder)
        self.home.reload()

    def show_test(self, folder: str) -> None:
        self.show_section("ensayos")
        self.tests_view.show_test(folder)

    def show_equipment(self, equipment_id: str) -> None:
        self.show_section("equipos")
        self.inventory.select(equipment_id)

    def close_tab(self, index: int) -> None:
        if index == 0:
            return
        widget = self.workspace.widget(index)
        if isinstance(widget, SetupEditor) and widget.dirty:
            answer = QMessageBox.question(
                self,
                "Cerrar setup",
                f"«{widget.setup.name}» tiene cambios sin guardar. ¿Guardar antes de cerrar?",
                QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel,
            )
            if answer == QMessageBox.StandardButton.Cancel:
                return
            if answer == QMessageBox.StandardButton.Save and not widget.save():
                return
        if isinstance(widget, TestWindow) and not widget.can_close():
            return
        self.workspace.removeTab(index)
        widget.deleteLater()

    # ── library ────────────────────────────────────────────────────────────
    def _library_changed(self, skip: QWidget | None = None) -> None:
        for page in self.pages.values():
            if page is not skip and hasattr(page, "reload"):
                page.reload()
        self.tests_view.folders.reload()  # folders may have changed on disk (and with them the tests listed)
        for editor in self._documents(SetupEditor):
            editor.refresh()
            self._retitle(editor)
        for window in self._documents(TestWindow):
            window.library_changed()  # launches and «Repetir» use the current equipment, setups and templates
        self._update_status()

    def choose_library(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Carpeta de la biblioteca", str(self.library.root))
        if not folder:
            return
        try:
            library = Library.load(folder)
        except LibraryError as exc:
            QMessageBox.critical(self, "Biblioteca", str(exc))
            return
        if not self._close_documents():
            return
        self.library = library
        for page in self.pages.values():
            page.set_library(library)
        self._update_status()
        self._update_title()

    def _close_documents(self) -> bool:
        for index in range(self.workspace.count() - 1, 0, -1):
            count = self.workspace.count()
            self.close_tab(index)
            if self.workspace.count() == count:
                return False
        return True

    def reload_library(self) -> None:
        try:
            self.library.reload()
        except LibraryError as exc:
            QMessageBox.critical(self, "Biblioteca", str(exc))
            return
        self._library_changed()

    def import_yaml(self) -> None:
        filenames, _ = QFileDialog.getOpenFileNames(self, "Importar YAML", str(Path.home()), "YAML (*.yaml *.yml)")
        imported = []
        for filename in filenames:
            try:
                kind, _target = self.library.import_yaml(Path(filename))
                imported.append(f"{Path(filename).name} → {kind}")
            except (LibraryError, OSError, ValueError) as exc:
                QMessageBox.warning(self, "Importar YAML", str(exc))
        if imported:
            self._library_changed()
            QMessageBox.information(self, "Importar YAML", "Importado:\n" + "\n".join(imported))

    def _update_title(self) -> None:
        self.setWindowTitle(f"CAL-ELEKTRA {__version__} — {self.library.root.name}")

    def _update_status(self) -> None:
        library = self.library
        problems = len(library.load_issues)
        text = f"Biblioteca: {library.root}"
        if problems:
            text += f"  ·  {problems} problemas al cargar (ver Inicio)"
        self.status_library.setText(text)
        vnas = []
        for equipment in sorted(library.equipment_in_categories(["vna"]), key=lambda e: e.id):
            mode = "simulado" if is_simulated(equipment, library) else (equipment.connection.resource if equipment.connection and equipment.connection.resource else "real")
            vnas.append(f"{equipment.id}: {mode}")
        self.status_instruments.setText("VNA  ·  " + "  ·  ".join(vnas) if vnas else "Sin VNA en el inventario")

    def _about(self) -> None:
        QMessageBox.about(
            self,
            "CAL-ELEKTRA",
            f"<b>CAL-ELEKTRA {__version__}</b><br><br>Software de laboratorio de calibración de equipos de MC, "
            "inspirado en la arquitectura de R&amp;S ELEKTRA (hardware setups, procedimientos, ensayos).<br><br>"
            "Equipos definidos por categorías con campos tipados; hardware setups con comprobación de rango, "
            "requisitos, conectores y calibración; ensayos con VNA (simulado o R&amp;S ZNB vía comandos del driver RsZnx).",
        )

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt API)
        dirty = [e.setup.name for e in self._documents(SetupEditor) if e.dirty]
        running = [w for w in self._documents(TestWindow) if w.running]
        if dirty or running:
            parts = []
            if dirty:
                parts.append(f"setups con cambios sin guardar ({', '.join(dirty)})")
            if running:
                parts.append("ensayos en curso (se detendrán)")
            answer = QMessageBox.question(self, "Salir", f"Hay {' y '.join(parts)}. ¿Salir de todos modos?")
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            for window in running:
                window.stop()
                window.wait(10_000)
        event.accept()
