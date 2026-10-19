"""Main window: navigation (left), closable tabs (centre), library menu."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QFileDialog,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QSplitter,
    QTabWidget,
)

from calelektra import __version__
from calelektra.catalog.library import Library, LibraryError
from calelektra.gui.home_view import HomeView
from calelektra.gui.inventory_view import InventoryView
from calelektra.gui.setup_editor import SetupEditor
from calelektra.gui.setups_view import SetupsView
from calelektra.setups.model import ConfiguredSetup

NAVIGATION = [
    ("inicio", "Inicio", True),
    ("inventario", "Inventario de equipos", True),
    ("setups", "Hardware setups", True),
    ("procedimientos", "Procedimientos (próximamente)", False),
    ("calibraciones", "Calibraciones (próximamente)", False),
]


class MainWindow(QMainWindow):
    def __init__(self, library: Library):
        super().__init__()
        self.library = library
        self.setWindowTitle(f"CAL-ELEKTRA {__version__}")
        self.resize(1500, 920)

        self.navigation = QListWidget()
        self.navigation.setObjectName("navegacion")
        self.navigation.setMinimumWidth(230)
        self.navigation.setMaximumWidth(280)
        for key, text, enabled in NAVIGATION:
            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, key)
            if not enabled:
                item.setFlags(Qt.ItemFlag.NoItemFlags)
            self.navigation.addItem(item)
        self.navigation.currentItemChanged.connect(self._navigate)

        self.tabs = QTabWidget()
        self.tabs.setTabsClosable(True)
        self.tabs.setDocumentMode(True)
        self.tabs.tabCloseRequested.connect(self.close_tab)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self.navigation)
        splitter.addWidget(self.tabs)
        splitter.setStretchFactor(1, 1)
        self.setCentralWidget(splitter)
        self._build_menu()

        self.home = HomeView(library)
        self.home.open_setup.connect(self.open_setup)
        self.inventory = InventoryView(library)
        self.inventory.equipment_changed.connect(self._equipment_changed)
        self.setups_view = SetupsView(library)
        self.setups_view.open_setup.connect(self.open_setup)
        self.setups_view.new_setup.connect(self.open_new_setup)
        self.fixed_tabs = {"inicio": self.home, "inventario": self.inventory, "setups": self.setups_view}
        self.navigation.setCurrentRow(0)
        self._update_status()

    # ── menu ───────────────────────────────────────────────────────────────
    def _build_menu(self) -> None:
        menu = self.menuBar().addMenu("&Archivo")
        actions = [
            ("Abrir biblioteca…", QKeySequence.StandardKey.Open, self.choose_library),
            ("Importar YAML…", None, self.import_yaml),
            ("Recargar biblioteca", QKeySequence.StandardKey.Refresh, self.reload_library),
            (None, None, None),
            ("Salir", QKeySequence.StandardKey.Quit, self.close),
        ]
        for text, shortcut, slot in actions:
            if text is None:
                menu.addSeparator()
                continue
            action = QAction(text, self)
            if shortcut is not None:
                action.setShortcut(shortcut)
            action.triggered.connect(slot)
            menu.addAction(action)
        help_menu = self.menuBar().addMenu("A&yuda")
        about = QAction("Acerca de CAL-ELEKTRA", self)
        about.triggered.connect(self._about)
        help_menu.addAction(about)

    def _about(self) -> None:
        QMessageBox.about(
            self,
            "CAL-ELEKTRA",
            f"CAL-ELEKTRA {__version__}\n\nSoftware de laboratorio de calibración de equipos de MC.\n"
            "Fase actual: catálogo de equipos, hardware setups y VNA dummy.",
        )

    # ── navigation and tabs ────────────────────────────────────────────────
    def _navigate(self, item: QListWidgetItem | None) -> None:
        if item is None:
            return
        widget = self.fixed_tabs.get(item.data(Qt.ItemDataRole.UserRole))
        if widget is None:
            return
        index = self.tabs.indexOf(widget)
        if index < 0:
            index = self.tabs.addTab(widget, item.text())
        self.tabs.setCurrentIndex(index)

    def _editor_tabs(self) -> list[SetupEditor]:
        return [w for i in range(self.tabs.count()) if isinstance(w := self.tabs.widget(i), SetupEditor)]

    def open_setup(self, setup_id: str) -> SetupEditor | None:
        for editor in self._editor_tabs():
            if editor.setup.id == setup_id:
                self.tabs.setCurrentWidget(editor)
                return editor
        setup = self.library.setups.get(setup_id)
        if setup is None:
            return None
        return self._add_editor(SetupEditor(self.library, setup))

    def open_new_setup(self, setup: ConfiguredSetup) -> SetupEditor:
        return self._add_editor(SetupEditor(self.library, setup, is_new=True))

    def _add_editor(self, editor: SetupEditor) -> SetupEditor:
        index = self.tabs.addTab(editor, self._editor_title(editor))
        editor.dirty_changed.connect(lambda _dirty, e=editor: self._retitle(e))
        editor.saved.connect(lambda _id: self._setup_saved())
        self.tabs.setCurrentIndex(index)
        return editor

    def _editor_title(self, editor: SetupEditor) -> str:
        return f"Setup: {editor.setup.name}{' *' if editor.dirty else ''}"

    def _retitle(self, editor: SetupEditor) -> None:
        index = self.tabs.indexOf(editor)
        if index >= 0:
            self.tabs.setTabText(index, self._editor_title(editor))

    def close_tab(self, index: int) -> None:
        widget = self.tabs.widget(index)
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
        self.tabs.removeTab(index)
        if isinstance(widget, SetupEditor):
            widget.deleteLater()

    # ── library ────────────────────────────────────────────────────────────
    def choose_library(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Carpeta de la biblioteca", str(self.library.root))
        if not folder:
            return
        try:
            library = Library.load(folder)
        except LibraryError as exc:
            QMessageBox.critical(self, "Biblioteca", str(exc))
            return
        self._replace_library(library)

    def _replace_library(self, library: Library) -> None:
        for editor in self._editor_tabs():
            self.tabs.removeTab(self.tabs.indexOf(editor))
            editor.deleteLater()
        self.library = library
        for view in (self.home, self.inventory, self.setups_view):
            view.library = library
        self.inventory.detail.library = library
        self.inventory.detail.show_equipment(None)
        self._refresh_views()

    def reload_library(self) -> None:
        try:
            self.library.reload()
        except LibraryError as exc:
            QMessageBox.critical(self, "Biblioteca", str(exc))
            return
        self._refresh_views()

    def import_yaml(self) -> None:
        filenames, _ = QFileDialog.getOpenFileNames(self, "Importar YAML", str(Path.home()), "YAML (*.yaml *.yml)")
        imported = []
        for filename in filenames:
            try:
                kind, target = self.library.import_yaml(Path(filename))
                imported.append(f"{Path(filename).name} → {kind}")
            except (LibraryError, OSError, ValueError) as exc:
                QMessageBox.warning(self, "Importar YAML", str(exc))
        if imported:
            self._refresh_views()
            QMessageBox.information(self, "Importar YAML", "Importado:\n" + "\n".join(imported))

    def _refresh_views(self) -> None:
        self.home.reload()
        self.inventory.reload()
        self.setups_view.reload()
        for editor in self._editor_tabs():
            editor.refresh()
        self._update_status()

    def _equipment_changed(self, _equipment_id: str) -> None:
        self.home.reload()
        self.setups_view.reload()
        for editor in self._editor_tabs():
            editor.refresh()

    def _setup_saved(self) -> None:
        self.home.reload()
        self.setups_view.reload()
        for editor in self._editor_tabs():
            self._retitle(editor)

    def _update_status(self) -> None:
        problems = len(self.library.load_issues)
        text = f"Biblioteca: {self.library.root}"
        if problems:
            text += f"   ·   {problems} problemas al cargar (ver Inicio)"
        self.statusBar().showMessage(text)

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt API)
        if any(editor.dirty for editor in self._editor_tabs()):
            answer = QMessageBox.question(
                self,
                "Salir",
                "Hay setups con cambios sin guardar. ¿Salir de todos modos?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
        event.accept()
