import pytest
import yaml

from calelektra.catalog.library import Library, LibraryError
from calelektra.core.frequency import FrequencyRange


def test_example_library_loads_cleanly(library: Library):
    assert library.load_issues == []
    assert {"vna", "cable_medida", "antena", "posicionador"} <= set(library.categories)
    assert "rs_znb43" in library.models
    assert set(library.templates) == {"camino_rf_2p_s", "camino_rf_2p_s21", "impedancia_1p", "acoplador_4p", "sac_nsa", "sac_svswr"}
    assert {"cable_CAB-DUT-0002", "cable_CAB-DUT-0003", "cable_CAB-DUT-0001", "lisn_LISN-0001"} <= set(library.procedures)
    assert {"transmision_CAB-DUT-0001", "acoplador_ACP-0001"} <= set(library.procedures)
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


def test_inherited_template(library_copy):
    child = {
        "formato": "calelektra/plantilla_setup@0.4",
        "id": "impedancia_antenas",
        "extiende": "impedancia_1p",
        "nombre": "Impedancia de antenas",
        "rango_medida": ["30 MHz", "1 GHz"],
        "caminos": {"P1": {"etiqueta": "Camino de la antena", "categorias": ["cable_medida"]}},
        "roles": {"dut": {"categoria": "antena", "etiqueta": "Antena"}},
        "requisitos": [{"id": "vna_osm", "campo": "vna.metodos_calibracion", "operador": "contiene", "valor": "OSM", "gravedad": "aviso"}],
    }
    (library_copy / "hardware_setups" / "plantillas" / "impedancia_antenas.yaml").write_text(yaml.safe_dump(child, allow_unicode=True))
    library = Library.load(library_copy)
    template = library.resolved_template("impedancia_antenas")
    assert template.roles["dut"].categories == ["antena"] and template.roles["dut"].is_dut  # overridden, the rest inherited
    assert template.roles["kit_cal"].cardinality == "0..1"  # inherited
    assert template.paths["P1"].label == "Camino de la antena" and template.paths["P1"].categories == ["cable_medida"]
    assert template.algorithms == ["vna_1_puerto"] and template.default_range == FrequencyRange(30e6, 1e9)
    severities = {r.id: r.severity for r in template.requirements}
    assert severities == {"vna_puertos": "error", "vna_osm": "aviso"}  # parent requirements + own ones (same id: replaced)


def test_setups_of_format_0_3_are_not_loaded(library_copy):
    old = {
        "formato": "calelektra/setup@0.3",
        "id": "viejo",
        "nombre": "Setup 0.3",
        "plantilla": "camino_rf_2p_s",
        "rango_requerido": ["1 GHz", "2 GHz"],
        "asignaciones": {"vna": "VNA-01", "cable_p1": "CAB-PT-N-01"},
    }
    (library_copy / "hardware_setups" / "configurados" / "viejo.yaml").write_text(yaml.safe_dump(old, allow_unicode=True))
    library = Library.load(library_copy)
    assert "viejo" not in library.setups
    [issue] = [i for i in library.load_issues if i.code == "yaml.version"]
    assert "formato 0.3 anterior a 0.4" in issue.message and "caminos" in issue.message


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
