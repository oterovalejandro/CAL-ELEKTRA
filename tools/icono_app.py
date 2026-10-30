"""Write the files of the icon of the application (``src/calelektra/gui/recursos``).

* ``cal-elektra.ico``: the Windows icon, with an image for each size (16 to 256 px), for the shortcuts;
* ``cal-elektra-<n>.png``: the navy icon, 16 to 256 px (Linux and macOS launchers, documents);
* ``cal-elektra-claro-256.png``: the light one, as inside the application.

The application draws its icon itself (``gui/app_icon.py``): these files are only for what is outside
it. Run it again if the drawing changes.

    QT_QPA_PLATFORM=offscreen python tools/icono_app.py
"""

from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication

from calelektra.gui.app_icon import RESOURCES, write_icon_files


def main() -> None:
    QApplication.instance() or QApplication(sys.argv)
    for path in write_icon_files(RESOURCES):
        print(path.relative_to(RESOURCES.parents[3]))


if __name__ == "__main__":
    main()
