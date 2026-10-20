"""GUI tests (run offscreen)."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest
import yaml
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QComboBox, QDialog

from calelektra.catalog.library import Library
from calelektra.core.frequency import FrequencyRange
from calelektra.gui import test_window as test_window_module
from calelektra.gui.equipment_dialogs import EquipmentDialog, ModelDialog
from calelektra.gui.main_window import SECTIONS, MainWindow
from calelektra.gui.plots import SParameterDialog, VnaCheckDialog
from calelektra.gui.setup_editor import SetupEditor
from calelektra.gui.test_window import LaunchTestDialog, TestWindow
from calelektra.procedures.engine import ProcedureRunner
from calelektra.setups.model import ConfiguredSetup


@pytest.fixture(scope="session")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(qapp, library: Library):
    win = MainWindow(library)
    win.show()
    qapp.processEvents()
    yield win
    for editor in win._documents(SetupEditor):
        editor.dirty = False
    for test in win._documents(TestWindow):
        test.wait()
    win.close()


def process(qapp):
    for _ in range(3):
        qapp.processEvents()


def wait_for(predicate, timeout_ms: int = 20_000) -> None:
    waited = 0
    while not predicate() and waited < timeout_ms:
        QTest.qWait(20)
        waited += 20
    assert predicate(), "tiempo de espera agotado"


def test_sections_and_ribbon(window, qapp):
    for key, _text, _icon in SECTIONS:
        window.show_section(key)
        process(qapp)
        assert window.current_section() == key
        texts = [a.text() for a in window.ribbon.actions() if a.text()]
        assert "Recargar" in texts and len(texts) > 2, key
    editor = window.open_setup("cable_vna_292")
    process(qapp)
    assert window.workspace.currentWidget() is editor
    assert "Guardar" in [a.text() for a in window.ribbon.actions()]
    assert window.rail_buttons["setups"].isChecked()
    assert "VNA-01" in window.status_instruments.text()


def test_open_every_setup(window, qapp):
    for setup_id in window.library.setups:
        editor = window.open_setup(setup_id)
        process(qapp)
        assert isinstance(editor, SetupEditor)
        assert editor.analysis is not None and editor.diagram.nodes, setup_id
    assert window.open_setup("cable_N_26G_alarma").badge.text().startswith("ALARMA")
    nsa = window.open_setup("sac_nsa_3m")
    assert nsa.analysis.covered
    assert {"vna.P1", "vna.P2", "antena_tx", "soporte_rx"} <= set(nsa.diagram.nodes)
    cable = window.open_setup("cable_vna_292")
    assert cable.state_combo.currentText() == "medida"
    assert {"vna.P1", "vna.P2", "dut"} <= set(cable.diagram.nodes)
    assert cable.diagram.nodes["dut"].spec.lines == ["lo define la plantilla de ensayo"]
    assert cable.calibration_panel.planes.rowCount() == 2
    assert "UOSM" in cable.bottom_tabs.tabText(cable.bottom_tabs.indexOf(cable.calibration_panel))
    assert cable.bottom_tabs.tabText(0) == "Frecuencias (800 puntos)" and "paso 50 MHz" in cable.frequency_summary.text()
    alarm = window.open_setup("cable_vna_N_alarma")
    assert "incidencias" in alarm.bottom_tabs.tabText(alarm.bottom_tabs.indexOf(alarm.checks))
    assert not alarm.test_button.isEnabled()


def _combo_in_role_panel(editor: SetupEditor) -> QComboBox:
    combos = editor.role_panel.assignment_box.findChildren(QComboBox)
    assert combos
    return combos[0]


def test_changing_assignment_clears_the_alarm(window, qapp):
    editor = window.open_setup("lisn_impedancia_VNA-01")
    assert not editor.analysis.covered
    editor._role_clicked("vna")
    combo = _combo_in_role_panel(editor)
    combo.setCurrentIndex(combo.findData("VNA-BF-01"))
    process(qapp)
    assert editor.setup.assignments["vna"] == "VNA-BF-01"
    assert editor.analysis.covered and editor.dirty
    assert window.library.setups["lisn_impedancia_VNA-01"].assignments["vna"] == "VNA-01"


def test_required_range_edit(window, qapp):
    editor = window.open_setup("cable_N_26G_alarma")
    editor.stop_edit.setText("18 GHz")
    editor._range_edited()
    process(qapp)
    assert editor.setup.required_range == FrequencyRange(100e3, 18e9)
    assert editor.analysis.covered
    editor.stop_edit.setText("dieciocho")
    editor._range_edited()
    assert "f9d6d2" in editor.stop_edit.styleSheet()


def test_new_setup_save_and_reload(window, qapp, library_copy):
    setup = ConfiguredSetup(id="nuevo_cable", name="Nuevo cable", template="cable_vna", required_range=FrequencyRange(10e6, 40e9))
    editor = window.open_new_setup(setup)
    editor.setup.assignments.update({"vna": "VNA-01", "kit_cal": "KIT-292-01", "dut": "CAB-DUT-0002"})
    editor.refresh()
    assert editor.analysis.ok and editor.analysis.covered
    editor._node_moved("dut", 10.0, 20.0)
    assert editor.save()
    data = yaml.safe_load((library_copy / "hardware_setups/configurados/nuevo_cable.yaml").read_text(encoding="utf-8"))
    assert data["formato"] == "calelektra/setup@0.3"
    assert data["rango_requerido"] == ["10 MHz", "40 GHz"]
    assert data["disposicion"]["dut"] == [10.0, 20.0]
    assert "nuevo_cable" in Library.load(library_copy).setups


def test_new_equipment_from_category_fields(window, qapp, library_copy):
    dialog = EquipmentDialog(window.library, parent=window, model_id="cable_coaxial_292_hh")
    assert dialog.category.currentData() == "cable"
    assert {"longitud_m", "conector_a", "rango_frecuencia"} <= set(dialog.form.editors)
    assert "modelo:" in dialog.form.editors["conector_a"].family.itemText(0)  # inherited from the model
    dialog.id_edit.setText("CAB-DUT-0099")
    dialog.serial.setText("SN-99")
    dialog.form.editors["longitud_m"].edit.setText("1,25")
    dialog.form.editors["rango_frecuencia"].high.setText("26.5 GHz")
    dialog.form.editors["rango_frecuencia"].low.setText("DC")
    dialog._save()
    assert dialog.result() == QDialog.DialogCode.Accepted
    data = yaml.safe_load((library_copy / "catalogo/equipos/CAB-DUT-0099.yaml").read_text(encoding="utf-8"))
    assert data["campos"] == {"rango_frecuencia": ["DC", "26.5 GHz"], "longitud_m": 1.25}
    reloaded = Library.load(library_copy)
    assert reloaded.load_issues == []
    assert reloaded.frequency_range_of(reloaded.equipment["CAB-DUT-0099"]).range == FrequencyRange(0, 26.5e9)


def test_equipment_dialog_validates_fields(window, qapp):
    dialog = EquipmentDialog(window.library, window.library.equipment["CAB-DUT-0002"], window)
    dialog.form.editors["longitud_m"].edit.setText("larga")
    with pytest.raises(Exception, match="Longitud"):
        dialog.build()
    dialog.form.editors["longitud_m"].edit.setText("0,75")
    dialog.s_reference.setText("no aplica")
    equipment = dialog.build()
    assert equipment.values["longitud_m"] == 0.75 and equipment.id == "CAB-DUT-0002"
    vna = EquipmentDialog(window.library, window.library.equipment["VNA-01"], window)
    assert vna.tabs.isTabVisible(vna.connection_tab_index) and vna.simulated.isChecked()
    assert set(vna.form.editors) == {"conector_puertos", "opciones_instaladas"}  # model-level fields stay in the model


def test_new_model_requires_model_fields(window, qapp, library_copy):
    dialog = ModelDialog(window.library, category_id="kit_calibracion", parent=window)
    dialog.id_edit.setText("kit_35_tosm")
    dialog.manufacturer.setText("Genérico")
    dialog.designation.setText("Kit 3,5 mm")
    with pytest.raises(Exception, match="obligatorios"):
        dialog.build()
    editors = dialog.form.editors
    editors["rango_frecuencia"].low.setText("DC")
    editors["rango_frecuencia"].high.setText("26.5 GHz")
    editors["familia_conector"].combo.setCurrentIndex(editors["familia_conector"].combo.findData("3.5 mm"))
    editors["metodos"].multi.set_selected(["OSM", "TOSM"])
    editors["nombre_kit_vna"].edit.setText("3.5 mm Ideal Kit")
    dialog._save()
    data = yaml.safe_load((library_copy / "catalogo/modelos/kit_35_tosm.yaml").read_text(encoding="utf-8"))
    assert data["categoria"] == "kit_calibracion" and data["campos"]["familia_conector"] == "3.5 mm"
    assert data["campos"]["metodos"] == ["OSM", "TOSM"]


def test_setup_frequencies_are_edited_in_the_setup(window, qapp, library_copy):
    editor = window.open_setup("cable_vna_292")
    frequencies = editor.frequency_editor
    assert frequencies.definition.currentData() == "paso" and frequencies.step.text() == "50 MHz"
    assert "800 puntos" in frequencies.preview.text()
    frequencies.mode.setCurrentIndex(frequencies.mode.findData("logaritmico"))
    frequencies.points.setValue(401)
    process(qapp)
    QTest.qWait(10)
    assert editor.setup.frequencies.mode == "logaritmico" and editor.dirty
    assert frequencies.form.isRowVisible(frequencies.points) and not frequencies.form.isRowVisible(frequencies.step)
    frequencies.mode.setCurrentIndex(frequencies.mode.findData("lista"))
    assert frequencies.use_list(library_copy / "hardware_setups/listas/puntos_cable_N.csv")
    process(qapp)
    QTest.qWait(10)
    assert editor.setup.frequencies.list_file == "../listas/puntos_cable_N.csv"
    assert editor.setup.required_range == FrequencyRange(30e6, 18e9)  # the range of the list
    assert "268 puntos" in frequencies.preview.text()
    assert editor.save()
    data = yaml.safe_load((library_copy / "hardware_setups/configurados/cable_vna_292.yaml").read_text(encoding="utf-8"))
    assert data["frecuencias"]["modo"] == "lista" and data["rango_requerido"] == ["30 MHz", "18 GHz"]


def test_model_specifications_are_edited_in_the_catalogue(window, qapp, library_copy):
    dialog = ModelDialog(window.library, window.library.models["cable_coaxial_292_hh"], parent=window)
    assert dialog.specs.table.rowCount() == 2 and dialog.specs.table.item(0, 2).text() == "S21, S12"
    dialog.specs.table.selectRow(1)
    dialog.specs._remove()
    assert dialog.specs.add_file(library_copy / "catalogo/especificaciones/cable_N_2m_referencia.s2p")
    dialog.specs.table.item(1, 2).setText("S11, S33")
    with pytest.raises(Exception, match="S33"):
        dialog.build()
    dialog.specs.table.item(1, 2).setText("S21")
    dialog._save()
    data = yaml.safe_load((library_copy / "catalogo/modelos/cable_coaxial_292_hh.yaml").read_text(encoding="utf-8"))
    assert [spec["fichero"] for spec in data["especificaciones_s"]] == [
        "../especificaciones/cable_292_0m5_mascara.s2p",
        "../especificaciones/cable_N_2m_referencia.s2p",
    ]


def test_template_calls_a_setup_and_defines_the_eut(window, qapp, library_copy):
    view = window.procedures_view
    window.show_section("procedimientos")
    view.reload(select="cable_CAB-DUT-0002")
    editor = view.editor
    assert editor.setup.currentData() == "cable_vna_292" and editor.eut.currentData() == "CAB-DUT-0002"
    assert "dut" in editor.diagram.nodes and "CAB-DUT-0002" in " ".join(editor.diagram.nodes["dut"].spec.lines)
    assert editor.specs.table.rowCount() == 2 and editor.specs.read_only  # specifications of the model of the EUT
    assert editor.plan.ok and "800 puntos" in editor.check_summary.text()
    setups = {editor.setup.itemData(i) for i in range(editor.setup.count())}
    assert "componente_292_tosm" in setups and "lisn_impedancia_VNA-BF" not in setups  # setups for 2 ports
    editor.setup.setCurrentIndex(editor.setup.findData("cable_vna_N_alarma"))
    editor.eut.setCurrentIndex(editor.eut.findData("CAB-DUT-0001"))
    editor.refresh_check()
    assert not editor.plan.ok and "familias incompatibles" in " ".join(i.message for i in editor.plan.issues)
    assert view.dirty and not view.action_launch.isEnabled()
    editor.setup.setCurrentIndex(editor.setup.findData("cable_vna_292"))
    editor.eut.setCurrentIndex(editor.eut.findData("CAB-DUT-0002"))
    editor.traces.per_trace.setChecked(True)
    editor.traces.table.item(2, 0).setText("300 Hz")  # S21
    assert view.save()
    data = yaml.safe_load((library_copy / "procedimientos/cable_CAB-DUT-0002.yaml").read_text(encoding="utf-8"))
    assert data["hardware_setup"] == "cable_vna_292" and data["eut"] == "CAB-DUT-0002"
    assert data["barrido"]["trazas"]["S21"]["ifbw"] == "300 Hz" and "modo" not in data["barrido"]
    assert view.action_launch.isEnabled()


def test_new_template_from_a_setup(window, qapp):
    editor = window.open_setup("lisn_impedancia_VNA-BF")
    editor.test_button.click()
    process(qapp)
    view = window.procedures_view
    assert window.current_section() == "procedimientos" and view.is_new
    assert view.editor.algorithm.currentData() == "vna_1_puerto" and view.editor.setup.currentData() == "lisn_impedancia_VNA-BF"
    view.editor.eut.setCurrentIndex(view.editor.eut.findData("LISN-0001"))
    view.editor.refresh_check()
    assert view.editor.plan.ok


def _choose(combo: QComboBox, data) -> None:
    index = combo.findData(data)
    assert index >= 0, data
    combo.setCurrentIndex(index)


def test_launch_dialog_precheck(window, qapp):
    library = window.library
    broken = library.procedures["cable_CAB-DUT-0002"].model_copy(deep=True, update={"id": "roto", "setup_id": "cable_vna_N_alarma", "eut_id": "CAB-DUT-0001"})
    library.procedures["roto"] = broken
    dialog = LaunchTestDialog(library, "roto", parent=window)
    assert dialog.plan is not None and not dialog.plan.ok and not dialog.accept_button.isEnabled()
    assert "familias incompatibles" in " ".join(i.message for i in dialog.plan.issues)
    _choose(dialog.procedure, "cable_CAB-DUT-0002")
    assert dialog.plan.ok and dialog.accept_button.isEnabled() and dialog.automatic.isEnabled()
    assert dialog.name.text().startswith("CAB-DUT-0002 ")
    assert "dut" in dialog.diagram.nodes and "800 puntos" in dialog.summary.text()


def test_launch_and_run_automatic_test(window, qapp, library_copy, monkeypatch):
    folder = window.library.tests_folder / "Clientes"
    folder.mkdir(parents=True)
    window.tests_view.folders.reload(select=folder)

    def accept(dialog):
        assert dialog.current_folder() == folder
        dialog.name.setText("Cable cliente 2001")
        dialog.automatic.setChecked(True)
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(test_window_module.LaunchTestDialog, "exec", accept)
    window.show_section("ensayos")
    test = window.launch_test("cable_CAB-DUT-0002")
    wait_for(lambda: not test.running)
    record = test.record
    assert record.status == "completado" and record.verdict == "PASA" and record.folder == folder / "Cable_cliente_2001"
    assert test.plot.datasets and test.plot.limits and test.spec_table.rowCount() == 4  # 2 specs × 2 parameters
    assert test.plot.smoothing == {"S11": 1.0, "S22": 1.0} and test.plot.smooth_box.isVisible()
    assert "PASA" in test.tabs.tabText(1) and test.start_button.text() == "Repetir ensayo"
    assert window.tests_view.table.rowCount() == 1
    assert window.home.tile_tests.value.text() == "1"


def test_test_with_operator_dialogs(window, qapp, monkeypatch):
    shown = []

    def answer(dialog):
        shown.append(dialog.windowTitle())
        return QDialog.DialogCode.Accepted

    library = window.library
    monkeypatch.setattr(test_window_module.PromptDialog, "exec", answer)
    test = window._add_test_window(TestWindow(library, library.procedures["lisn_LISN-0001"], library.tests_folder, "LISN 1", "prueba"))
    assert test.badge.text() == "Listo para lanzar"
    test.start()
    wait_for(lambda: not test.running)
    first = test.record
    assert first.status == "completado"
    assert len(shown) == 3 + 1 + 2  # OSM standards, match check, two LISN lines
    assert "Abierto" in shown[0]

    monkeypatch.setattr(test_window_module.PromptDialog, "exec", lambda dialog: QDialog.DialogCode.Rejected)
    test.start()  # «Repetir»: the template again, a new test in the same folder
    wait_for(lambda: not test.running)
    assert test.record.status == "abortado" and test.record.folder != first.folder
    assert test.record.title == "LISN 1 (repetición)"
    assert len(library.tests()) == 2


def test_tests_explorer_and_finished_test(window, qapp):
    library = window.library
    result = ProcedureRunner(library, library.procedures["cable_CAB-DUT-0003"], folder=library.tests_folder / "2026", name="Cable largo", seed=3).run()
    record = result.record
    assert record.verdict == "FALLA"
    view = window.tests_view
    window.show_section("ensayos")
    view.show_test(record.folder)
    assert view.folders.current_folder() == library.tests_folder / "2026"
    assert view.table.rowCount() == 1 and view.detail.record.folder == record.folder
    assert "FALLA" in view.detail.specs.text() and view.detail.plot.limits
    assert list(view.detail.plot.datasets) == ["DUT"]  # the measurement, not the specification files kept with it
    frequencies, s = view.detail.plot.datasets["DUT"]
    assert len(frequencies) == 800 and 20 * np.log10(abs(s[-1, 1, 0])) < -7.5  # 1,2 m cable: about 8 dB at 40 GHz
    assert view.action_repeat.isEnabled()
    test = window.open_test_folder(record.folder)
    assert test.record.verdict == "FALLA" and test.start_button.text() == "Repetir ensayo"
    assert test.plot.datasets and test.spec_table.rowCount() == 4


def test_dialogs(window, qapp):
    library = window.library
    SParameterDialog(library, library.equipment["CAB-PT-N-01"], window).show()
    check = VnaCheckDialog(library, library.equipment["VNA-01"], window)
    check.show()
    check.sweep()
    assert "completado" in check.status.text()
    check.start.setText("9 kHz")
    check.sweep()
    assert "fuera de las especificaciones" in check.status.text()
    assert "configurar_barrido" in check.log.toPlainText()
    check.close()
