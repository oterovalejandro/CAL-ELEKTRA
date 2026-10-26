"""Render the main GUI screens offscreen to PNG (visual check / documentation).

    QT_QPA_PLATFORM=offscreen python tools/capturas.py [carpeta_salida]

Works on a temporary copy of the example library and first launches the
example test templates in folders, so the dashboard and the test explorer
have data. The LISN template uses the E5061B, whose certificate expired on
2026-07-30: it is launched on a day when the calibration was valid. A
synthetic workbook of the TPH probe gives the ambient conditions of all the
tests but one, whose uncertainty stays pending.
"""

from __future__ import annotations

import math
import shutil
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

from PySide6.QtTest import QTest
from PySide6.QtWidgets import QAbstractItemView, QApplication, QMessageBox

from calelektra.catalog.library import Library
from calelektra.gui.app import EXAMPLE_LIBRARY
from calelektra.gui.equipment_dialogs import EquipmentDialog, ModelDialog
from calelektra.gui.main_window import MainWindow
from calelektra.gui.plots import SParameterDialog
from calelektra.gui.style import apply_theme
from calelektra.gui.test_window import LaunchTestDialog, PromptDialog, TestWindow
from calelektra.procedures.ambient import AmbientReading, write_tph_workbook
from calelektra.procedures.engine import AutoOperator, ProcedureRunner, PromptRequest
from calelektra.procedures.record_uncertainty import load_ambient_conditions


def grab(widget, path: Path) -> None:
    QApplication.processEvents()
    QTest.qWait(30)
    widget.grab().save(str(path))
    print("captura", path)


def wait(test: TestWindow) -> None:
    for _ in range(600):
        QTest.qWait(20)
        if not test.running:
            break


def probe_workbook(path: Path, start: datetime, end: datetime) -> Path:
    """Synthetic export of the TPH probe: a reading per minute, 22,8 – 23,6 °C."""
    readings = []
    time = start.replace(second=45, microsecond=0) - timedelta(minutes=10)
    while time <= end:
        i = len(readings)
        readings.append(AmbientReading(time, round(23.2 + 0.4 * math.sin(i / 9), 1), round(942.6 + 0.3 * math.sin(i / 40), 1), round(37 + 1.5 * math.cos(i / 15), 1)))
        time += timedelta(minutes=1)
    return write_tph_workbook(path, readings, "VIRTUAL LAB: Datos sonda TPH")


def main() -> None:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "capturas")
    out.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="calelektra_")) / "biblioteca"
    shutil.copytree(EXAMPLE_LIBRARY, work, ignore=shutil.ignore_patterns("ensayos"))
    app = QApplication(sys.argv)
    apply_theme(app)
    library = Library.load(work)
    kit = library.equipment["KIT-N-01"]  # «caduca pronto» in the example: keep it so whatever the date
    kit.calibration.due = date.today() + timedelta(days=20)
    library.save_equipment(kit)
    client = library.tests_folder / "2026" / "Cliente ACME"
    lisn = None
    records = []
    operators = {}
    for folder, name, template, today in (
        (client, "Cable 2,92 mm nº 2001", "cable_CAB-DUT-0002", None),
        (client, "Cable 2,92 mm nº 3001", "cable_CAB-DUT-0003", None),
        (client, "Cable N nº 1234", "cable_CAB-DUT-0001", None),
        (library.tests_folder / "2026" / "LISN", "LISN-0001 verificación anual", "lisn_LISN-0001", date(2026, 7, 15)),
        (client, "Pérdidas cable N nº 1234", "transmision_CAB-DUT-0001", None),
        (client, "Acoplador nº 9001", "acoplador_ACP-0001", None),
        (client, "Cable 2,92 mm nº 4001 (por fórmula)", "cable_CAB-DUT-0004", None),
        (client, "Cable SUCOFLEX_118_A nº 5001", "cable_CAB-DUT-0005", None),
    ):
        operators[template] = AutoOperator()
        result = ProcedureRunner(
            library, library.procedures[template], operators[template], operator_name="Demo", seed=1, folder=folder, name=name, today=today,
        ).run()
        records.append(result.record)
        if template.startswith("lisn"):
            lisn = result.record
    workbook = probe_workbook(work.parent / "Condiciones_ambientales.xlsx", min(r.started for r in records), datetime.now() + timedelta(hours=1))
    for record in records:
        if "3001" not in record.title:  # this one keeps its uncertainty pending
            load_ambient_conditions(library, record, workbook)

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
    window.inventory.select("CAB-PT-NMD-02")
    grab(window, out / "03b_equipos_cable_nmd.png")
    window.inventory.select("KIT-N-02")
    grab(window, out / "03c_equipos_kit_aa180.png")

    window.show_section("catalogo")
    window.catalog._select(("categoria", "vna"))
    grab(window, out / "04_catalogo_categoria_vna.png")
    window.catalog._select(("modelo", "cable_coaxial_292_hh"))
    grab(window, out / "05_catalogo_modelo_cable.png")
    model_dialog = ModelDialog(library, library.models["cable_coaxial_292_hh"], parent=window)
    model_dialog.show()
    scroll = model_dialog.fields_holder.parentWidget().parentWidget()
    scroll.verticalScrollBar().setValue(scroll.verticalScrollBar().maximum())
    grab(model_dialog, out / "05b_modelo_especificaciones.png")
    model_dialog.close()

    window.show_section("setups")
    grab(window, out / "06_setups.png")
    editor = window.open_setup("cable_vna_292")
    editor.diagram.fit()
    editor.bottom_tabs.setCurrentIndex(0)
    grab(window, out / "10_setup_frecuencias.png")
    editor.bottom_tabs.setCurrentWidget(editor.checks)
    grab(window, out / "10b_setup_cable_vna_compatibilidad.png")
    editor.bottom_tabs.setCurrentWidget(editor.calibration_panel)
    grab(window, out / "11_setup_cable_vna_calibracion.png")
    listed = window.open_setup("cable_N_18G")
    listed.diagram.fit()
    listed.bottom_tabs.setCurrentIndex(0)
    grab(window, out / "11b_setup_lista_frecuencias.png")
    alarm = window.open_setup("cable_vna_N_alarma")
    alarm.diagram.fit()
    alarm.bottom_tabs.setCurrentWidget(alarm.checks)
    grab(window, out / "12_setup_conectores_alarma.png")
    for name in ("componente_292_tosm", "sac_nsa_3m", "lisn_impedancia_VNA-01", "acoplador_N_18G", "transmision_N_18G"):
        other = window.open_setup(name)
        other.diagram.fit()
        grab(window, out / f"13_setup_{name}.png")
    paths = window.open_setup("acoplador_N_18G")
    paths.diagram.fit()
    paths._role_clicked("P1-2")
    paths.bottom_tabs.setCurrentWidget(paths.checks)
    grab(window, out / "14_setup_camino_P1.png")
    transmission = window.open_setup("transmision_N_18G")
    transmission.diagram.fit()
    transmission.state_combo.setCurrentIndex(transmission.state_combo.findText("normalizacion"))
    transmission.bottom_tabs.setCurrentWidget(transmission.calibration_panel)
    grab(window, out / "14b_setup_transmision_normalizacion.png")

    window.show_section("procedimientos")
    window.procedures_view.reload(select="cable_CAB-DUT-0002")
    grab(window, out / "07_plantilla_ensayo.png")
    scroll = window.procedures_view.editor.parentWidget().parentWidget()
    scroll.verticalScrollBar().setValue(scroll.verticalScrollBar().maximum())
    grab(window, out / "08_plantilla_trazas.png")
    scroll.verticalScrollBar().setValue(0)
    window.procedures_view.reload(select="acoplador_ACP-0001")
    grab(window, out / "07b_plantilla_acoplador.png")
    window.procedures_view.reload(select="cable_CAB-DUT-0004")
    editor = window.procedures_view.editor
    scroll.ensureWidgetVisible(editor.cable_box)
    grab(window, out / "07c_plantilla_cable_formula.png")
    scroll.verticalScrollBar().setValue(0)
    model_dialog = ModelDialog(library, library.models["cable_coaxial_292_hh_k"], parent=window)
    model_dialog.show()
    model_scroll = model_dialog.fields_holder.parentWidget().parentWidget()
    model_scroll.verticalScrollBar().setValue(model_scroll.verticalScrollBar().maximum())
    grab(model_dialog, out / "05c_modelo_cable_formula.png")
    model_dialog.close()
    model_dialog = ModelDialog(library, library.models["hs_sucoflex_118_a"], parent=window)
    model_dialog.show()
    grab(model_dialog, out / "05d_modelo_cable_hoja_datos_rf.png")
    model_scroll = model_dialog.fields_holder.parentWidget().parentWidget()
    model_scroll.verticalScrollBar().setValue(model_scroll.verticalScrollBar().maximum() // 2)
    grab(model_dialog, out / "05e_modelo_cable_hoja_datos_mecanica.png")
    model_dialog.close()

    dialog = LaunchTestDialog(library, "cable_CAB-DUT-0002", client, parent=window)
    dialog.show()
    dialog.name.setText("Cable 2,92 mm nº 2001 (recalibración)")
    dialog._name_typed("")
    grab(dialog, out / "20_lanzar_ensayo.png")
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
    prompt = PromptDialog(next(r for r in operators["cable_CAB-DUT-0005"].requests if r.step == "medida"), window)
    prompt.show()
    grab(prompt, out / "21b_peticion_radio_curvatura.png")
    prompt.close()

    test = window._add_test_window(
        TestWindow(library, library.procedures["cable_CAB-DUT-0002"], client, "Cable 2,92 mm nº 2001 (recalibración)", "Demo", automatic=True)
    )
    grab(window, out / "22_ensayo_listo.png")
    test.start()
    wait(test)
    test.tabs.setCurrentIndex(0)
    grab(window, out / "23_ensayo_grafica.png")
    test.plot.mode.setCurrentIndex(test.plot.mode.findData("fase"))
    test.plot.plot.setXRange(0, 3e9)
    grab(window, out / "23b_ensayo_incertidumbre_fase.png")
    test.plot.mode.setCurrentIndex(test.plot.mode.findData("vswr_db"))
    test.plot.plot.getPlotItem().getViewBox().autoRange()
    grab(window, out / "23c_ensayo_vswr_magnitud.png")
    test.plot.mode.setCurrentIndex(test.plot.mode.findData("db"))
    test.plot.plot.getPlotItem().getViewBox().autoRange()
    test.tabs.setCurrentIndex(1)
    grab(window, out / "24_ensayo_pasa_falla.png")
    test.tabs.setCurrentIndex(2)
    grab(window, out / "25_ensayo_hardware_setup.png")
    test.tabs.setCurrentIndex(3)
    grab(window, out / "26_ensayo_resultados.png")
    test.load_conditions(workbook)
    for box in window.findChildren(QMessageBox):
        grab(box, out / "26b_condiciones_cargadas.png")
        box.close()
    grab(window, out / "26c_ensayo_con_condiciones.png")

    window.show_section("ensayos")
    failed = next(r for r in library.tests() if r.verdict == "FALLA")
    window.tests_view.show_test(failed.folder)
    grab(window, out / "27_explorador_ensayos.png")
    pending = next(r for r in library.tests() if "3001" in r.title)
    window.tests_view.show_test(pending.folder)
    grab(window, out / "27b_explorador_incertidumbre_pendiente.png")
    coupler = next(r for r in library.tests() if r.template_id == "acoplador_ACP-0001")
    coupler_window = window._add_test_window(TestWindow(library, record=coupler))
    coupler_window.tabs.setCurrentIndex(1)
    grab(window, out / "27c_acoplador_pasa_falla.png")
    coupler_window.tabs.setCurrentIndex(0)
    grab(window, out / "27d_acoplador_grafica.png")
    coupler_window.plot.mode.setCurrentIndex(coupler_window.plot.mode.findData("vswr_db"))
    grab(window, out / "27g_acoplador_vswr.png")
    coupler_window.plot.mode.setCurrentIndex(coupler_window.plot.mode.findData("db"))
    window.show_section("ensayos")
    window.tests_view.show_test(coupler.folder)
    grab(window, out / "27f_explorador_acoplador.png")
    formula = next(r for r in library.tests() if r.template_id == "cable_CAB-DUT-0004")
    formula_window = window._add_test_window(TestWindow(library, record=formula))
    formula_window.tabs.setCurrentIndex(0)
    grab(window, out / "27h_cable_formula_grafica.png")
    formula_window.plot.mode.setCurrentIndex(formula_window.plot.mode.findData("vswr_db"))
    grab(window, out / "27i_cable_formula_vswr.png")
    formula_window.plot.mode.setCurrentIndex(formula_window.plot.mode.findData("db"))
    formula_window.tabs.setCurrentIndex(1)
    grab(window, out / "27j_cable_formula_pasa_falla.png")
    window._add_test_window(
        TestWindow(library, library.procedures["cable_CAB-DUT-0005"], client, "Cable SUCOFLEX_118_A nº 5001 (recalibración)", "Demo", automatic=True)
    )
    grab(window, out / "27k_cable_retardo_comprobacion.png")
    delay = next(r for r in library.tests() if r.template_id == "cable_CAB-DUT-0005")
    delay_window = window._add_test_window(TestWindow(library, record=delay))
    delay_window.tabs.setCurrentIndex(3)
    table = delay_window.results
    last = max(row for row in range(table.rowCount()) if table.item(row, 0).text().startswith("retardo nominal"))
    table.scrollToItem(table.item(last, 0), QAbstractItemView.ScrollHint.PositionAtBottom)
    grab(window, out / "27l_cable_retardo_resultados.png")
    transmission_record = next(r for r in library.tests() if r.template_id == "transmision_CAB-DUT-0001")
    transmission_window = window._add_test_window(TestWindow(library, record=transmission_record))
    transmission_window.tabs.setCurrentIndex(3)
    grab(window, out / "27e_transmision_resultados.png")
    lisn_window = window._add_test_window(TestWindow(library, record=lisn))
    lisn_window.tabs.setCurrentIndex(0)
    grab(window, out / "28_lisn_impedancia.png")
    lisn_window.tabs.setCurrentIndex(3)
    lisn_window.results.scrollToBottom()
    grab(window, out / "29_resultados_incertidumbre.png")

    equipment_dialog = EquipmentDialog(library, library.equipment["VNA-01"], window)
    equipment_dialog.show()
    grab(equipment_dialog, out / "30_editar_equipo_vna.png")
    equipment_dialog.tabs.setCurrentIndex(1)
    grab(equipment_dialog, out / "30b_vna_incertidumbres.png")
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
