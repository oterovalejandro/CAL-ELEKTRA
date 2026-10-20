"""Render the main GUI screens offscreen to PNG (visual check / documentation).

    QT_QPA_PLATFORM=offscreen python tools/capturas.py [carpeta_salida]

Works on a temporary copy of the example library and first runs two simulated
tests, so the dashboard and the test history have data.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from calelektra.catalog.library import Library
from calelektra.gui.app import EXAMPLE_LIBRARY
from calelektra.gui.equipment_dialogs import EquipmentDialog
from calelektra.gui.main_window import MainWindow
from calelektra.gui.plots import SParameterDialog
from calelektra.gui.style import apply_theme
from calelektra.gui.test_window import NewTestDialog, PromptDialog
from calelektra.procedures.engine import AutoOperator, ProcedureRunner, PromptRequest


def grab(widget, path: Path) -> None:
    QApplication.processEvents()
    QTest.qWait(30)
    widget.grab().save(str(path))
    print("captura", path)


def main() -> None:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "capturas")
    out.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="calelektra_")) / "biblioteca"
    shutil.copytree(EXAMPLE_LIBRARY, work, ignore=shutil.ignore_patterns("ensayos"))
    app = QApplication(sys.argv)
    apply_theme(app)
    library = Library.load(work)
    for procedure, setup in (("cable_parametros_s", "componente_292_tosm"), ("impedancia_lisn", "lisn_impedancia_VNA-BF")):
        ProcedureRunner(library, library.procedures[procedure], library.setups[setup], AutoOperator(), operator_name="Demo", seed=1).run()

    window = MainWindow(library)
    window.resize(1600, 980)
    window.show()
    window._library_changed()
    grab(window, out / "01_inicio.png")

    window.show_section("equipos")
    window.inventory.select("VNA-01")
    grab(window, out / "02_equipos_vna.png")
    window.inventory.select("CAB-DUT-0002")
    grab(window, out / "03_equipos_cable.png")

    window.show_section("catalogo")
    window.catalog._select(("categoria", "vna"))
    grab(window, out / "04_catalogo_categoria_vna.png")
    window.catalog._select(("modelo", "rs_znb43"))
    grab(window, out / "05_catalogo_modelo_znb43.png")

    window.show_section("setups")
    grab(window, out / "06_setups.png")
    window.show_section("procedimientos")
    grab(window, out / "07_procedimientos.png")

    editor = window.open_setup("cable_vna_292")
    editor.diagram.fit()
    editor.bottom_tabs.setCurrentIndex(1)
    grab(window, out / "10_setup_cable_vna_compatibilidad.png")
    editor.bottom_tabs.setCurrentIndex(2)
    grab(window, out / "11_setup_cable_vna_calibracion.png")
    alarm = window.open_setup("cable_vna_N_alarma")
    alarm.diagram.fit()
    alarm.bottom_tabs.setCurrentIndex(1)
    grab(window, out / "12_setup_conectores_alarma.png")
    for name in ("componente_292_tosm", "sac_nsa_3m", "lisn_impedancia_VNA-01"):
        other = window.open_setup(name)
        other.diagram.fit()
        grab(window, out / f"13_setup_{name}.png")

    dialog = NewTestDialog(library, "cable_parametros_s", "cable_vna_292", window)
    dialog.show()
    grab(dialog, out / "20_nuevo_ensayo.png")
    dialog.close()

    prompt = PromptDialog(
        PromptRequest(
            "Calibración UOSM — 1/7: Abierto (Open) en PR1",
            "Conecte el estándar Abierto (Open) hembra del kit «KIT-292-01» (2.92 mm Ideal Kit) en el plano PR1 "
            "(puerto P1 del VNA, 2,92 mm (K) macho).",
            image="open",
        ),
        window,
    )
    prompt.show()
    grab(prompt, out / "21_peticion_operador.png")
    prompt.close()

    test = window.open_test_window("cable_parametros_s", "cable_vna_292", "Demo", automatic=True)
    test.start()
    for _ in range(600):
        QTest.qWait(20)
        if not test.running:
            break
    grab(window, out / "22_ensayo_grafica.png")
    test.tabs.setCurrentIndex(1)
    grab(window, out / "23_ensayo_resultados.png")
    test.tabs.setCurrentIndex(2)
    grab(window, out / "24_ensayo_registro.png")

    window.show_section("ensayos")
    grab(window, out / "25_ensayos.png")

    equipment_dialog = EquipmentDialog(library, library.equipment["VNA-01"], window)
    equipment_dialog.show()
    grab(equipment_dialog, out / "30_editar_equipo_vna.png")
    equipment_dialog.close()
    new_dialog = EquipmentDialog(library, parent=window, model_id="cable_coaxial_292_hh")
    new_dialog.show()
    grab(new_dialog, out / "31_nuevo_equipo_cable.png")
    new_dialog.close()

    s_dialog = SParameterDialog(library, library.equipment["CAB-PT-N-01"], window)
    s_dialog.show()
    grab(s_dialog, out / "32_parametros_s.png")
    window.close()
    shutil.rmtree(work.parent, ignore_errors=True)


if __name__ == "__main__":
    main()
