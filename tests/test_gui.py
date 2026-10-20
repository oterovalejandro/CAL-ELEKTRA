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
from calelektra.gui.test_window import NewTestDialog, TestWindow
from calelektra.procedures.engine import ProcedureRunner
from calelektra.procedures.record import TestDefinition
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
    assert cable.diagram.nodes["dut"].spec.lines == ["se elige en cada ensayo"]
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
    editor = view.editor
    assert editor.frequency.setup_range.isChecked()  # this template takes the range of the setup
    editor.frequency.points.setValue(1601)
    assert view.dirty and view.action_save.isEnabled()
    editor.traces.per_trace.setChecked(True)
    editor.traces.table.item(2, 0).setText("100 Hz")  # S21
    editor.traces.table.item(2, 1).setText("4")
    assert view.save()
    data = yaml.safe_load((library_copy / "procedimientos/cable_parametros_s.yaml").read_text(encoding="utf-8"))
    assert data["barrido"]["puntos"] == 1601 and data["plantillas_compatibles"] == ["cable_vna", "componente_2p"]
    assert data["barrido"]["modo"] == "lineal" and "inicio" not in data["barrido"]
    assert data["barrido"]["por_traza"] is True and data["barrido"]["trazas"] == {"S21": {"ifbw": "100 Hz", "promedios": 4}}


def test_template_editor_frequency_modes_and_specs(window, qapp, library_copy):
    view = window.procedures_view
    view.reload(select="cable_292_pasa_falla")
    editor = view.editor
    frequency = editor.frequency
    assert frequency.definition.currentData() == "paso" and frequency.step.text() == "50 MHz"
    assert "800 puntos" in frequency.preview.text()
    assert editor.specs.table.rowCount() == 2 and editor.specs.table.item(0, 2).text() == "S21, S12"
    frequency.mode.setCurrentIndex(frequency.mode.findData("logaritmico"))
    assert not frequency.form.isRowVisible(frequency.step) and frequency.form.isRowVisible(frequency.points)
    frequency.mode.setCurrentIndex(frequency.mode.findData("lista"))
    assert frequency.form.isRowVisible(frequency._file_row)
    with pytest.raises(ValueError, match="CSV"):
        editor.build()
    reference = window.library.copy_procedure_file(library_copy / "procedimientos/listas/puntos_cable_N.csv", "listas")
    frequency.list_file.setText(reference)
    frequency._edited()
    assert "268 puntos" in frequency.preview.text()
    editor.specs.table.item(1, 2).setText("S11, S33")
    with pytest.raises(ValueError, match="S33"):
        editor.build()
    editor.specs.table.selectRow(1)
    editor.specs._remove()
    assert editor.specs.table.rowCount() == 1
    assert editor.specs.add_file(library_copy / "procedimientos/especificaciones/cable_N_2m_referencia.s2p")
    procedure = editor.build()
    assert procedure.sweep.mode == "lista" and procedure.sweep.list_file == "listas/puntos_cable_N.csv"
    assert [s.file for s in procedure.specifications][-1] == "especificaciones/cable_N_2m_referencia.s2p"


def _choose(combo: QComboBox, data) -> None:
    index = combo.findData(data)
    assert index >= 0, data
    combo.setCurrentIndex(index)


def test_new_test_dialog_precheck(window, qapp):
    dialog = NewTestDialog(window.library, "cable_parametros_s", "cable_vna_N_alarma", parent=window)
    assert dialog.current_setup().id == "cable_vna_N_alarma"
    _choose(dialog.dut, "CAB-DUT-0001")
    assert dialog.plan is not None and not dialog.plan.ok and not dialog.accept_button.isEnabled()
    assert "familias incompatibles" in " ".join(i.message for i in dialog.plan.issues)
    dialog = NewTestDialog(window.library, "cable_292_pasa_falla", "cable_vna_292", parent=window)
    assert dialog.current_dut() is None and not dialog.plan.ok  # the cable to calibrate is chosen in the test
    assert "Cable a calibrar" in dialog.dut_label.text()
    _choose(dialog.dut, "CAB-DUT-0002")
    assert dialog.plan.ok and dialog.accept_button.isEnabled() and dialog.automatic.isEnabled()
    assert dialog.name.text().startswith("CAB-DUT-0002 ")
    setups = {dialog.setup.itemData(i) for i in range(dialog.setup.count())}
    assert "lisn_impedancia_VNA-BF" not in setups and "componente_292_tosm" in setups  # only compatible setups
    assert "dut" in dialog.diagram.nodes and "CAB-DUT-0002" in " ".join(dialog.diagram.nodes["dut"].spec.lines)
    assert dialog.specs.table.rowCount() == 2  # specifications of the template, editable for this test
    _choose(dialog.setup, "componente_292_tosm")
    assert dialog.current_dut() == "CAB-DUT-0002"  # the chosen cable is kept; the check says it does not fit
    assert not dialog.plan.ok and "género" in " ".join(i.message for i in dialog.plan.issues)
    _choose(dialog.dut, "CAB-DUT-0003")
    assert dialog.plan.ok and dialog.plan.design.method == "TOSM"


def test_create_and_run_automatic_test(window, qapp, library_copy):
    folder = window.library.tests_folder / "Clientes"
    folder.mkdir(parents=True)
    window.tests_view.folders.reload(select=folder)
    dialog = NewTestDialog(window.library, "cable_292_pasa_falla", "cable_vna_292", folder=folder, parent=window)
    assert dialog.current_folder() == folder
    _choose(dialog.dut, "CAB-DUT-0002")
    dialog.name.setText("Cable cliente 2001")
    dialog.automatic.setChecked(True)
    dialog._accept()
    record = dialog.created
    assert record is not None and record.status == "preparado" and record.folder == folder / "Cable_cliente_2001"
    test = window.open_test_record(record, automatic=True)
    assert test.start_button.isEnabled() and test.badge.text() == "Preparado"
    test.start()
    wait_for(lambda: not test.running)
    assert test.record.status == "completado" and test.record.verdict == "PASA"
    assert test.plot.datasets and test.plot.limits and test.spec_table.rowCount() == 4  # 2 specs × 2 parameters
    assert "PASA" in test.tabs.tabText(1)
    window.show_section("ensayos")
    assert window.tests_view.table.rowCount() == 1
    assert window.home.tile_tests.value.text() == "1"


def test_test_with_operator_dialogs(window, qapp, monkeypatch):
    shown = []

    def answer(dialog):
        shown.append(dialog.windowTitle())
        return QDialog.DialogCode.Accepted

    library = window.library
    monkeypatch.setattr(test_window_module.PromptDialog, "exec", answer)
    definition = TestDefinition.from_template(library.procedures["impedancia_lisn"], "lisn_impedancia_VNA-BF", "LISN-0001")
    record = library.create_test(library.tests_folder, "LISN 1", definition, "prueba")
    test = window.open_test_record(record)
    test.start()
    wait_for(lambda: not test.running)
    assert test.record.status == "completado"
    assert len(shown) == 3 + 1 + 2  # OSM standards, match check, two LISN lines
    assert "Abierto" in shown[0]

    monkeypatch.setattr(test_window_module.PromptDialog, "exec", lambda dialog: QDialog.DialogCode.Rejected)
    test.start()  # repetition: a new test with the same definition, in the same folder
    wait_for(lambda: not test.running)
    assert test.record.status == "abortado" and test.record.folder != record.folder
    assert test.record.title == "LISN 1 (repetición)"
    assert len(library.tests()) == 2


def test_tests_explorer_and_finished_test(window, qapp):
    library = window.library
    definition = TestDefinition.from_template(library.procedures["cable_292_pasa_falla"], "componente_292_tosm", "CAB-DUT-0003")
    record = library.create_test(library.tests_folder / "2026", "Cable largo", definition, "prueba")
    result = ProcedureRunner.for_test(library, record, seed=3).run()
    assert result.record.verdict == "FALLA"
    prepared = library.create_test(library.tests_folder / "2026", "Pendiente", definition, "prueba")
    view = window.tests_view
    window.show_section("ensayos")
    view.show_test(record.folder)
    assert view.folders.current_folder() == library.tests_folder / "2026"
    assert view.table.rowCount() == 2
    assert view.detail.record.folder == record.folder
    assert "FALLA" in view.detail.specs.text() and view.detail.plot.limits
    assert list(view.detail.plot.datasets) == ["DUT"]  # the measurement, not the archived specification file
    frequencies, s = view.detail.plot.datasets["DUT"]
    assert len(frequencies) == 800 and 20 * np.log10(abs(s[-1, 1, 0])) < -7.5  # 1,2 m cable: about 8 dB at 40 GHz
    assert view.action_repeat.isEnabled()
    view.show_test(prepared.folder)
    assert view.action_open.text().startswith("Ejecutar") and not view.action_repeat.isEnabled()
    test = window.open_test_folder(record.folder)
    assert test.record.verdict == "FALLA" and test.start_button.text() == "Repetir ensayo"
    assert test.plot.datasets and test.spec_table.rowCount() == 4 and not test.edit_button.isEnabled()
    copy = window.repeat_test(record.folder)
    assert copy.record.status == "preparado" and copy.record.definition.dut_id == "CAB-DUT-0003"
    assert copy.edit_button.isEnabled()


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
