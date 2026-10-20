import pytest
import yaml

from calelektra.catalog.library import Library, LibraryError
from calelektra.core.frequency import FrequencyRange


def test_example_library_loads_cleanly(library: Library):
    assert library.load_issues == []
    assert {"vna", "cable_medida", "antena", "posicionador"} <= set(library.categories)
    assert "rs_znb43" in library.models
    assert {"cable_vna", "componente_2p", "reflexion_1p", "lisn_impedancia", "sac_nsa", "sac_svswr"} <= set(library.templates)
    assert {"cable_CAB-DUT-0002", "cable_CAB-DUT-0003", "cable_CAB-DUT-0001", "lisn_LISN-0001"} <= set(library.procedures)
    assert library.procedures["cable_CAB-DUT-0002"].setup_id == "cable_vna_292"  # a test template calls a hardware setup…
    assert library.procedures["cable_CAB-DUT-0002"].eut_id == "CAB-DUT-0002"  # … and defines the EUT
    assert library.tests() == []


def test_equipment_range_comes_from_model_and_s_parameters(library: Library):
    vna = library.frequency_range_of(library.equipment["VNA-01"])
    assert vna.range == FrequencyRange(100e3, 43.5e9)
    cable = library.frequency_range_of(library.equipment["CAB-PT-N-01"])
    # model DC–18 GHz ∩ S2P 5 Hz–18 GHz
    assert cable.range.start == pytest.approx(5.0)
    assert cable.range.stop == pytest.approx(18e9)
    assert any("Datos S CAB-PT-N-01.s2p" in source for source in cable.sources)


def test_inherited_template(library: Library):
    template = library.resolved_template("lisn_impedancia")
    assert template.roles["dut"].categories == ["lisn"]
    assert template.roles["adaptador_p1"].categories == ["adaptador"]  # inherited
    assert template.roles["adaptador_p1"].cardinality == "1"  # overridden
    assert "carga_receptor" in template.roles
    assert template.default_range == FrequencyRange(9e3, 30e6)
    ids = [r.id for r in template.requirements]
    assert ids[:2] == ["vna_puertos", "vna_osm"] and "carga_50" in ids  # parent requirements + own ones
    assert template.reference_planes["PR1"]["en"] == ["cable_p1.B"]  # overridden plane


def test_broken_files_are_reported(library_copy):
    (library_copy / "catalogo/equipos/MAL.yaml").write_text("formato: calelektra/equipo@0.2\nid: MAL\n")
    (library_copy / "catalogo/equipos/OTRO.yaml").write_text("formato: calelektra/modelo@0.2\nid: X\n")
    (library_copy / "catalogo/equipos/HUERFANO.yaml").write_text(
        "formato: calelektra/equipo@0.2\nid: HUERFANO\nmodelo: no_existe\n"
    )
    library = Library.load(library_copy)
    codes = {issue.code for issue in library.load_issues}
    assert {"yaml.invalido", "yaml.formato", "equipo.modelo"} <= codes
    assert "MAL" not in library.equipment


def test_save_equipment_updates_s_parameter_reference(library: Library):
    cable = library.equipment["CAB-PT-N-01"]
    cable.s_parameters.reference = "Certificado 2026-123"
    path = library.save_equipment(cable)
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert data["datos_s"]["referencia"] == "Certificado 2026-123"
    reloaded = Library.load(library.root)
    assert reloaded.equipment["CAB-PT-N-01"].s_parameters.reference == "Certificado 2026-123"
    assert reloaded.load_issues == []


def test_import_yaml(library: Library, tmp_path):
    source = tmp_path / "VNA-02.yaml"
    source.write_text("formato: calelektra/equipo@0.2\nid: VNA-02\nmodelo: rs_znb43\nconexion: { simulado: true }\n")
    kind, target = library.import_yaml(source)
    assert kind == "equipo" and target.exists()
    assert "VNA-02" in library.equipment
    bad = tmp_path / "bad.yaml"
    bad.write_text("formato: otra_cosa\n")
    with pytest.raises(LibraryError):
        library.import_yaml(bad)
