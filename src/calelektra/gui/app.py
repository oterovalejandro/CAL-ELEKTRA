"""Entry point of the GUI: ``python -m calelektra [carpeta_biblioteca]``."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox

from calelektra.catalog.library import Library, LibraryError
from calelektra.gui.main_window import MainWindow
from calelektra.gui.style import apply_theme

EXAMPLE_LIBRARY = Path(__file__).resolve().parents[3] / "biblioteca_ejemplo"


def default_library_path(argv: list[str]) -> Path | None:
    if len(argv) > 1:
        return Path(argv[1])
    if os.environ.get("CALELEKTRA_BIBLIOTECA"):
        return Path(os.environ["CALELEKTRA_BIBLIOTECA"])
    return EXAMPLE_LIBRARY if EXAMPLE_LIBRARY.is_dir() else None


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv if argv is None else argv
    app = QApplication(argv)
    app.setApplicationName("CAL-ELEKTRA")
    apply_theme(app)
    path = default_library_path(argv)
    if path is None:
        folder = QFileDialog.getExistingDirectory(None, "Carpeta de la biblioteca de CAL-ELEKTRA")
        if not folder:
            return 1
        path = Path(folder)
    try:
        library = Library.load(path)
    except LibraryError as exc:
        QMessageBox.critical(None, "CAL-ELEKTRA", str(exc))
        return 1
    window = MainWindow(library)
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
