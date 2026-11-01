"""The standards of the calibration kits in the GUI: the model of the kit (import of its .ckt, a standard
with its definition, specifications and view) and the unit of the kit (serial numbers, state, own data)."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from datetime import date
from pathlib import Path

import pyqtgraph as pg
import pytest
import yaml
from PySide6.QtCore import QEvent
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox

from calelektra.catalog.library import Library
from calelektra.gui.catalog_view import CatalogView
from calelektra.gui.equipment_dialogs import EquipmentDialog, ModelDialog
from calelektra.gui.kit_standards import StandardDialog, UnitStandardDialog
from calelektra.gui.sparam_viewer import SParamViewer
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
    assert "Valores reales del 01/09/2026:</b> 6 patrones; 5 cumplen" in editor.status.text()
    assert editor.table.item(1, 6).text() == "01/09/2026 · No cumple" and editor.table.item(6, 6).text() == "—"
    model = library.models["aa180_vnackn"]
    unit = UnitStandardDialog(library, model.standards[1], editor.units["abierto_m"], library.kit_settings(model), "KIT-N-02",
                              model.source_path.parent, library.root / "catalogo" / "equipos")
    assert "OPENM resultados.s1p · 01/09/2026" in unit.real_info.text() and "No cumple" in unit.real_info.text()
    assert unit.own_specs.text() == "Especificaciones propias (si no, las de sus valores reales)" and unit.specs.table.rowCount() == 36
    assert "(definición del modelo, especificaciones de sus valores reales)" in unit.preview.info.text()
    unit.serial.setText("975")
    unit.state.setCurrentIndex(unit.state.findData("danado"))
    editor.units["abierto_m"] = unit.build()
    assert editor.units["abierto_m"].real_values.verdict == "no_cumple"  # the dialog keeps them
    editor._fill()
    dialog.issued.setText("2026-04-01")
    dialog.due.setText("2027-04-01")
    dialog._update_kit_status()
    assert "válida hasta el 01/04/2027" in editor.status.text() and "dañado" in editor.status.text()
    assert editor.table.item(1, 3).text() == "975" and editor.table.item(1, 7).text() == "Open(m) 975"
    dialog._save()
    library.reload()
    kit = library.equipment["KIT-N-02"]
    assert kit.standards["abierto_m"].serial_number == "975" and library.kit_genders(kit) == ["hembra"]
    saved = yaml.safe_load(kit.source_path.read_text(encoding="utf-8"))
    assert {k: v for k, v in saved["patrones"]["abierto_m"].items() if k != "valores_reales"} == {"numero_serie": "975", "estado": "danado"}
    assert saved["patrones"]["abierto_m"]["valores_reales"]["resultado"] == "no_cumple" and len(saved["patrones"]) == 6
    # a copy of the unit does not take its serial numbers
    copy = EquipmentDialog(library, copy_of=kit)
    assert copy.unit_standards.units == {}


def test_the_calibration_of_the_kit(qapp, library: Library, tmp_path):
    """«Calibración del kit…»: the real values of the standards, their deviations and what is saved."""
    dialog = EquipmentDialog(library, library.equipment["KIT-N-02"])
    editor = dialog.unit_standards
    calibration = editor.calibration_dialog()
    assert calibration.date_edit.text() == "2026-09-01" and calibration.band.text() == "1 GHz"
    rows = {entry.resolved.id: calibration.row_texts(entry) for entry in calibration.entries}
    assert rows["abierto_m"][1:4] == ["OPENM resultados.s1p", "No cumple", "S11 mód. −0,033 dB (17,5 GHz) · S11 fase −14,114° (18 GHz)"]
    assert rows["carga_m"][2] == "Cumple" and rows["through_hm"][1:3] == ["—", ""]
    assert rows["adaptador_hh"][2] == "Sin definición por el modelo: no se compara"
    # the chart of the chosen standard: its deviation, ±1,5° of the manufacturer and its own specification
    calibration.table.selectRow(next(i for i, e in enumerate(calibration.entries) if e.resolved.id == "abierto_m"))
    calibration.detail.quantity.setCurrentIndex(1)
    assert calibration.detail.chart.labels() == ["Desviación"] and "No cumple" in calibration.detail.summary.text()
    assert calibration.detail.current().column == "fase_grados"
    # new files, assigned by their name; the others keep theirs
    source = library.root / "catalogo" / "patrones" / "KIT-N-02"
    for name, target in (("OPENM resultados.s1p", "Open(m) 2027.s1p"), ("LOADH resultados.s1p", "medida 3.s1p")):
        (tmp_path / target).write_bytes((source / name).read_bytes())
    unassigned = calibration.assign_files([tmp_path / "Open(m) 2027.s1p", tmp_path / "medida 3.s1p"])
    assert unassigned == [tmp_path / "medida 3.s1p"]
    assert calibration.row_texts(calibration.entry("abierto_m"))[1] == "Open(m) 2027.s1p (nuevo)"
    calibration.set_file("carga_h", tmp_path / "medida 3.s1p")
    calibration.set_file("cortocircuito_h", None)  # «Quitar»
    calibration.date_edit.setText("2027-09-01")
    calibration.certificate.setText("CC-2027-001")
    calibration.band.setText("2 GHz")
    units, entered = calibration.build()
    assert (entered.issued, entered.certificate) == (date(2027, 9, 1), "CC-2027-001")
    assert units["abierto_m"].real_values.file == "../patrones/KIT-N-02/Open(m) 2027.s1p" and len(units["abierto_m"].real_values.specifications) == 18
    assert units["carga_h"].real_values.reference == "CC-2027-001" and "cortocircuito_h" not in units
    calibration.result_units, calibration.result_calibration = units, entered
    editor.apply_calibration(calibration)
    assert (dialog.issued.text(), dialog.certificate.text()) == ("2027-09-01", "CC-2027-001")
    assert "Valores reales del 01/09/2027:</b> 5 patrones; 4 cumplen" in editor.status.text()
    dialog._save()
    library.reload()
    kit = library.equipment["KIT-N-02"]
    assert kit.calibration.issued == date(2027, 9, 1) and kit.standards["abierto_m"].real_values.band_width == 2e9
    assert (library.root / "catalogo" / "patrones" / "KIT-N-02" / "Open(m) 2027.s1p").is_file()
    # a date is required, and a file that is not of the standard is not accepted
    again = EquipmentDialog(library, kit).unit_standards.calibration_dialog()
    again.date_edit.setText("")
    with pytest.raises(ValueError, match="fecha de calibración"):
        again.build()
    again.date_edit.setText("2027-09-01")
    again.set_file("abierto_m", LOAD_SDATB.parent.parent / "kits" / "AA180-VNACKN1.ckt")
    assert again.entry("abierto_m").error and again.row_texts(again.entry("abierto_m"))[2].startswith("✖")
    with pytest.raises(ValueError, match="no se pueden usar"):
        again.build()


def test_the_kit_in_the_catalogue(qapp, library: Library):
    view = CatalogView(library)
    view._show_model("aa180_vnackn")
    text = view.extra_text.text()
    assert "Patrones del kit" in text and "OPEN -M-" in text and "57,993 ps" in text and "fase 1,5°" in text


def test_the_correlation_of_a_standard(qapp, library: Library):
    model = library.models["aa180_vnackn"]
    dialog = ModelDialog(library, model)
    editor = dialog.kit_editor
    window = StandardDialog(library, editor.items[1], {s.id for s in editor.items}, dialog._kit_settings, lambda: model.id, dialog._model_folder)
    preview, view = window.preview, window.preview.correlation
    assert preview.tabs.tabText(2) == "Correlación"
    assert view.matrix is None and "al abrir esta pestaña" in view.summary.text()  # computed when it is shown
    preview.tabs.setCurrentIndex(2)
    window.show()
    QApplication.processEvents()
    assert view.matrix is not None and view.matrix.size == 1200
    assert "S11, parte real: 1200 de las 1800 frecuencias del rango" in view.summary.text() and "total (±1)" in view.summary.text()
    # the phase of the open: the same deviation at all the frequencies
    view.component.setCurrentIndex(view.component.findData("fase"))
    assert view.cell_text(0, 1199) == "10 MHz × 18 GHz: ρ = 1,0000 · u = 0,75° y 0,75° (k = 1)"
    assert view.cell_text(-1, 0) == " " and view.cell_text(0, 1200) == " "
    view.start.setText("1 GHz")
    view.stop.setText("2 GHz")
    view.redraw()
    assert view.matrix.size == 101 and "101 frecuencias, 1 GHz – 2 GHz" in view.summary.text()
    assert [label for _position, label in view.plot.getAxis("bottom")._tickLevels[0]][:2] == ["1 GHz", "1,2 GHz"]
    view.kind.setCurrentIndex(view.kind.findData("covarianza"))
    assert view.cell_text(0, 1).startswith("1 GHz × 1,01 GHz: covarianza = 0,56")
    header, first = view.matrix_text().splitlines()[:2]
    assert header.split("\t")[:2] == ["f (Hz)", "1000000000"] and float(first.split("\t")[1]) == pytest.approx(0.5625)
    view.component.setCurrentIndex(view.component.findData("modulo"))
    assert "sin incertidumbre" in view.summary.text()
    # at one frequency: its parameters, with their values in the cells
    view.kind.setCurrentIndex(view.kind.findData("correlacion"))
    view.scope.setCurrentIndex(view.scope.findData("punto"))
    assert not view.parameter.isVisibleTo(view) and view.at.isVisibleTo(view) and not view.start.isVisibleTo(view)
    view.at.setText("1 GHz")
    view.redraw()
    assert view.matrix.labels == ["S11 re", "S11 im"] and view.cell_text(0, 1).startswith("S11 re × S11 im: ρ = 1,0000")
    assert len([item for item in view._cells if isinstance(item, pg.TextItem)]) == 4
    view.at.setText("mucho")
    view.redraw()
    assert view.summary.text().startswith("✖ «Frecuencia»") and view.matrix is None and not view.copy_button.isEnabled()
    # a through: at 6 GHz, S21 and S12 share the inputs of the specifications of the transmission
    thru = StandardDialog(library, editor.items[6], {s.id for s in editor.items}, dialog._kit_settings, lambda: model.id, dialog._model_folder)
    thru.specs.load([SpecRow(frecuencia=0, perdidas_retorno_db=-30, transmision_modulo_db=0.05, transmision_fase_grados=0.5)])
    thru.refresh()
    view = thru.preview.correlation
    view.scope.setCurrentIndex(view.scope.findData("punto"))
    view.at.setText("6 GHz")
    view.redraw()
    assert view.matrix.size == 8 and view.cell_text(2, 4).startswith("S21 re × S12 re: ρ = 1,0000")
    assert view.cell_text(0, 6).startswith("S11 re × S22 re: ρ = 0,0000")


def test_a_file_of_vna_tools_in_the_viewer(qapp, tmp_path):
    viewer = SParamViewer(LOAD_SDATB)
    assert viewer.windowTitle().endswith(LOAD_SDATB.name) and "533 puntos" in viewer.preview.info.text()
    view = viewer.preview.correlation
    view.redraw()
    assert "nula: cada frecuencia depende de sus propias entradas" in view.summary.text()
    assert view.plot.getAxis("bottom")._tickLevels  # frequencies on the axes
    assert not viewer.open_file(tmp_path / "no_existe.sdatb") and viewer.preview.info.text().startswith("✖")
    assert view.matrix is None and viewer.path == LOAD_SDATB


def test_the_contribution_of_the_kit_to_the_uncertainty(qapp, library: Library, tmp_path, monkeypatch):
    """«Contribución a la incertidumbre…»: OSM, UOSM and normalization, against the uncertainty file of the ZNB43."""
    dialog = EquipmentDialog(library, library.equipment["KIT-N-02"])
    contribution = dialog.unit_standards.contribution_dialog()
    assert contribution.definitions.currentData() == "unidad" and contribution.vna.currentData() == "VNA-01"
    # OSM with the specifications of the manufacturer: a reflection of 0,1 at its worst phase
    contribution.definitions.setCurrentIndex(contribution.definitions.findData("modelo"))
    assert [contribution.dut.itemData(i) for i in range(contribution.dut.count())] == ["reflexion", "fichero"]
    assert not contribution.gender2.isVisibleTo(contribution)
    assert contribution.compute()
    total = contribution.contribution("S11", "modulo")["total"]
    assert 0.0268 < total.min() < total.max() < 0.0275
    assert "su fichero de incertidumbres da 0,006 a 0,009" in contribution.summary.text()
    rows = [[contribution.terms.item(r, c).text() for c in range(contribution.terms.columnCount())] for r in range(contribution.terms.rowCount())]
    assert rows[0][:3] == ["Directividad (puerto 1)", "kit 31,4 dB · VNA 45 dB", "kit 31,4 dB · VNA 42 dB"]
    assert contribution.preview.data is not None and contribution.preview.data.n_ports == 1
    header = contribution.table_text().splitlines()[0].split("\t")
    assert header[0] == "f (Hz)" and header[-1] == "total" and "Puerto 1: OPEN -M-" in header
    # UOSM with the real values of the unit: an attenuator of 20 dB, its transmission in dB
    contribution.definitions.setCurrentIndex(contribution.definitions.findData("unidad"))
    contribution.method.setCurrentIndex(contribution.method.findData("UOSM"))
    assert contribution.gender2.isVisibleTo(contribution) and not contribution.through.isVisibleTo(contribution)
    contribution.dut.setCurrentIndex(contribution.dut.findData("atenuador"))
    assert contribution.compute() and contribution.parameter.count() == 4
    contribution.parameter.setCurrentText("S21")
    contribution.quantity.setCurrentIndex(contribution.quantity.findData("db"))
    assert contribution.chart.labels()[0] == "Total del kit" and len(contribution.chart.labels()) == 7
    assert "S21: U |S| (dB) del kit" in contribution.summary.text()
    # saved as .sdatb, with the inputs of the kit
    target = tmp_path / "atenuador.sdatb"
    monkeypatch.setattr("calelektra.gui.kit_contribution.QFileDialog.getSaveFileName", lambda *a, **k: (str(target), ""))
    contribution.save()
    from calelektra.sdata import read_sdatb

    saved = read_sdatb(target)
    assert saved.n_ports == 2 and len(saved.vector.inputs) == len(contribution.result.data.vector.inputs) > 0
    # normalization with a flush through: the kit does not take part
    contribution.method.setCurrentIndex(contribution.method.findData("normalizacion"))
    assert contribution.compute() and not contribution.contribution("S21", "db")["total"].any()
    # a kit without the standards of a gender
    library.equipment["KIT-N-02"].standards["abierto_h"].state = "danado"
    dialog.unit_standards.units["abierto_h"].state = "danado"
    broken = dialog.unit_standards.contribution_dialog()
    broken.gender1.setCurrentIndex(broken.gender1.findData("hembra"))
    assert not broken.compute() and "abierto hembra" in broken.summary.text()
