"""The standards of the calibration kits in the GUI: the model of the kit (import of its .ckt, a standard
with its definition, specifications and view) and the unit of the kit (serial numbers, state, own data)."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path

import pytest
import yaml
from PySide6.QtCore import QEvent
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox

from calelektra.catalog.library import Library
from calelektra.gui.catalog_view import CatalogView
from calelektra.gui.equipment_dialogs import EquipmentDialog, ModelDialog
from calelektra.gui.kit_standards import StandardDialog, UnitStandardDialog
from calelektra.kits import SpecRow

DATA = Path(__file__).resolve().parent / "datos"
CKT = DATA / "kits" / "AA180-VNACKN1.ckt"
LOAD_SDATB = DATA / "metas" / "Example_Loadf_18831_20150824.sdatb"


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def close_windows(qapp):
    """The dialogs of each test are deleted before the next one (and before Qt ends)."""
    yield
    for widget in QApplication.topLevelWidgets():
        if isinstance(widget, QDialog) and widget.parent() is None:  # not the pop-ups of the combo boxes
            widget.close()
            widget.deleteLater()
    QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    QApplication.processEvents()


def test_a_new_kit_model_from_its_ckt(qapp, library: Library, monkeypatch):
    dialog = ModelDialog(library, category_id="kit_calibracion")
    assert dialog.kit_card.isVisibleTo(dialog) and not dialog.specs_card.isVisibleTo(dialog)
    dialog.id_edit.setText("kit_n_nuevo")
    dialog.manufacturer.setText("Genérico")
    dialog.kit_editor.import_file(CKT)
    assert dialog.kit_editor.table.rowCount() == 7
    assert dialog.designation.text() == "AA180-VNACKN" and dialog.description.text() == "TypeN (50) Calibration Kit"
    assert dialog.form.editors["familia_conector"].raw() == "N" and dialog.form.editors["nombre_kit_vna"].raw() == "AA180-VNACKN"
    dialog.form.editors["rango_frecuencia"].set_value("DC – 18 GHz")
    dialog.form.editors["metodos"].set_value(["OSM", "TOSM"])
    assert dialog.kit_file == "../patrones/kit_n_nuevo/AA180-VNACKN1.ckt"
    # importing again: replace them (yes) and keep the specifications of the same standards
    dialog.kit_editor.items[1].specifications = [SpecRow(frecuencia=0, fase_grados=1.5)]
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    dialog.kit_editor.import_file(CKT)
    assert len(dialog.kit_editor.items) == 7 and dialog.kit_editor.items[1].specifications[0].phase_deg == 1.5
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.No)  # add them
    dialog.kit_editor.import_file(CKT)
    assert len(dialog.kit_editor.items) == 14 and dialog.kit_editor.items[7].id == "carga_m_2"
    dialog.kit_editor.table.selectRow(13)
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    dialog.kit_editor.remove_standard()
    assert len(dialog.kit_editor.items) == 13
    dialog._save()
    library.reload()
    model = library.models["kit_n_nuevo"]
    assert len(model.standards) == 13 and model.kit_file and (library.root / "catalogo" / "patrones" / "kit_n_nuevo" / "AA180-VNACKN1.ckt").is_file()
    saved = yaml.safe_load(model.source_path.read_text(encoding="utf-8"))
    assert saved["patrones"][1]["definicion"]["c"] == [89.939, 2536.7999, -264.9901, 13.4]
    assert saved["fichero_kit"] == "../patrones/kit_n_nuevo/AA180-VNACKN1.ckt"


def test_a_standard_its_chart_and_its_table(qapp, library: Library):
    model = library.models["aa180_vnackn"]
    dialog = ModelDialog(library, model)
    editor = dialog.kit_editor
    standard = editor.items[1]  # OPEN -M-
    window = StandardDialog(library, standard, {s.id for s in editor.items}, dialog._kit_settings, lambda: model.id, dialog._model_folder)
    preview = window.preview
    assert "1800 puntos, 10 MHz – 18 GHz" in preview.info.text() and "1 entrada" in preview.info.text()
    assert preview.table_model.rowCount() == 1800
    line = preview.table_text().splitlines()[100]  # 1 GHz
    assert line.split("\t")[:6] == ["1 GHz", "S11", "0.706027", "-0.708020", "0.999883", "-45.081"] and line.split("\t")[-1] == "1.5"
    assert line.split("\t")[-2] == "0"  # only the phase deviates
    for mode in range(preview.mode.count()):
        preview.mode.setCurrentIndex(mode)
        assert preview.chart.labels() == ["S11"]
    # a change of the definition is seen in the view
    window.definition.delay.setText("0")
    window.refresh()
    assert preview.table_text().splitlines()[100].split("\t")[2] != "0.706027"
    built = window.build()
    assert built.id == "abierto_m" and built.definition.offset_delay_ps == 0 and built.specifications[0].phase_deg == 1.5
    # a through: four parameters
    thru = StandardDialog(library, editor.items[6], {s.id for s in editor.items}, dialog._kit_settings, lambda: model.id, dialog._model_folder)
    assert thru.preview.chart.labels() == ["S11", "S21", "S12", "S22"] and thru.preview.table_model.rowCount() == 7200
    # a standard by data: the .sdatb of a load, copied into the library
    new = StandardDialog(library, None, {s.id for s in editor.items}, dialog._kit_settings, lambda: model.id, dialog._model_folder)
    new.kind.setCurrentIndex(new.kind.findData("carga"))
    new.gender1.setCurrentIndex(new.gender1.findData("hembra"))
    assert new.id_edit.text() == "carga_h_2"
    new.definition.kind.setCurrentIndex(new.definition.kind.findData("datos"))
    new.definition.use_file(LOAD_SDATB)
    new.refresh()
    assert new.definition.file.text() == "../patrones/aa180_vnackn/Example_Loadf_18831_20150824.sdatb"
    assert "533 puntos" in new.preview.info.text() and "12792 entradas" in new.preview.info.text()
    assert new.build().definition.sha256 and (library.root / "catalogo" / "patrones" / "aa180_vnackn" / LOAD_SDATB.name).is_file()


def test_the_standards_of_a_unit_of_the_kit(qapp, library: Library):
    dialog = EquipmentDialog(library, library.equipment["KIT-N-02"])
    assert dialog.tabs.isTabVisible(dialog.standards_tab_index)
    editor = dialog.unit_standards
    assert editor.table.rowCount() == 9 and "sin fecha de próxima calibración" in editor.status.text()
    model = library.models["aa180_vnackn"]
    unit = UnitStandardDialog(library, model.standards[1], None, library.kit_settings(model), "KIT-N-02",
                              model.source_path.parent, library.root / "catalogo" / "equipos")
    unit.serial.setText("975")
    unit.state.setCurrentIndex(unit.state.findData("danado"))
    editor.units["abierto_m"] = unit.build()
    editor._fill()
    dialog.issued.setText("2026-04-01")
    dialog.due.setText("2027-04-01")
    dialog._update_kit_status()
    assert "válida hasta el 01/04/2027" in editor.status.text() and "dañado" in editor.status.text()
    assert editor.table.item(1, 3).text() == "975" and editor.table.item(1, 6).text() == "Open(m) 975"
    dialog._save()
    library.reload()
    kit = library.equipment["KIT-N-02"]
    assert kit.standards["abierto_m"].serial_number == "975" and library.kit_genders(kit) == ["hembra"]
    saved = yaml.safe_load(kit.source_path.read_text(encoding="utf-8"))
    assert saved["patrones"] == {"abierto_m": {"numero_serie": "975", "estado": "danado"}}
    # a copy of the unit does not take its serial numbers
    copy = EquipmentDialog(library, copy_of=kit)
    assert copy.unit_standards.units == {}


def test_the_kit_in_the_catalogue(qapp, library: Library):
    view = CatalogView(library)
    view._show_model("aa180_vnackn")
    text = view.extra_text.text()
    assert "Patrones del kit" in text and "OPEN -M-" in text and "57,993 ps" in text and "fase 1,5°" in text
