"""GUI smoke tests (run offscreen)."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
import yaml
from PySide6.QtWidgets import QApplication, QComboBox

from calelektra.catalog.library import Library
from calelektra.core.frequency import FrequencyRange
from calelektra.gui.main_window import MainWindow
from calelektra.gui.plots import SParameterDialog, VnaCheckDialog
from calelektra.gui.setup_editor import SetupEditor
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
    for editor in win._editor_tabs():
        editor.dirty = False
    win.close()


def process(qapp):
    for _ in range(3):
        qapp.processEvents()


def test_open_every_setup(window, qapp):
    for setup_id in window.library.setups:
        editor = window.open_setup(setup_id)
        process(qapp)
        assert isinstance(editor, SetupEditor)
        assert editor.analysis is not None
        assert editor.diagram.nodes, setup_id
    alarm = window.open_setup("cable_N_26G_alarma")
    assert alarm.badge.text().startswith("ALARMA")
    nsa = window.open_setup("sac_nsa_3m")
    assert nsa.analysis.covered
    assert {"vna.P1", "vna.P2", "antena_tx", "soporte_rx"} <= set(nsa.diagram.nodes)


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
    assert editor.analysis.covered
    assert editor.dirty
    # the library keeps the saved version until the editor saves
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
    setup = ConfiguredSetup(id="nuevo_cable", name="Nuevo cable", template="componente_2p", required_range=FrequencyRange(10e6, 40e9))
    editor = window.open_new_setup(setup)
    editor.setup.assignments.update(
        {"vna": "VNA-01", "cable_p1": "CAB-PT-292-01", "cable_p2": "CAB-PT-292-02", "kit_cal": "KIT-292-01", "dut": "CAB-DUT-0001"}
    )
    editor.refresh()
    assert editor.analysis.covered
    editor._node_moved("dut", 10.0, 20.0)
    assert editor.save()
    data = yaml.safe_load((library_copy / "hardware_setups/configurados/nuevo_cable.yaml").read_text(encoding="utf-8"))
    assert data["rango_requerido"] == ["10 MHz", "40 GHz"]
    assert data["disposicion"]["dut"] == [10.0, 20.0]
    assert "nuevo_cable" in Library.load(library_copy).setups


def test_inventory_saves_s_parameter_reference(window, qapp, library_copy):
    inventory = window.inventory
    inventory.refresh(select_id="CAB-PT-N-01")
    process(qapp)
    assert inventory.detail.equipment.id == "CAB-PT-N-01"
    inventory.detail.s_reference.setText("Certificado CAL-2026-777")
    inventory.detail._save_s_data()
    data = yaml.safe_load((library_copy / "catalogo/equipos/CAB-PT-N-01.yaml").read_text(encoding="utf-8"))
    assert data["datos_s"]["referencia"] == "Certificado CAL-2026-777"


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
