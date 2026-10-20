"""GUI tests (run offscreen)."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

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
from calelektra.gui.test_window import NewTestDialog, TestWindow
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
    assert cable.calibration_panel.planes.rowCount() == 2
    assert "UOSM" in cable.bottom_tabs.tabText(2)
    alarm = window.open_setup("cable_vna_N_alarma")
    assert "incidencias" in alarm.bottom_tabs.tabText(1)
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


def test_procedure_editor_saves(window, qapp, library_copy):
    view = window.procedures_view
    window.show_section("procedimientos")
    view.reload(select="cable_parametros_s")
    view.editor.points.setValue(1601)
    assert view.dirty and view.action_save.isEnabled()
    assert view.save()
    data = yaml.safe_load((library_copy / "procedimientos/cable_parametros_s.yaml").read_text(encoding="utf-8"))
    assert data["barrido"]["puntos"] == 1601 and data["plantillas_compatibles"] == ["cable_vna", "componente_2p"]


def test_new_test_dialog_precheck(window, qapp):
    dialog = NewTestDialog(window.library, "cable_parametros_s", "cable_vna_N_alarma", window)
    assert dialog.plan is not None and not dialog.plan.ok and not dialog.start.isEnabled()
    dialog = NewTestDialog(window.library, "cable_parametros_s", "cable_vna_292", window)
    assert dialog.plan.ok and dialog.start.isEnabled() and dialog.automatic.isEnabled()
    setups = {dialog.setups.item(i).data(256) for i in range(dialog.setups.count())}
    assert "lisn_impedancia_VNA-BF" not in setups  # not compatible with the procedure


def test_automatic_simulated_test(window, qapp):
    test = window.open_test_window("cable_parametros_s", "cable_vna_292", "prueba", automatic=True)
    test.start()
    wait_for(lambda: not test.running)
    assert test.result.status == "completado"
    assert test.plot.datasets and test.results.rowCount() > 5
    assert window.tests_view.table.rowCount() == 1
    assert window.home.tile_tests.value.text() == "1"


def test_test_with_operator_dialogs(window, qapp, monkeypatch):
    shown = []

    def answer(dialog):
        shown.append(dialog.windowTitle())
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(test_window_module.PromptDialog, "exec", answer)
    test = window.open_test_window("impedancia_lisn", "lisn_impedancia_VNA-BF", "prueba")
    test.start()
    wait_for(lambda: not test.running)
    assert test.result.status == "completado"
    assert len(shown) == 3 + 1 + 2  # OSM standards, match check, two LISN lines
    assert "Abierto" in shown[0]

    monkeypatch.setattr(test_window_module.PromptDialog, "exec", lambda dialog: QDialog.DialogCode.Rejected)
    test.start()
    wait_for(lambda: not test.running)
    assert test.result.status == "abortado"


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
