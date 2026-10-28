"""GUI tests (run offscreen)."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pyqtgraph as pg
import pytest
import yaml
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QComboBox, QDialog, QLabel, QMessageBox

from calelektra.catalog.library import Library
from calelektra.core.frequency import FrequencyRange
from calelektra.core.networks import smoothed
from calelektra.gui import test_window as test_window_module
from calelektra.gui.equipment_dialogs import EquipmentDialog, ModelDialog
from calelektra.gui.main_window import SECTIONS, MainWindow
from calelektra.gui.plots import ResultPlots, SParameterDialog, SParameterPlot, VnaCheckDialog
from calelektra.gui.setup_editor import SetupEditor
from calelektra.gui.test_window import LaunchTestDialog, TestWindow
from calelektra.procedures.engine import ProcedureRunner
from calelektra.setups.model import ConfiguredSetup
from calelektra.uncertainty.impedance import impedance_uncertainty
from conftest import E5061B_VALID_DAY, renew_calibration, tph_workbook


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
    assert {"vna", "puerto:P1", "puerto:P2", "P1-1", "plano:PR1", "antena_tx", "soporte_rx"} <= set(nsa.diagram.nodes)
    # every setup: VNA on the left, its ports, the path of each port, the planes, then what hangs from them
    x = {key: node.pos().x() for key, node in nsa.diagram.nodes.items()}
    assert x["vna"] < x["puerto:P1"] == x["puerto:P2"] < x["P1-1"] < x["plano:PR1"] == x["plano:PR2"] < x["antena_tx"]
    assert nsa.diagram.nodes["puerto:P2"].spec.lines == ["NMD 2,92 mm macho", "Camino RX"]
    cable = window.open_setup("cable_vna_292")
    assert cable.state_combo.currentText() == "medida"
    assert {"vna", "puerto:P1", "puerto:P2", "plano:PR1", "plano:PR2", "dut"} <= set(cable.diagram.nodes)
    assert cable.diagram.nodes["dut"].spec.lines == ["lo define la plantilla de ensayo"]
    assert cable.diagram.nodes["plano:PR1"].spec.lines == ["NMD 2,92 mm macho", "DUT: 2,92 mm (K) hembra"]
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
    renew_calibration(window.library, "KIT-N-01")  # the example kit expires on 2026-11-05; the editor checks today
    setup = ConfiguredSetup(id="nuevo_cable", name="Nuevo cable", template="camino_rf_2p_s", required_range=FrequencyRange(10e6, 18e9))
    editor = window.open_new_setup(setup)
    editor.setup.assignments.update({"vna": "VNA-01", "kit_cal": "KIT-N-01", "dut": "CAB-DUT-0001"})
    editor.refresh()
    assert not editor.analysis.ok  # empty paths: the planes are the 2,92 mm ports of the VNA, not N
    # the path of each port, from the VNA: a measurement cable 2,92 mm – N
    for path, cable in (("P1", "CAB-PT-N-01"), ("P2", "CAB-PT-N-02")):
        editor.show_path(path)
        panel = editor.role_panel
        assert panel.title.text() == f"Camino {path}" and "Sin equipos" in panel.path_list.item(0).text()
        panel.path_candidates.setCurrentIndex(panel.path_candidates.findData(cable))
        panel.path_buttons["añadir"].click()
        process(qapp)
    assert [i.equipment for i in editor.setup.paths["P1"]] == ["CAB-PT-N-01"] and editor.selected_role == "P2-1"
    assert editor.analysis.ok and editor.analysis.covered
    assert "DUT: N 50 Ω macho" in editor.diagram.nodes["plano:PR2"].spec.lines
    assert "Conector de DUT admitido:</b> N 50 Ω macho" in editor.role_panel.info.text()
    editor.role_panel.path_buttons["invertir"].click()  # connected by its N end: the planes cannot be reached
    process(qapp)
    assert editor.setup.paths["P2"][0].reversed and not editor.analysis.ok
    editor.role_panel.path_buttons["invertir"].click()
    process(qapp)
    editor._node_moved("dut", 10.0, 20.0)
    assert editor.save()
    data = yaml.safe_load((library_copy / "hardware_setups/configurados/nuevo_cable.yaml").read_text(encoding="utf-8"))
    assert data["formato"] == "calelektra/setup@0.4"
    assert data["rango_requerido"] == ["10 MHz", "18 GHz"]
    assert data["caminos"] == {"P1": ["CAB-PT-N-01"], "P2": ["CAB-PT-N-02"]}
    assert data["disposicion"]["dut"] == [10.0, 20.0]
    assert "nuevo_cable" in Library.load(library_copy).setups


def test_path_panel_orders_the_equipment(window, qapp):
    renew_calibration(window.library, "KIT-N-01")
    editor = window.open_setup("acoplador_N_18G")
    editor._role_clicked("P1-2")  # the adapter: its path is shown with it selected
    panel = editor.role_panel
    assert panel.title.text() == "Camino P1" and panel.path_list.currentRow() == 1
    assert panel.path_list.item(1).text().startswith("2. ADP-NN-01")
    panel.path_buttons["subir"].click()
    process(qapp)
    assert [i.equipment for i in editor.setup.paths["P1"]] == ["ADP-NN-01", "CAB-PT-N-01"]
    assert not editor.analysis.ok  # an N male adapter on the 2,92 mm port of the VNA
    editor.role_panel.path_buttons["bajar"].click()
    process(qapp)
    assert [i.equipment for i in editor.setup.paths["P1"]] == ["CAB-PT-N-01", "ADP-NN-01"] and editor.analysis.ok
    editor.role_panel.path_buttons["quitar"].click()
    process(qapp)
    assert [i.equipment for i in editor.setup.paths["P1"]] == ["CAB-PT-N-01"]
    assert "DUT: N 50 Ω macho" in editor.diagram.nodes["plano:PR1"].spec.lines


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
    renew_calibration(window.library, "VNA-BF-01")
    editor = window.open_setup("lisn_impedancia_VNA-BF")
    editor.test_button.click()
    process(qapp)
    view = window.procedures_view
    assert window.current_section() == "procedimientos" and view.is_new
    assert view.editor.algorithm.currentData() == "vna_1_puerto" and view.editor.setup.currentData() == "lisn_impedancia_VNA-BF"
    view.editor.eut.setCurrentIndex(view.editor.eut.findData("LISN-0001"))
    view.editor.refresh_check()
    assert view.editor.plan.ok
    assert "EUT en PR1" in view.editor.check_summary.text() and "Camino P1:</b> P1 (N 50 Ω hembra) → CAB-PT-BF-01" in view.editor.check_summary.text()


def test_template_editor_for_coupler_pairs_and_eut_ports(window, qapp):
    renew_calibration(window.library, "KIT-N-01")
    view = window.procedures_view
    window.show_section("procedimientos")
    view.reload(select="acoplador_ACP-0001")
    editor = view.editor
    assert editor.algorithm.currentData() == "vna_acoplador" and editor.setup.currentData() == "acoplador_N_18G"
    assert editor.pair_values() == [("In", "Out"), ("In", "Coupl. Fwd"), ("Out", "Coupl. Fwd")]
    assert editor.pairs.isVisibleTo(editor) and not editor.ports_row.isVisibleTo(editor)
    assert "Valores nominales del acoplador" in editor.check_summary.text() and editor.plan.ok
    aislado = next(row for row in range(editor.pairs.count()) if editor.pairs.item(row).data(Qt.ItemDataRole.UserRole) == ("In", "Coupl. Rev"))
    editor.pairs.item(aislado).setCheckState(Qt.CheckState.Checked)
    template = editor.build()
    assert ("In", "Coupl. Rev") in template.measured_pairs() and len(template.measured_pairs()) == 4
    view._set_dirty(False)  # discard the change

    view.reload(select="cable_CAB-DUT-0003")
    editor = view.editor
    assert editor.ports_row.isVisibleTo(editor) and not editor.pairs.isVisibleTo(editor)
    assert editor.eut_port_values() == [] and editor.build().eut_ports == []  # the order of its category: A on PR1, B on PR2
    _choose(editor.eut_ports[0], "B")
    _choose(editor.eut_ports[1], "A")
    assert editor.build().eut_ports == ["B", "A"]
    editor.refresh_check()
    assert not editor.plan.ok and "B en PR1, A en PR2" in editor.check_summary.text()


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
        assert not dialog.isModal()  # the test window can still stop the test
        dialog.done(QDialog.DialogCode.Accepted)

    library = window.library
    monkeypatch.setattr(test_window_module.PromptDialog, "show", answer)
    test = window._add_test_window(
        TestWindow(library, library.procedures["lisn_LISN-0001"], library.tests_folder, "LISN 1", "prueba", today=E5061B_VALID_DAY)
    )
    assert test.badge.text() == "Listo para lanzar"
    test.start()
    wait_for(lambda: not test.running)
    first = test.record
    assert first.status == "completado"
    assert len(shown) == 3 + 1 + 1  # OSM standards, match check, the EUT terminal
    assert "Abierto" in shown[0]

    monkeypatch.setattr(test_window_module.PromptDialog, "show", lambda dialog: dialog.done(QDialog.DialogCode.Rejected))
    test.start()  # «Repetir»: the template again, a new test in the same folder
    wait_for(lambda: not test.running)
    assert test.record.status == "abortado" and test.record.folder != first.folder
    assert test.record.title == "LISN 1 (repetición)"
    assert len(library.tests()) == 2


def test_units_of_the_charts(qapp):
    f = np.linspace(1e9, 3e9, 5)
    s = np.zeros((5, 2, 2), dtype=complex)
    s[:, 0, 0], s[:, 1, 1], s[:, 1, 0], s[:, 0, 1] = 0.1, 0.2j, 0.5, 0.5
    plots = ResultPlots()
    plot = plots.for_algorithm("vna_2_puertos")
    assert [plot.mode.itemText(i) for i in range(plot.mode.count())] == ["VSWR, Magnitud (dB)", "Magnitud (dB)", "Fase (°)", "Retardo de grupo (ns)"]
    assert plot.mode.currentData() == "db"
    plot.set_data("DUT", f, s)
    plot.set_uncertainty("DUT", {p: (np.full(5, 0.5), np.full(5, 2.0)) for p in ("S11", "S21", "S12", "S22")})
    plot.mode.setCurrentIndex(plot.mode.findData("vswr_db"))  # VSWR of the reflections, dB of the transmissions on the right axis
    item = plot.plot.getPlotItem()
    assert item.getAxis("right").isVisible() and item.getAxis("left").labelText == "VSWR, suavizado 1 %"
    left = {c.opts["name"]: c.yData for c in item.listDataItems()}
    right = {c.opts["name"]: c.yData for c in plot.right.addedItems if isinstance(c, pg.PlotDataItem)}
    assert set(left) == {"S11", "S22"} and set(right) == {"S21", "S12"}
    assert np.allclose(left["S11"], 1.1 / 0.9) and np.allclose(right["S21"], 20 * np.log10(0.5))
    assert [i for i in plot.right.addedItems if isinstance(i, pg.FillBetweenItem)]  # uncertainty also on the second axis
    plot.set_data("DUT", f, np.where(np.eye(2, dtype=bool), np.nan, s))  # only transmissions: one axis in dB
    assert not item.getAxis("right").isVisible() and item.getAxis("left").labelText == "Magnitud (dB)"
    assert not plot.mode.findData("z") >= 0  # the impedance is not a unit of the S-parameters
    # the phase as time: the group delay of the transmissions, with U_φ/(360°·f); no specifications on it
    delayed = s.copy()
    delayed[:, 1, 0] = delayed[:, 0, 1] = 0.5 * np.exp(-2j * np.pi * f * 0.3e-9)  # 0.3 ns
    plot.set_data("DUT", f, delayed)
    plot.mode.setCurrentIndex(plot.mode.findData("retardo"))
    curves = {c.opts["name"]: c.yData for c in item.listDataItems()}
    assert set(curves) == {"S21", "S12"} and np.allclose(curves["S21"], 0.3)
    assert item.getAxis("left").labelText == "Retardo de grupo (ns), suavizado 1 %" and not item.getAxis("right").isVisible()
    fills = [i for i in item.items if isinstance(i, pg.FillBetweenItem)]
    assert fills and np.allclose(fills[0].curves[0].yData, 0.3 + 2.0 / (360 * f) * 1e9)
    assert not plot.show_limits.isEnabled()
    # the VSWR is always smoothed over 1 % of the points, as in the certificates, not with the smoothing of the template
    many = np.linspace(1e9, 2e9, 600)
    jumping = np.zeros((600, 2, 2), dtype=complex)
    jumping[:, 0, 0] = np.where(np.arange(600) % 2, 0.02, 0.06)
    jumping[:, 1, 1], jumping[:, 1, 0], jumping[:, 0, 1] = 0.05, 0.5, 0.5
    plot.set_data("DUT", many, jumping)
    plot.set_smoothing({"S11": 10.0})
    plot.mode.setCurrentIndex(plot.mode.findData("vswr_db"))
    curves = {c.opts["name"]: c.yData for c in item.listDataItems()}
    assert curves["S11"][300] == pytest.approx((1.02 / 0.98 + 1.06 / 0.94) / 2) and "1 %" in plot.smooth_box.toolTip()

    impedance_plots = ResultPlots()
    impedance = impedance_plots.for_algorithm("vna_1_puerto")  # the 1-port impedance measurement
    assert [impedance.mode.itemText(i) for i in range(impedance.mode.count())] == ["Impedancia (Ω)", "Impedancia (dBΩ)", "Fase (°)"]
    gamma = np.full(5, 1 / 3 + 0j)  # Z = 100 Ω
    impedance.set_data("DUT", f, gamma)
    impedance.set_uncertainty("DUT", {"S11": (np.full(5, 0.5), np.full(5, 2.0))})
    curves = impedance.plot.getPlotItem().listDataItems()
    assert np.allclose(curves[0].yData, 100) and impedance.uncertainty_box.isEnabled()
    # the band of Z comes from that of S11 (uncertainty/impedance.py)
    expected = impedance_uncertainty(gamma, np.full(5, 0.5), np.full(5, 2.0), "dB")
    fills = [i for i in impedance.plot.getPlotItem().items if isinstance(i, pg.FillBetweenItem)]
    assert fills and np.allclose(fills[0].curves[0].yData, 100 + expected.magnitude_plus)
    impedance.mode.setCurrentIndex(impedance.mode.findData("z_db"))
    assert np.allclose(impedance.plot.getPlotItem().listDataItems()[0].yData, 40)
    impedance.mode.setCurrentIndex(impedance.mode.findData("z_fase"))
    assert np.allclose(impedance.plot.getPlotItem().listDataItems()[0].yData, 0)
    assert [i for i in impedance.plot.getPlotItem().items if isinstance(i, pg.FillBetweenItem)]
    # the boxes of a test chart are always there; usable when the test has smoothing or specifications
    assert impedance.smooth_box.isVisibleTo(impedance) and not impedance.smooth_box.isEnabled()
    assert impedance.show_limits.isVisibleTo(impedance) and not impedance.show_limits.isEnabled()
    other = SParameterPlot()
    assert not other.smooth_box.isVisibleTo(other) and not other.show_limits.isVisibleTo(other)  # other charts: when they apply


def test_stop_while_the_operator_is_asked(window, qapp, monkeypatch):
    library = window.library
    open_dialogs = []
    monkeypatch.setattr(test_window_module.PromptDialog, "show", lambda dialog: open_dialogs.append(dialog))  # nobody answers
    test = window._add_test_window(
        TestWindow(library, library.procedures["lisn_LISN-0001"], library.tests_folder, "LISN 2", "prueba", today=E5061B_VALID_DAY)
    )
    test.start()
    wait_for(lambda: bool(open_dialogs))
    assert test.running and test.action_stop.isEnabled()
    window.workspace.setCurrentWidget(test)
    window.stop_test_action.trigger()  # menu «Ensayo → Detener ensayo», same as the button of the window
    wait_for(lambda: not test.running)
    assert test.record.status == "abortado" and test.record.message == "Ensayo detenido por el operador"
    assert len(open_dialogs) == 1 and test.bridge.dialog is None and not test.action_stop.isEnabled()
    assert test.start_button.isEnabled() and test.start_button.text() == "Repetir ensayo"


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


def chips_of(layout) -> list[str]:
    return [layout.itemAt(i).widget().text() for i in range(layout.count()) if isinstance(layout.itemAt(i).widget(), QLabel)]


def close_messages(widget) -> list[str]:
    """Texts of the (non-blocking) message boxes shown to the operator, which are closed."""
    texts = []
    for box in widget.window().findChildren(QMessageBox):
        if box.isVisible():
            texts.append(box.text())
            box.close()
    return texts


def test_uncertainty_bands_in_the_test_window_and_explorer(window, qapp, tmp_path):
    library = window.library
    test = window._add_test_window(
        TestWindow(library, library.procedures["cable_CAB-DUT-0002"], library.tests_folder, "Cable con U", "prueba", automatic=True)
    )
    assert not test.conditions_button.isEnabled() and not test.save_button.isEnabled()
    test.start()
    wait_for(lambda: not test.running)
    record = test.record
    assert record.status == "completado" and record.conditions == {"estado": "pendiente"}
    plot = test.plot
    assert set(plot.uncertainty["DUT"]) == {"S11", "S21", "S12", "S22"}
    assert plot.uncertainty_box.isVisible() and plot.uncertainty_box.text() == "Incertidumbre (k=2)"
    fills = [item for item in plot.plot.getPlotItem().items if isinstance(item, pg.FillBetweenItem)]
    assert len(fills) >= 4  # one band (or more, between gaps) per trace
    plot.uncertainty_box.setChecked(False)
    assert not [item for item in plot.plot.getPlotItem().items if isinstance(item, pg.FillBetweenItem)]
    plot.uncertainty_box.setChecked(True)
    rows = {test.results.item(r, 0).text(): test.results.item(r, 1).text() for r in range(test.results.rowCount())}
    assert rows["Condiciones ambientales"].startswith("pendientes") and rows["Incertidumbre · estado"].startswith("PENDIENTE")
    assert rows["Incertidumbre · S21 (U, k=2)"].startswith("0.04 – 0.06 dB")
    assert "INCERTIDUMBRE PENDIENTE" in chips_of(test.chips)
    assert test.conditions_button.isEnabled() and test.action_save.isEnabled()

    # the probe workbook of the test completes the uncertainty
    assert test.load_conditions(tph_workbook(tmp_path / "Condiciones_ambientales.xlsx", record.started, record.finished, 22.5))
    assert any("Incertidumbre recalculada" in text for text in close_messages(test))
    process(qapp)
    assert "INCERTIDUMBRE PENDIENTE" not in chips_of(test.chips)
    rows = {test.results.item(r, 0).text(): test.results.item(r, 1).text() for r in range(test.results.rowCount())}
    assert rows["Temperatura ambiente"] == "22.5 – 22.5 °C (media 22.5)" and rows["Incertidumbre · estado"].startswith("completa")
    assert "Humedad relativa" in rows and set(plot.uncertainty["DUT"]) == {"S11", "S21", "S12", "S22"}
    zip_path = test.save_package(tmp_path / "Cable con U.zip")
    assert zip_path is not None and zip_path.is_file()
    assert any("Ensayo guardado" in text for text in close_messages(test))

    window.show_section("ensayos")
    window.tests_view.show_test(record.folder)
    process(qapp)
    detail = window.tests_view.detail
    assert set(detail.plot.uncertainty["DUT"]) == {"S11", "S21", "S12", "S22"}
    assert np.allclose(detail.plot.uncertainty["DUT"]["S21"][0], plot.uncertainty["DUT"]["S21"][0], rtol=1e-6)
    assert "Incertidumbre y condiciones" in detail.info.text() and "Temperatura ambiente" in detail.info.text()
    assert "INCERTIDUMBRE PENDIENTE" not in chips_of(detail.chips)


def test_explorer_loads_the_conditions_of_a_pending_test(window, qapp, tmp_path):
    library = window.library
    record = ProcedureRunner(library, library.procedures["cable_CAB-DUT-0002"], seed=3).run().record
    opened = window.open_test_folder(record.folder)
    window.show_section("ensayos")
    view = window.tests_view
    view.show_test(record.folder)
    process(qapp)
    assert "INCERTIDUMBRE PENDIENTE" in chips_of(view.detail.chips)
    assert view.action_conditions.isEnabled() and view.action_save.isEnabled()
    row = next(r for r in range(view.table.rowCount()) if view.table.item(r, 0).text() == record.title)
    assert view.table.item(row, 6).text().endswith("incertidumbre pendiente")
    # a workbook that does not cover the test: a warning, nothing changes
    early = tph_workbook(tmp_path / "otro_dia.xlsx", record.started.replace(year=2025), record.finished.replace(year=2025))
    assert not view.load_conditions(early)
    assert any("no cubre el ensayo" in text for text in close_messages(view))
    assert view.load_conditions(tph_workbook(tmp_path / "tph.xlsx", record.started, record.finished))
    close_messages(view)
    process(qapp)
    assert "INCERTIDUMBRE PENDIENTE" not in chips_of(view.detail.chips)
    assert library.load_test(record.folder).uncertainty["estado"] == "completa"
    assert opened.record.uncertainty["estado"] == "completa"  # the open window shows the record again
    assert view.save_package(tmp_path / "paquete.zip").is_file()
    close_messages(view)


def test_launch_dialog_and_template_uncertainty(window, qapp):
    library = window.library
    dialog = LaunchTestDialog(library, "cable_CAB-DUT-0002", library.tests_folder, parent=window)
    assert "Incertidumbre</b> (k=2)" in dialog.summary.text()
    assert not hasattr(dialog, "record_temperature")  # the temperature comes from the TPH probe, not from the operator
    dialog.close()

    window.show_section("procedimientos")
    view = window.procedures_view
    view.reload(select="cable_CAB-DUT-0002")
    editor = view.editor
    assert editor.uncertainty.isChecked() and editor.coverage.value() == 2.0
    assert "sonda TPH" in editor.temperature_stability.toolTip()
    editor.coverage.setValue(2.5)
    editor.trace_noise.setChecked(False)
    template = editor.build()
    assert template.uncertainty.coverage_factor == 2.5 and not template.uncertainty.trace_noise
    assert template.to_yaml_dict()["incertidumbre"] == {"activa": True, "k": 2.5, "ruido_traza": False, "estabilidad_temperatura": True}


def test_vna_dialog_keeps_its_uncertainty_file(window, qapp):
    library = window.library
    dialog = EquipmentDialog(library, library.equipment["VNA-01"], window)
    assert dialog.uncertainty_file.text().endswith("VNA_ZNB43_101683_uncertainties_calculation.yaml")
    assert "VNA-ZNB43-101683-UNC-08-2026" in dialog.uncertainty_info.text() and "vence 2028-04-21" in dialog.uncertainty_info.text()
    assert dialog.build().uncertainty_file == library.equipment["VNA-01"].uncertainty_file
    cable = EquipmentDialog(library, library.equipment["CAB-DUT-0002"], window)
    assert not cable.calibration_form.isRowVisible(cable.uncertainty_row)
    assert cable.build().uncertainty_file is None


def test_transmission_and_coupler_tests_in_the_window(window, qapp):
    library = window.library
    renew_calibration(library, "KIT-N-01")
    transmission = window._add_test_window(
        TestWindow(library, library.procedures["transmision_CAB-DUT-0001"], library.tests_folder, "Pérdidas", "prueba", automatic=True)
    )
    transmission.start()
    wait_for(lambda: not transmission.running)
    assert transmission.record.status == "completado"
    labels = [transmission.plot.trace_boxes.itemAt(i).widget().text() for i in range(transmission.plot.trace_boxes.count())]
    assert labels == ["S21"]  # only the measured trace
    rows = {transmission.results.item(r, 0).text(): transmission.results.item(r, 1).text() for r in range(transmission.results.rowCount())}
    assert rows["Camino P1"].startswith("P1 (NMD 2,92 mm macho) → CAB-PT-N-01 (A→B) → PR1: N 50 Ω hembra")
    assert rows["Calibración"] == "normalización de transmisión · through ADP-NN-01" and rows["Medida"].endswith("_S21.csv (A en P1, B en P2)")

    coupler = window._add_test_window(
        TestWindow(library, library.procedures["acoplador_ACP-0001"], library.tests_folder, "Acoplador", "prueba", automatic=True)
    )
    coupler.start()
    wait_for(lambda: not coupler.running)
    pairs = {"In-Out", "In-Coupl. Fwd", "Out-Coupl. Fwd"}
    assert coupler.record.verdict == "PASA" and set(coupler.plot.datasets) == pairs
    assert set(coupler.plot.uncertainty) == set(coupler.plot.datasets)
    assert coupler.spec_table.rowCount() == 5 and coupler.tabs.tabText(1) == "Pasa / falla — PASA"
    # two charts: the reflection of each port (In and Out from the In–Out pair) and the transmission of each pair
    plots = coupler.plot
    assert plots is coupler.plots.coupler and coupler.plots.currentWidget() is plots
    assert [plots.mode.itemText(i) for i in range(plots.mode.count())] == ["VSWR, Magnitud (dB)", "Magnitud (dB)", "Fase (°)"]
    assert plots.reflection.labels() == ["In (In-Out, S11)", "Out (In-Out, S22)", "Coupl. Fwd (In-Coupl. Fwd, S22)"]
    assert plots.transmission.labels() == ["In → Out · IL", "In → Coupl. Fwd · C", "Out → Coupl. Fwd · I"]
    assert "Pérdidas de retorno" in plots.reflection.heading.text()
    # the boxes of the specifications and the smoothing are there, also without smoothing in the template
    assert plots.show_limits.isVisibleTo(plots) and plots.show_limits.isEnabled()
    assert plots.smooth_box.isVisibleTo(plots) and not plots.smooth_box.isEnabled()
    _, s = plots.datasets["In-Out"]
    rl = {trace.label.split(" (")[0]: trace for trace in plots.reflection_traces()}
    assert np.allclose(rl["Out"].values, -20 * np.log10(np.abs(s[:, 1, 1])))
    assert np.allclose(rl["Out"].band[1] - rl["Out"].values, plots.uncertainty["In-Out"]["S22"][0])
    _, s = plots.datasets["Out-Coupl. Fwd"]
    assert np.allclose(plots.transmission_traces()[2].values, -20 * np.log10(np.abs(s[:, 1, 0])))
    for chart in (plots.reflection, plots.transmission):
        assert [item for item in chart.plot.getPlotItem().items if isinstance(item, pg.FillBetweenItem)]
    # nominal values of the model: RL ≥ 14 dB, IL ≤ 1.2 dB, C 20 ± 1 dB, I ≥ 32 dB
    reflection_lines, transmission_lines = plots.nominal_lines()
    assert [line.label for line in reflection_lines] == ["Espec. RL ≥ 14 dB (In, Out)"] and np.allclose(reflection_lines[0].values, 14)
    labels = [line.label for line in transmission_lines if line.label]
    assert labels == ["Espec. IL ≤ 1.2 dB", "Espec. C 20 ± 1 dB", "Espec. I ≥ 32 dB"]
    assert len(transmission_lines) == 4 and {tuple(line.values) for line in transmission_lines[1:3]} == {(21, 21), (19, 19)}
    names = [item.opts.get("name") for item in plots.transmission.plot.getPlotItem().listDataItems()]
    assert "Espec. C 20 ± 1 dB" in names
    plots.mode.setCurrentIndex(plots.mode.findData("vswr_db"))  # VSWR of each port, TL of each pair
    assert "VSWR" in plots.reflection.heading.text() and "TL" in plots.transmission.heading.text()
    gamma = np.abs(plots.datasets["In-Out"][1][:, 0, 0])
    assert np.allclose(plots.reflection_traces()[0].values, smoothed((1 + gamma) / (1 - gamma)))  # over 1 % of the points
    assert plots.nominal_lines()[0][0].label == "Espec. VSWR ≤ 1.5 (In, Out)"
    plots.mode.setCurrentIndex(plots.mode.findData("fase"))
    assert plots.nominal_lines() == ([], []) and not plots.show_limits.isEnabled()
    plots.mode.setCurrentIndex(plots.mode.findData("db"))
    plots.show_limits.setChecked(False)
    assert plots.nominal_lines() == ([], [])
    plots.show_limits.setChecked(True)
    plots.reflection.trace_boxes.itemAt(1).widget().setChecked(False)
    assert len([i for i in plots.reflection.plot.getPlotItem().listDataItems() if not (i.opts.get("name") or "").startswith("Espec")]) == 2
    window.show_section("ensayos")
    window.tests_view.show_test(coupler.record.folder)
    process(qapp)
    detail = window.tests_view.detail
    assert set(detail.plot.datasets) == pairs
    assert detail.plot is detail.plots.coupler and len(detail.plot.transmission.labels()) == 3 and detail.plot.nominal
    assert "Caminos y medidas" in detail.info.text() and "Medida In-Coupl. Fwd" in detail.info.text()
    window.tests_view.show_test(transmission.record.folder)
    process(qapp)
    assert detail.plots.currentWidget() is detail.plot is detail.plots.s_parameters and list(detail.plot.datasets) == ["DUT"]
    assert detail.plots.sizeHint() == detail.plot.sizeHint()  # not the size of the two coupler charts


def test_equipment_already_used_in_the_setup_is_not_offered(window, qapp):
    library = window.library
    renew_calibration(library, "KIT-N-01")
    editor = window.open_setup("acoplador_N_18G")
    editor._role_clicked("carga_1")
    offered = [_combo_in_role_panel(editor).itemData(i) for i in range(_combo_in_role_panel(editor).count())]
    assert "LOAD-N-01" in offered and "LOAD-N-02" not in offered  # LOAD-N-02 is the other load
    assert "ya usados en otro rol" in editor.role_panel.info.text()
    editor.show_path("P2")
    panel = editor.role_panel
    candidates = {panel.path_candidates.itemData(i) for i in range(panel.path_candidates.count())}
    assert not candidates & {"CAB-PT-N-01", "CAB-PT-N-02", "ADP-NN-01", "ADP-NN-02"}  # in a path already
    assert {"ADP-THRU-N-01", "CAB-PT-BF-01"} <= candidates
    # a repeated equipment (hand-written YAML) is an error of the setup
    editor.setup.paths["P2"][1] = editor.setup.paths["P1"][1].model_copy()
    editor.refresh()
    repeated = [i for i in editor.analysis.issues if i.code == "equipo.repetido"]
    assert repeated and "«ADP-NN-01» está asignado más de una vez" in repeated[0].message and not editor.analysis.ok
    # the EUT of a test template cannot be an equipment of its setup
    window.show_section("procedimientos")
    view = window.procedures_view
    view.reload(select="cable_CAB-DUT-0001")
    euts = {view.editor.eut.itemData(i) for i in range(view.editor.eut.count())}
    assert "CAB-DUT-0001" in euts and not euts & {"CAB-PT-N-01", "CAB-PT-N-02"}


def test_open_test_windows_follow_the_edits_of_the_library(window, qapp):
    library = window.library
    launch = window._add_test_window(
        TestWindow(library, library.procedures["cable_CAB-DUT-0002"], library.tests_folder, "Cable", "prueba", automatic=True)
    )
    assert launch.plan.ok and launch.start_button.isEnabled()
    # the EUT is edited in the inventory: the launch that is open is checked again
    dialog = EquipmentDialog(library, library.equipment["CAB-DUT-0002"], window)
    dialog.status.setCurrentIndex(dialog.status.findData("fuera_de_servicio"))
    dialog._save()
    window.inventory._after_change("CAB-DUT-0002")
    process(qapp)
    assert not launch.plan.ok and not launch.start_button.isEnabled() and launch.badge.text() == "No ejecutable"
    assert any("no está en servicio" in i.message for i in launch.plan.issues)
    dialog = EquipmentDialog(library, library.equipment["CAB-DUT-0002"], window)
    dialog.status.setCurrentIndex(dialog.status.findData("en_servicio"))
    dialog._save()
    window.inventory._after_change("CAB-DUT-0002")
    process(qapp)
    assert launch.plan.ok and launch.start_button.isEnabled()

    # after a run, «Repetir» uses the current version of the template
    launch.start()
    wait_for(lambda: not launch.running)
    assert launch.record.status == "completado"
    edited = library.procedures["cable_CAB-DUT-0002"].model_copy(deep=True, update={"version": "2.0"})
    edited.set_source_path(library.procedures["cable_CAB-DUT-0002"].source_path)
    library.save_procedure(edited)
    library.reload()
    window._library_changed()
    assert launch.procedure is library.procedures["cable_CAB-DUT-0002"] and launch.procedure.version == "2.0"
    assert launch.record.template["version"] == "1.0"  # the record keeps the template as it was launched
    launch.start()
    wait_for(lambda: not launch.running)
    assert launch.record.template["version"] == "2.0"


def test_cable_specification_by_formula_in_the_editor_and_the_test(window, qapp):
    library = window.library
    window.show_section("procedimientos")
    view = window.procedures_view
    view.reload(select="cable_CAB-DUT-0004")
    editor = view.editor
    assert editor.cable_box.isVisibleTo(editor) and "0.26·√f + 0.00091·f" in editor.cable_info.text()
    assert "cable ensamblado 1.324" in editor.cable_info.text()
    assert editor.cable_tolerance_values() == {"perdidas_insercion_db": 0.5, "vswr_pct": 10.0}
    editor.vswr_tolerance.setValue(editor.vswr_tolerance.minimum())  # «— (sin tolerancia)»
    assert editor.build().cable_tolerances.vswr_pct is None and editor.build().cable_tolerances.insertion_loss_db == 0.5
    view._set_dirty(False)
    view.reload(select="cable_CAB-DUT-0002")
    assert "no tiene especificación por fórmula" in editor.cable_info.text() and not editor.cable_tolerance_values()

    test = window._add_test_window(
        TestWindow(library, library.procedures["cable_CAB-DUT-0004"], library.tests_folder, "Cable por fórmula", "prueba", automatic=True)
    )
    assert "Especificación por fórmula" in test.plan_label.text() and "VSWR ≤ 1.457" in test.plan_label.text()
    test.start()
    assert {curve.parameter for curve in test.plot.limits} == {"S11", "S21", "S12", "S22"}  # drawn while measuring
    wait_for(lambda: not test.running)
    assert test.record.verdict == "PASA" and test.spec_table.rowCount() == 4  # 2 limits × 2 parameters
    limits = {curve.parameter: curve for curve in test.plot.limits}  # from the record
    assert np.allclose(limits["S11"].values, -test.record.cable_specification["rl_min_db"])
    assert limits["S21"].values[-1] == pytest.approx(-(4.3504 + 0.5), abs=1e-3)
