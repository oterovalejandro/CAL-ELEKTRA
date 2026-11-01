"""The calibration of a kit: the real values of its standards against their definition (``kits/calibration.py``).

The real values are those of the AA180-VNACKN of the laboratory, calibrated on 01/09/2026: six Touchstone
files (open, short and load, male and female; 9 kHz – 18 GHz, without uncertainty) in the example library
(``catalogo/patrones/KIT-N-02/``). The load of METAS VNA Tools serves as real values with uncertainty.
"""

from __future__ import annotations

import math
from datetime import date
from pathlib import Path

import numpy as np
import pytest
import yaml

from calelektra.catalog.model import Calibration
from calelektra.core.issues import Severity
from calelektra.kits import (
    RealValues,
    ResolvedStandard,
    SpecRow,
    UnitStandard,
    match_standard,
    standard_deviations,
    table_rows,
)
from calelektra.kits.calibration import QuantityDeviation
from calelektra.sdata import load_sparam_data

METAS_LOAD = Path(__file__).resolve().parent / "datos" / "metas" / "Example_Loadf_18831_20150824.sdatb"
FILES = {
    "OPENM resultados.s1p": "abierto_m",
    "OPENH resultados.s1p": "abierto_h",
    "SHORTM resultados.s1p": "cortocircuito_m",
    "SHORTH resultados.s1p": "cortocircuito_h",
    "LOADM resultados.s1p": "carga_m",
    "LOADH resultados.s1p": "carga_h",
}


def kit_files(library) -> Path:
    return library.root / "catalogo" / "patrones" / "KIT-N-02"


def resolved(library, standard_id: str, unit: UnitStandard | None = None) -> ResolvedStandard:
    kit = library.equipment["KIT-N-02"]
    model = library.model_of(kit)
    folder = kit.source_path.parent
    return ResolvedStandard(next(s for s in model.standards if s.id == standard_id), unit, model.source_path.parent, folder)


def test_each_file_is_for_its_standard(library):
    standards = library.model_of(library.equipment["KIT-N-02"]).standards
    for name, standard_id in FILES.items():
        assert match_standard(name, standards).id == standard_id
    assert match_standard("Open(f).s1p", standards).id == "abierto_h"
    assert match_standard("THRU(F-M).s2p", standards).id == "through_hm"
    assert match_standard("SHORT_FEMALE.s1p", standards).id == "cortocircuito_h"
    assert match_standard("certificado carga_h.s1p", standards).id == "carga_h"  # its identifier
    assert match_standard("abierto.s1p", standards) is None  # two opens
    assert match_standard("medida.s1p", standards) is None


def test_deviations_of_the_real_values_from_the_model(library):
    kit = library.equipment["KIT-N-02"]
    settings = library.kit_settings(library.model_of(kit), kit)
    # the male open: its phase deviates up to −14,1° at 18 GHz, against ±1,5° of its datasheet
    opened = library.standard_deviations(resolved(library, "abierto_m"), settings, kit_files(library) / "OPENM resultados.s1p")
    assert (len(opened.frequencies), opened.frequencies[0], opened.frequencies[-1]) == (116, 9e3, 18e9)
    magnitude, phase = opened.quantities
    assert (magnitude.column, phase.column, phase.parameter) == ("modulo_db", "fase_grados", "S11")
    k = int(np.argmin(abs(opened.frequencies - 1e9)))
    assert phase.deviation[k] == pytest.approx(-0.3824110716654465, abs=1e-9)  # −45,463° measured, −45,081° by the model
    assert phase.deviation[phase.worst] == pytest.approx(-14.11449932658904, abs=1e-9) and opened.frequencies[phase.worst] == 18e9
    assert magnitude.deviation[magnitude.worst] == pytest.approx(-0.033155442343835106, abs=1e-12)
    assert (magnitude.verdict, phase.verdict, opened.verdict) == ("sin_especificacion", "no_cumple", "no_cumple")  # magnitude: 0 dB = not specified
    verdicts = phase.verdicts()
    assert (verdicts == "no_cumple").sum() == 61 and opened.frequencies[np.argmax(verdicts == "no_cumple")] == 3e9
    assert not opened.uncertain and not np.any(phase.uncertainty)
    assert opened.summary() == [
        "S11, desviación de módulo: −0,033 dB a 17,5 GHz",
        "S11, desviación de fase: −14,114° a 18 GHz (especificación ±1,500°)",
    ]
    # the male load: |Γ − Γ₀| up to 0,0178 (−35,01 dB) against −31,45 dB (VSWR 1,055)
    load = library.standard_deviations(resolved(library, "carga_m"), settings, kit_files(library) / "LOADM resultados.s1p")
    distance = load.quantities[0]
    assert distance.distance and load.frequencies[distance.worst] == 17.75e9
    assert distance.deviation[distance.worst] == pytest.approx(0.0177684546826, rel=1e-12) and load.verdict == "cumple"
    assert float(distance.shown(distance.deviation[distance.worst])) == pytest.approx(-35.00700682037393)
    assert distance.limit[0] == pytest.approx(10 ** (-31.45 / 20))
    # the others meet it
    for name, standard_id in FILES.items():
        result = library.standard_deviations(resolved(library, standard_id), settings, kit_files(library) / name)
        assert result.verdict == ("no_cumple" if standard_id == "abierto_m" else "cumple"), standard_id


def test_own_specifications_from_the_deviations(library):
    kit = library.equipment["KIT-N-02"]
    settings = library.kit_settings(library.model_of(kit), kit)
    opened = library.standard_deviations(resolved(library, "abierto_m"), settings, kit_files(library) / "OPENM resultados.s1p")
    rows = opened.specifications(1e9)  # bands (0, 1 GHz], (1, 2 GHz]…: two rows each, at its first and last frequency
    assert len(rows) == 36 and [r.frequency for r in rows[:4]] == [9e3, 1e9, 1.25e9, 2e9]
    first = np.flatnonzero(opened.frequencies <= 1e9)
    assert rows[0].phase_deg == rows[1].phase_deg == pytest.approx(np.abs(opened.quantities[1].deviation[first]).max(), abs=1e-4)
    assert rows[0].phase_deg >= np.abs(opened.quantities[1].deviation[first]).max()  # rounded up
    assert (rows[-1].frequency, rows[-1].phase_deg, rows[-1].magnitude_db) == (18e9, 14.12, 0.03316)
    single = opened.specifications(None)
    assert [r.to_yaml() for r in single] == [
        {"frecuencia": "9 kHz", "modulo_db": 0.03316, "fase_grados": 14.12},
        {"frecuencia": "18 GHz", "modulo_db": 0.03316, "fase_grados": 14.12},
    ]
    load = library.standard_deviations(resolved(library, "carga_m"), settings, kit_files(library) / "LOADM resultados.s1p")
    assert load.specifications(None)[0].to_yaml() == {"frecuencia": "9 kHz", "perdidas_retorno_db": -35.0}  # toward 0
    # with them, the uncertainty of the standard covers its deviation: U = the band value (k = 2)
    unit = UnitStandard(valores_reales=RealValues(fichero="../patrones/KIT-N-02/OPENM resultados.s1p", ancho_bandas="1 GHz", resultado="no_cumple", especificaciones=rows))
    standard = resolved(library, "abierto_m", unit)
    assert standard.specification_origin == "valores_reales" and standard.specifications == rows
    data = library.standard_data(standard, settings)
    row = next(r for r in table_rows(data) if r.frequency == pytest.approx(17.5e9))
    assert row.u_phase_deg == pytest.approx(14.12) and row.u_magnitude == pytest.approx(abs(row.value) * math.log(10) / 20 * 0.03316)
    own = UnitStandard(especificaciones=[SpecRow(frecuencia=0, fase_grados=3)], valores_reales=unit.real_values)
    assert resolved(library, "abierto_m", own).specification_origin == "propias"  # the own ones of the unit, first


def test_real_values_with_their_uncertainty():
    """The load of METAS as the real values of a female load: U of |Γ − Γ₀| from its covariance (radial part)."""
    from calelektra.catalog.library import Library
    from calelektra.gui.app import EXAMPLE_LIBRARY

    library = Library.load(EXAMPLE_LIBRARY)
    standard = next(s for s in library.models["aa180_vnackn"].standards if s.id == "carga_h")
    data = load_sparam_data(METAS_LOAD)
    result = standard_deviations(standard, data)
    distance = result.quantities[0]
    assert result.uncertain and len(result.frequencies) == 533
    k = int(np.argmin(abs(result.frequencies - 10e9)))
    s = data.s[k, 0, 0]
    covariance = data.vector.covariance([data.element(k, 0, 0, 0), data.element(k, 0, 0, 1)])
    g = np.array([s.real, s.imag]) / abs(s)  # Γ₀ = 0: radial
    assert distance.deviation[k] == pytest.approx(abs(s)) and distance.uncertainty[k] == pytest.approx(2 * math.sqrt(g @ covariance @ g))
    verdicts = list(distance.verdicts())
    assert (verdicts.count("cumple"), verdicts.count("indeterminado"), verdicts.count("no_cumple")) == (506, 2, 25)  # to 45 GHz
    assert result.verdict == "no_cumple"
    undetermined = verdicts.index("indeterminado")
    bound, limit = distance.bound[undetermined], distance.limit[undetermined]
    assert distance.deviation[undetermined] - distance.uncertainty[undetermined] <= limit < bound
    assert result.specifications(None)[0].return_loss_db == -24.69  # |δ| + U


def test_verdicts_of_a_quantity():
    quantity = QuantityDeviation("fase_grados", "S11", np.array([1.0, -1.4, 1.6, 2.0]), np.array([0.2, 0.2, 0.2, 0.2]), np.full(4, 1.5))
    assert list(quantity.verdicts()) == ["cumple", "indeterminado", "indeterminado", "no_cumple"]
    assert quantity.verdict == "no_cumple" and quantity.worst == 3
    assert QuantityDeviation("modulo_db", "S11", np.array([0.1]), np.zeros(1), None).verdict == "sin_especificacion"


def test_the_calibration_of_the_kit_in_the_example(library):
    """KIT-N-02: calibrated on 01/09/2026, with the real values of its six standards."""
    kit = library.equipment["KIT-N-02"]
    assert kit.calibration.issued == date(2026, 9, 1)
    model = library.model_of(kit)
    standards = {s.id: s for s in library.kit_standards(model, kit)}
    for name, standard_id in FILES.items():
        real = standards[standard_id].real_values
        assert real.file == f"../patrones/KIT-N-02/{name}" and real.calibration_date == date(2026, 9, 1) and real.band_width == 1e9
        assert standards[standard_id].real_values_path().is_file() and len(real.sha256) == 64
        assert standards[standard_id].specification_origin == "valores_reales" and len(real.specifications) == 36
        assert real.verdict == ("no_cumple" if standard_id == "abierto_m" else "cumple")
    # the stored specifications are those of its deviations
    settings = library.kit_settings(model, kit)
    computed = library.standard_deviations(standards["abierto_m"], settings).specifications(1e9)
    assert [r.to_yaml() for r in computed] == [r.to_yaml() for r in standards["abierto_m"].real_values.specifications]
    issues = library.kit_issues(kit)
    assert [(i.code, i.severity) for i in issues] == [("patron.fuera_de_especificacion", Severity.WARNING)]
    assert "OPEN -M-" in issues[0].message and "01/09/2026" in issues[0].message
    # saved and read again
    library.save_equipment(kit)
    saved = yaml.safe_load(kit.source_path.read_text(encoding="utf-8"))
    real = saved["patrones"]["abierto_m"]["valores_reales"]
    assert (real["fecha"], real["ancho_bandas"], real["resultado"]) == ("2026-09-01", "1 GHz", "no_cumple")
    assert real["especificaciones"][-1] == {"frecuencia": "18 GHz", "modulo_db": 0.03316, "fase_grados": 14.12}
    # a newer certificate, and a file that is missing
    kit.calibration = Calibration(fecha=date(2027, 9, 1))
    (kit_files(library) / "LOADH resultados.s1p").unlink()
    codes = [(i.code, i.severity) for i in library.kit_issues(kit)]
    assert ("patron.valores_reales", Severity.ERROR) in codes and codes.count(("patron.valores_antiguos", Severity.WARNING)) == 6
