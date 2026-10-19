"""Render the main GUI screens offscreen to PNG (visual check / documentation).

    QT_QPA_PLATFORM=offscreen python tools/capturas.py [carpeta_salida]
"""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtWidgets import QApplication

from calelektra.catalog.library import Library
from calelektra.gui.app import EXAMPLE_LIBRARY
from calelektra.gui.main_window import MainWindow
from calelektra.gui.plots import SParameterDialog, VnaCheckDialog
from calelektra.gui.style import apply_theme


def grab(widget, path: Path) -> None:
    QApplication.processEvents()
    widget.grab().save(str(path))
    print("captura", path)


def main() -> None:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "capturas")
    out.mkdir(parents=True, exist_ok=True)
    app = QApplication(sys.argv)
    apply_theme(app)
    library = Library.load(EXAMPLE_LIBRARY)
    window = MainWindow(library)
    window.resize(1600, 980)
    window.show()
    grab(window, out / "01_inicio.png")
    window.navigation.setCurrentRow(1)
    window.inventory.table.selectRow(0)
    grab(window, out / "02_inventario.png")
    window.navigation.setCurrentRow(2)
    grab(window, out / "03_setups.png")
    for name in ("cable_N_26G_alarma", "lisn_impedancia_VNA-01", "sac_nsa_3m", "sac_svswr_3m"):
        editor = window.open_setup(name)
        QApplication.processEvents()
        editor.diagram.fit()
        grab(window, out / f"10_setup_{name}.png")
    editor = window.open_setup("sac_nsa_3m")
    editor._role_clicked("antena_rx")
    grab(window, out / "11_setup_sac_nsa_rol_antena_rx.png")
    dialog = SParameterDialog(library, library.equipment["CAB-PT-N-01"], window)
    dialog.show()
    grab(dialog, out / "20_parametros_s.png")
    check = VnaCheckDialog(library, library.equipment["VNA-01"], window)
    check.show()
    check.device.setCurrentIndex(check.device.findData("PATH-RX-01"))
    check.sweep()
    grab(check, out / "21_vna_dummy.png")
    check.start.setText("9 kHz")
    check.sweep()
    grab(check, out / "22_vna_dummy_fuera_de_rango.png")


if __name__ == "__main__":
    main()
