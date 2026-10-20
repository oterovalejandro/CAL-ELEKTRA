"""Tree of the test folders: ``ensayos/`` of the library, its sub-folders and the external folders.

Tests are launched in a folder chosen by the user (like the test explorer of
ELEKTRA). Folders that are tests themselves (they hold ``ensayo.yaml``) are not
shown; the number of tests of each folder is.
"""

from __future__ import annotations

import re
from pathlib import Path

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtWidgets import QFileDialog, QInputDialog, QMessageBox, QTreeWidget, QTreeWidgetItem, QWidget

from calelektra.catalog.library import Library
from calelektra.gui import style
from calelektra.gui.icons import icon
from calelektra.procedures.record import RECORD_FILE

FOLDER_NAME = re.compile(r'^[^\\/:*?"<>|]+$')
MAX_DEPTH = 8


def _test_count(folder: Path) -> int:
    try:
        return sum(1 for child in folder.iterdir() if (child / RECORD_FILE).is_file())
    except OSError:
        return 0


class TestFolderTree(QTreeWidget):
    __test__ = False  # not a pytest test class

    folder_changed = Signal(object)  # Path | None

    def __init__(self, library: Library, parent: QWidget | None = None):
        super().__init__(parent)
        self.library = library
        self.setHeaderHidden(True)
        self.setIconSize(QSize(18, 18))
        self.setMinimumWidth(220)
        self.currentItemChanged.connect(lambda *_: self.folder_changed.emit(self.current_folder()))
        self.reload()

    def set_library(self, library: Library) -> None:
        self.library = library
        self.reload()

    # ── content ────────────────────────────────────────────────────────────
    def reload(self, select: Path | None = None) -> None:
        current = Path(select) if select is not None else self.current_folder()
        self.blockSignals(True)
        self.clear()
        for index, root in enumerate(self.library.test_roots()):
            external = index > 0
            name = "Ensayos de la biblioteca" if not external else root.name or str(root)
            item = self._item(root, name, "biblioteca" if not external else "carpeta_externa")
            if not root.is_dir():
                item.setForeground(0, style.ERROR)
                item.setToolTip(0, f"{root}\nNo se encuentra la carpeta")
            self.addTopLevelItem(item)
            self._add_children(item, root, 1)
            item.setExpanded(True)
        target = self._find(current) if current is not None else None
        self.setCurrentItem(target or self.topLevelItem(0))
        self.blockSignals(False)
        self.folder_changed.emit(self.current_folder())

    def _item(self, folder: Path, name: str, icon_name: str) -> QTreeWidgetItem:
        count = _test_count(folder) if folder.is_dir() else 0
        item = QTreeWidgetItem([f"{name}  ({count})" if count else name])
        item.setIcon(0, icon(icon_name, style.PRIMARY.name()))
        item.setData(0, Qt.ItemDataRole.UserRole, str(folder))
        item.setToolTip(0, f"{folder}\n{count} ensayo(s) en esta carpeta")
        return item

    def _add_children(self, parent: QTreeWidgetItem, folder: Path, depth: int) -> None:
        if depth > MAX_DEPTH:
            return
        for child in self.library.test_folders(folder):
            item = self._item(child, child.name, "carpeta")
            parent.addChild(item)
            self._add_children(item, child, depth + 1)

    def _find(self, folder: Path) -> QTreeWidgetItem | None:
        wanted = str(folder)
        stack = [self.topLevelItem(i) for i in range(self.topLevelItemCount())]
        while stack:
            item = stack.pop()
            if item.data(0, Qt.ItemDataRole.UserRole) == wanted:
                return item
            stack.extend(item.child(i) for i in range(item.childCount()))
        return None

    def current_folder(self) -> Path | None:
        item = self.currentItem()
        return Path(item.data(0, Qt.ItemDataRole.UserRole)) if item else None

    def select_folder(self, folder: Path) -> None:
        item = self._find(Path(folder))
        if item is not None:
            self.setCurrentItem(item)

    def is_external_root(self, folder: Path | None) -> bool:
        return folder is not None and Path(folder) in self.library.test_roots()[1:]

    # ── actions ────────────────────────────────────────────────────────────
    def create_folder(self) -> Path | None:
        """Ask for a name and create a sub-folder of the selected folder."""
        parent = self.current_folder() or self.library.tests_folder
        name, ok = QInputDialog.getText(self, "Nueva carpeta", f"Nombre de la carpeta (dentro de {parent.name or parent}):")
        name = name.strip()
        if not ok or not name:
            return None
        if not FOLDER_NAME.match(name) or name in (".", ".."):
            QMessageBox.warning(self, "Nueva carpeta", "Nombre de carpeta no válido.")
            return None
        folder = parent / name
        try:
            folder.mkdir(parents=True, exist_ok=False)
        except FileExistsError:
            QMessageBox.warning(self, "Nueva carpeta", f"Ya existe la carpeta «{name}».")
            return None
        except OSError as exc:
            QMessageBox.warning(self, "Nueva carpeta", str(exc))
            return None
        self.reload(select=folder)
        return folder

    def add_external(self) -> Path | None:
        """Add a folder outside the library (e.g. on a network drive) to the explorer."""
        folder = QFileDialog.getExistingDirectory(self, "Carpeta de ensayos externa", str(Path.home()))
        if not folder:
            return None
        try:
            self.library.add_test_root(Path(folder))
        except OSError as exc:
            QMessageBox.warning(self, "Carpeta externa", str(exc))
            return None
        path = Path(folder).resolve()
        self.reload(select=path if path in self.library.test_roots() else None)
        return path

    def remove_external(self) -> None:
        folder = self.current_folder()
        if not self.is_external_root(folder):
            return
        answer = QMessageBox.question(
            self, "Quitar carpeta externa", f"¿Quitar «{folder}» del explorador? Los ensayos no se borran."
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.library.remove_test_root(folder)
            self.reload()
