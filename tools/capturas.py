"""Render the main GUI screens offscreen to PNG (visual check / documentation).

    QT_QPA_PLATFORM=offscreen python tools/capturas.py [carpeta_salida]

Works on a temporary copy of the example library and first creates and runs
some simulated tests in folders, so the dashboard and the test explorer have data.
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
from calelektra.procedures.record import TestDefinition


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
    client = library.tests_folder / "2026" / "Cliente ACME"
    for folder, name, procedure, setup, dut, run in (
        (client, "Cable 2,92 mm 0,5 m nº 2001", "cable_292_pasa_falla", "cable_vna_292", "CAB-DUT-0002", True),
        (client, "Cable 2,92 mm 1,2 m nº 3001", "cable_292_pasa_falla", "componente_292_tosm", "CAB-DUT-0003", True),
        (client, "Cable N nº 1234 (pendiente)", "cable_N_lista", "cable_N_18G", "CAB-DUT-0001", False),
        (library.tests_folder / "2026" / "LISN", "LISN-0001 verificación anual", "impedancia_lisn", "lisn_impedancia_VNA-BF", "LISN-0001", True),
    ):
        definition = TestDefinition.from_template(library.procedures[procedure], setup, dut)
        record = library.create_test(folder, name, definition, "Demo")
        if run:
            ProcedureRunner.for_test(library, record, AutoOperator(), seed=1).run()

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
    window.procedures_view.reload(select="cable_292_pasa_falla")
    grab(window, out / "07_plantilla_ensayo.png")
    scroll = window.procedures_view.editor.parentWidget().parentWidget()
    scroll.verticalScrollBar().setValue(scroll.verticalScrollBar().maximum())
    grab(window, out / "08_plantilla_especificaciones.png")
    window.procedures_view.reload(select="cable_N_lista")
    scroll.verticalScrollBar().setValue(0)
    grab(window, out / "09_plantilla_lista_csv.png")

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

    dialog = NewTestDialog(library, "cable_292_pasa_falla", "cable_vna_292", client, parent=window)
    dialog.show()
    dialog.dut.setCurrentIndex(dialog.dut.findData("CAB-DUT-0002"))
    dialog.name.setText("Cable 2,92 mm 0,5 m nº 2002")
    dialog._name_typed("")
    grab(dialog, out / "20_nuevo_ensayo.png")
    dialog.tabs.setCurrentIndex(1)
    grab(dialog, out / "20b_nuevo_ensayo_especificaciones.png")
    dialog.setup.setCurrentIndex(dialog.setup.findData("cable_vna_N_alarma"))
    dialog.dut.setCurrentIndex(dialog.dut.findData("CAB-DUT-0001"))
    dialog.tabs.setCurrentIndex(0)
    grab(dialog, out / "20c_nuevo_ensayo_alarma.png")
    dialog.setup.setCurrentIndex(dialog.setup.findData("cable_vna_292"))
    dialog.dut.setCurrentIndex(dialog.dut.findData("CAB-DUT-0002"))
    dialog.automatic.setChecked(True)
    dialog._accept()
    created = dialog.created

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

    test = window.open_test_record(created, automatic=True)
    grab(window, out / "22_ensayo_preparado.png")
    test.start()
    for _ in range(600):
        QTest.qWait(20)
        if not test.running:
            break
    test.tabs.setCurrentIndex(0)
    grab(window, out / "23_ensayo_grafica.png")
    test.tabs.setCurrentIndex(1)
    grab(window, out / "24_ensayo_pasa_falla.png")
    test.tabs.setCurrentIndex(2)
    grab(window, out / "25_ensayo_hardware_setup.png")
    test.tabs.setCurrentIndex(3)
    grab(window, out / "26_ensayo_resultados.png")

    window.show_section("ensayos")
    failed = next(r for r in library.tests() if r.verdict == "FALLA")
    window.tests_view.show_test(failed.folder)
    grab(window, out / "27_explorador_ensayos.png")

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
