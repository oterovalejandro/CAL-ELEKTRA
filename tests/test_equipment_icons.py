"""Pictograms of the equipment and the diagram of the hardware setups: nodes, ports of the EUT and of a coupler."""

from __future__ import annotations

import pytest
from conftest import TODAY, setup_with_dut
from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import QApplication

from calelektra.catalog.library import Library
from calelektra.gui.equipment_icons import GLYPHS, glyph_for, icon_pixmap, paint_glyph
from calelektra.gui.main_window import MainWindow
from calelektra.gui.setup_diagram import (
    DY,
    NODE_H,
    build_diagram_model,
    connection_picture,
    coupler_layout,
)
from calelektra.gui.test_window import TestWindow
from calelektra.procedures.engine import (
    AutoOperator,
    ProcedureRunner,
    PromptRequest,
    prepare_run,
)
from calelektra.setups.analysis import analyze_setup
from calelektra.setups.model import ConfiguredSetup

PORTS = ["In", "Out", "Coupl. Fwd", "Coupl. Rev"]
PAIRS = ["In-Out", "In-Coupl. Fwd", "Out-Coupl. Fwd"]


@pytest.fixture(scope="session")
def qapp():
    return QApplication.instance() or QApplication([])


def diagram(library: Library, setup: ConfiguredSetup, state: str = "medida"):
    template = library.resolved_template(setup.template)
    return build_diagram_model(template, setup, library, analyze_setup(setup, library, TODAY), state)


def test_pictogram_of_each_category_and_type(library):
    assert glyph_for("vna") == "vna" and glyph_for("kit_calibracion") == "kit" and glyph_for("acoplador") == "acoplador"
    assert glyph_for("antena", "bocina") == "bocina" and glyph_for("antena", None) == "log_periodica"
    assert glyph_for("posicionador", "mesa_giratoria") == "mesa_giratoria" and glyph_for("carga", "cortocircuito") == "cortocircuito"
    assert glyph_for("adaptador", "through") == "through" and glyph_for("adaptador", "coaxial") == "adaptador"
    assert glyph_for("recinto", "FAR") == "far" and glyph_for("otra") == "generico" and glyph_for(None) == "generico"
    assert [category for category in library.categories if glyph_for(category) == "generico"] == []


def test_every_pictogram_draws(qapp):
    for key in GLYPHS:
        image = QImage(48, 48, QImage.Format.Format_ARGB32)
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        paint_glyph(painter, QRectF(0, 0, 48, 48), key, QColor("#000000"))
        painter.end()
        inked = sum(1 for x in range(48) for y in range(48) if image.pixelColor(x, y).alpha() > 128)
        assert 40 < inked < 1200, key
        assert not icon_pixmap(key, 32).isNull()


def test_coupler_ports_on_the_side_of_what_they_are_connected_to():
    def plane(row: int) -> tuple[float, float]:
        return -100.0, row * DY

    # the two ends of the main line on the planes: two vertical lines, Fwd next to In
    assert coupler_layout(PORTS, {"In": plane(0), "Out": plane(1)}).corners == {"In": "lt", "Out": "lb", "Coupl. Fwd": "rt", "Coupl. Rev": "rb"}
    # one port of each line: two horizontal lines
    assert coupler_layout(PORTS, {"In": plane(0), "Coupl. Fwd": plane(1)}).corners == {"In": "lt", "Out": "rt", "Coupl. Fwd": "lb", "Coupl. Rev": "rb"}
    assert coupler_layout(PORTS, {"Out": plane(0), "Coupl. Fwd": plane(1)}).corners == {"Out": "lt", "In": "rt", "Coupl. Fwd": "lb", "Coupl. Rev": "rb"}
    assert coupler_layout(PORTS, {"Out": plane(1), "In": plane(0), "Coupl. Rev": (100.0, 0.0)}).corners["In"] == "lt"  # a load on the right
    assert coupler_layout(PORTS, {}).corners == {"In": "lt", "Out": "rt", "Coupl. Fwd": "lb", "Coupl. Rev": "rb"}


def test_coupler_setup_drawn_with_its_ports(library):
    model = diagram(library, library.setups["acoplador_N_18G"])
    dut = model.nodes["dut"]
    assert dut.coupler is not None and dut.coupler.corners == {"In": "lt", "Out": "lb", "Coupl. Fwd": "rt", "Coupl. Rev": "rb"}
    assert dut.height == DY + NODE_H and model.positions["dut"][1] == model.positions["plano:PR1"][1]
    assert model.positions["carga_1"][1] == model.positions["plano:PR1"][1] and model.positions["carga_2"][1] == model.positions["plano:PR2"][1]
    named = sorted(port for edge in model.edges for port in (edge.a_port, edge.b_port) if port)
    assert named == sorted(PORTS)
    glyphs = {key: node.glyph for key, node in model.nodes.items()}
    assert glyphs["vna"] == "vna" and glyphs["puerto:P1"] == "puerto" and glyphs["plano:PR2"] == "plano" and glyphs["kit_cal"] == "kit"
    assert glyphs["P1-1"] == "cable_medida" and glyphs["P1-2"] == "adaptador" and glyphs["carga_1"] == "carga_50"


def test_ports_of_the_eut_named_where_they_arrive(library):
    # the EUT of the template (cable, measurement cable, adapter or path) is not chosen yet: a generic component
    model = diagram(library, library.setups["cable_N_18G"])
    assert model.nodes["dut"].glyph == "generico" and model.nodes["dut"].coupler is None
    assert {edge.a_port or edge.b_port for edge in model.edges if "dut" in (edge.a, edge.b)} == {"A", "B"}
    assert diagram(library, setup_with_dut(library, "cable_N_18G", "CAB-DUT-0001")).nodes["dut"].glyph == "cable"
    # an EUT of any category, with ports of different names: no name until the test template chooses it
    impedance = diagram(library, library.setups["lisn_impedancia_VNA-01"])
    assert impedance.nodes["dut"].glyph == "generico"
    assert [edge for edge in impedance.edges if "dut" in (edge.a, edge.b) and (edge.a_port or edge.b_port)] == []
    nsa = diagram(library, library.setups["sac_nsa_3m"])
    assert nsa.nodes["antena_tx"].glyph == "biconica" and nsa.nodes["soporte_tx"].glyph == "mastil" and nsa.nodes["recinto"].glyph == "sac"


def test_each_pair_of_a_coupler_with_its_connections(qapp, library):
    template = library.procedures["acoplador_ACP-0001"]
    plan = prepare_run(library, template, TODAY)
    assert [measurement.name for measurement in plan.measurements] == PAIRS
    for measurement in plan.measurements:
        model = build_diagram_model(plan.template, plan.setup, library, plan.analysis, "medida", measurement.expanded)
        corners = model.nodes["dut"].coupler.corners
        first, second, *loaded = measurement.dut_ports
        assert corners[first] == "lt" and corners[second] == "lb" and {corners[port] for port in loaded} == {"rt", "rb"}
        assert not connection_picture(measurement.expanded, library).isNull()
    # the requests to connect the coupler say which pair they are for
    operator = AutoOperator()
    ProcedureRunner(library, template, operator, today=TODAY, seed=1).run()
    assert [request.measurement for request in operator.requests if request.step == "medida"] == PAIRS


def test_test_window_shows_the_connection_of_each_pair(qapp, library):
    window = TestWindow(library, library.procedures["acoplador_ACP-0001"], library.tests_folder, "Acoplador", "prueba", today=TODAY)
    prompt = PromptRequest("Par In – Coupl. Fwd (2/3)", "Conecte el DUT…", step="medida", measurement="In-Coupl. Fwd")
    assert window.prompt_picture(prompt) is not None
    window._prompted(prompt)  # the hardware setup tab: In on PR1, Coupl. Fwd on PR2
    corners = window.diagram.nodes["dut"].spec.coupler.corners
    assert corners["In"] == "lt" and corners["Coupl. Fwd"] == "lb"
    cable = TestWindow(library, library.procedures["cable_CAB-DUT-0002"], library.tests_folder, "Cable", "prueba", today=TODAY)
    assert cable.prompt_picture(PromptRequest("Conectar el DUT", "…", step="medida", measurement="DUT")) is None  # its drawing
    for test in (window, cable):
        test.close()
        test.deleteLater()


def test_arrange_nodes_forgets_the_ones_moved_by_hand(qapp, library):
    window = MainWindow(library)
    editor = window.open_setup("acoplador_N_18G")
    editor.diagram.nodes["dut"].setPos(900, 900)  # dragged by hand
    assert editor.setup.layout["dut"] == [900.0, 900.0] and editor.dirty
    editor.arrange_nodes()
    assert editor.setup.layout == {} and editor.diagram.nodes["dut"].pos().y() == editor.diagram.nodes["plano:PR1"].pos().y()
    assert "Recolocar\nnodos" in [action.text() for action in editor.ribbon_actions() if action is not None]
    editor.dirty = False
    window.close()
