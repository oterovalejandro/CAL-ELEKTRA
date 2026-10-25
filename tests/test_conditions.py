"""Ambient conditions of the TPH probe, pending uncertainty and the package of a test."""

from __future__ import annotations

import io
import zipfile
from datetime import datetime, timedelta

import numpy as np
import openpyxl
import pytest
import yaml
from conftest import E5061B_VALID_DAY, TODAY, renew_calibration, tph_workbook

from calelektra.core.touchstone import read_touchstone
from calelektra.procedures.ambient import (
    AmbientError,
    AmbientReading,
    read_tph_workbook,
    write_tph_workbook,
)
from calelektra.procedures.engine import AutoOperator, ProcedureRunner
from calelektra.procedures.package import SUMMARY_FILE, save_test_package, summary_text
from calelektra.procedures.record import SETUP_SNAPSHOT, TEMPLATE_SNAPSHOT
from calelektra.procedures.record_uncertainty import is_pending, load_ambient_conditions
from calelektra.procedures.results import db
from calelektra.uncertainty import UncertaintyEngine

START = datetime(2026, 10, 23, 10, 0, 45)


def readings(minutes: int, temperature=lambda i: 24.2) -> list[AmbientReading]:
    return [AmbientReading(START + timedelta(minutes=i), temperature(i), 942.6, 37.0) for i in range(minutes)]


def test_reader_of_the_probe_workbook(tmp_path):
    path = write_tph_workbook(tmp_path / "Condiciones_ambientales.xlsx", readings(30, lambda i: 22.0 + i / 10), "VIRTUAL LAB: Datos sonda TPH")
    log = read_tph_workbook(path)
    assert len(log.readings) == 30 and log.query == "VIRTUAL LAB: Datos sonda TPH"
    assert log.readings[0] == AmbientReading(START, 22.0, pytest.approx(942.6), 37.0)  # Pa → hPa
    # a test between two readings: the readings just before and just after cover it
    conditions = log.conditions(START + timedelta(minutes=10, seconds=10), START + timedelta(minutes=10, seconds=40))
    assert conditions["estado"] == "cargadas" and conditions["lecturas"] == 2
    assert conditions["temperatura_c"] == {"min": 23.0, "max": 23.1, "media": 23.05}
    assert conditions["presion_hpa"]["media"] == 942.6 and conditions["humedad_pct"]["max"] == 37.0
    longer = log.conditions(START + timedelta(minutes=5), START + timedelta(minutes=8))
    assert longer["lecturas"] == 4 and longer["lecturas_desde"] == "2026-10-23 10:05:45"
    with pytest.raises(AmbientError, match="no cubre el ensayo"):
        log.conditions(START + timedelta(hours=2), START + timedelta(hours=2, minutes=5))
    with pytest.raises(AmbientError, match="no cubre el ensayo"):
        log.conditions(START - timedelta(minutes=20), START + timedelta(minutes=3))


def test_reader_finds_the_header_and_converts_units(tmp_path):
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.append(["Exportación de la sonda"])
    sheet.append([])
    sheet.append(["Fecha", "Temperatura", "TUNIT", "Presión", "PUNIT", "Humedad", "HUNIT"])
    sheet.append(["23/10/2026 10:00:45", "297,15", "K", "0,9426", "bar", "37,5", "%"])
    sheet.append(["23/10/2026 10:01:45", "75,2", "F", "942,6", "mbar", "38", "%"])
    sheet.append(["sin fecha", "1", "C", None, None, None, None])
    workbook.save(tmp_path / "sonda.xlsx")
    log = read_tph_workbook(tmp_path / "sonda.xlsx")
    assert [r.temperature_c for r in log.readings] == [pytest.approx(24.0), pytest.approx(24.0)]
    assert [r.pressure_hpa for r in log.readings] == [pytest.approx(942.6), pytest.approx(942.6)]
    assert log.readings[0].humidity_pct == 37.5 and log.query is None
    (tmp_path / "otro.xlsx").write_text("no es un libro")
    with pytest.raises(AmbientError, match="No se puede leer"):
        read_tph_workbook(tmp_path / "otro.xlsx")
    empty = openpyxl.Workbook()
    empty.active.append(["A", "B"])
    empty.save(tmp_path / "vacio.xlsx")
    with pytest.raises(AmbientError, match="no tiene lecturas"):
        read_tph_workbook(tmp_path / "vacio.xlsx")


def run_cable(library):
    return ProcedureRunner(library, library.procedures["cable_CAB-DUT-0002"], AutoOperator(), today=TODAY, seed=2).run()


def test_uncertainty_is_pending_until_the_conditions_are_loaded(library, tmp_path):
    result = run_cable(library)
    record = result.record
    entry = record.uncertainty["medidas"][0]
    csv_file = record.folder / entry["fichero"]
    provisional = csv_file.read_text(encoding="utf-8-sig")
    assert is_pending(record) and record.uncertainty["temperatura_c"].startswith("pendiente")
    # a workbook of another day does not cover the test: nothing changes
    other_day = tph_workbook(tmp_path / "otro_dia.xlsx", record.started - timedelta(days=1), record.finished - timedelta(days=1))
    with pytest.raises(AmbientError):
        load_ambient_conditions(library, record, other_day)
    assert is_pending(record) and record.conditions == {"estado": "pendiente"}

    message = load_ambient_conditions(library, record, tph_workbook(tmp_path / "Condiciones_ambientales.xlsx", record.started, record.finished))
    assert "23 – 23 °C" in message and "Incertidumbre recalculada" in message
    saved = library.load_test(record.folder)  # the record is saved
    assert saved.conditions["estado"] == "cargadas" and saved.conditions["temperatura_c"]["max"] == 23.0
    assert saved.conditions["fichero"] == "condiciones/Condiciones_ambientales.xlsx" and saved.conditions["fichero"] in saved.files
    assert (record.folder / saved.conditions["fichero"]).is_file()
    assert saved.uncertainty["estado"] == "completa" and saved.uncertainty["temperatura_c"] == [23.0, 23.0]
    assert saved.uncertainty["fichero"] == record.uncertainty["fichero"] and saved.uncertainty["vna"].startswith("VNA-01")
    assert "incertidumbre pendiente" not in saved.message
    step = next(s for s in saved.steps if s.key == "incertidumbre")
    assert step.status == "ok" and "con las condiciones ambientales (23 – 23 °C)" in step.detail
    # inside the reference range of the file (18 – 28 °C) the temperature adds nothing: same values
    assert csv_file.read_text(encoding="utf-8-sig") == provisional


def test_temperature_outside_the_reference_range_adds_its_component(library, tmp_path):
    result = run_cable(library)
    record = result.record
    provisional = {p: t.magnitude_db.copy() for p, t in result.uncertainty["DUT"].items()}
    load_ambient_conditions(library, record, tph_workbook(tmp_path / "caluroso.xlsx", record.started, record.finished, temperature=30.5))
    assert record.uncertainty["estado"] == "completa" and record.uncertainty["temperatura_c"] == [30.5, 30.5]
    entry = record.uncertainty["medidas"][0]
    from calelektra.gui.test_common import read_uncertainty_csv

    final = read_uncertainty_csv(record.folder / entry["fichero"])
    s21 = final["S21"][0]
    valid = np.isfinite(s21) & np.isfinite(provisional["S21"])
    assert valid.sum() > 100 and np.all(s21[valid] > provisional["S21"][valid])
    # each point is the laboratory algorithm with the temperature of the test (ΔT = 2.5 K over 28 °C)
    data = read_touchstone(record.folder / entry["fichero_resultado"])
    engine = UncertaintyEngine(record.folder / record.uncertainty["fichero"])
    ifbw = record.launched_template().sweep.trace("S21")[0]
    k = int(np.flatnonzero(valid)[len(np.flatnonzero(valid)) // 2])
    expected = engine.calculate(
        record.uncertainty["designacion"], "S21", data.frequencies[k], float(db(data.s[k, 1, 0])), "dB",
        temp_min_c=30.5, temp_max_c=30.5, if_bandwidth_hz=ifbw, today=TODAY,
    )
    assert s21[k] == pytest.approx(expected.expanded_uncertainty, rel=1e-6)
    results_csv = (record.folder / entry["fichero_resultado"]).with_suffix(".csv").read_text(encoding="utf-8-sig")
    assert "U S21 (dB, k=2)" in results_csv.splitlines()[0]


def test_lisn_recalculation_checks_the_calibration_on_the_day_of_the_test(library, tmp_path):
    renew_calibration(library, "VNA-BF-01")
    record = ProcedureRunner(library, library.procedures["lisn_LISN-0001"], AutoOperator(), today=E5061B_VALID_DAY, seed=1).run().record
    assert is_pending(record) and record.uncertainty["dia_comprobacion"] == "2026-07-15"
    warnings = record.uncertainty.get("avisos")
    load_ambient_conditions(library, record, tph_workbook(tmp_path / "tph.xlsx", record.started, record.finished))
    assert record.uncertainty["estado"] == "completa" and record.uncertainty.get("avisos") == warnings
    [entry] = record.uncertainty["medidas"]  # the EUT terminal (generic impedance in 1 port)
    assert entry["fichero_resultado"].endswith(".s1p") and (record.folder / entry["fichero"]).is_file()
    assert entry["impedancia"]["U_ohm"]["max"] > 0  # the uncertainty of Z is calculated again too
    header = (record.folder / entry["fichero"]).read_text(encoding="utf-8-sig").splitlines()[0]
    assert "U+ |Z| (Ω, k=2)" in header
    assert "DUT Z: U |Z|" in summary_text(record)


def test_package_keeps_all_the_information_of_the_test(library, tmp_path):
    result = run_cable(library)
    record = result.record
    target = save_test_package(record, tmp_path / "entregas" / "cable")  # .zip added
    assert target.name == "cable.zip"
    with zipfile.ZipFile(target) as package:
        names = set(package.namelist())
        root = record.id
        for name in (SUMMARY_FILE, "ensayo.yaml", SETUP_SNAPSHOT, TEMPLATE_SNAPSHOT, "registro.txt", *record.files):
            assert f"{root}/{name}" in names
        summary = package.read(f"{root}/{SUMMARY_FILE}").decode("utf-8")
        setup = yaml.safe_load(package.read(f"{root}/{SETUP_SNAPSHOT}"))
        template = yaml.safe_load(package.read(f"{root}/{TEMPLATE_SNAPSHOT}"))
        s2p_name = next(f for f in record.files if f.endswith(".s2p"))
        s2p = package.read(f"{root}/{s2p_name}").decode("utf-8")
    assert f"Inicio: {record.started:%Y-%m-%d %H:%M:%S}" in summary and f"Fin: {record.finished:%Y-%m-%d %H:%M:%S}" in summary
    assert "PENDIENTES: no se ha cargado el fichero de la sonda TPH" in summary and "Estado: PENDIENTE" in summary
    assert setup["setup"]["id"] == record.setup["id"] and {e["id"] for e in setup["equipos"]} >= {"VNA-01", "CAB-DUT-0002"}
    assert template["id"] == "cable_CAB-DUT-0002" and template["hardware_setup"] == record.setup["id"]
    # the traces as measured: the display smoothing of the template (S11, S22: 1 %) is not applied
    assert template["barrido"]["trazas"]["S11"]["suavizado_pct"] == 1
    path = tmp_path / "medida.s2p"
    path.write_text(s2p)
    assert np.allclose(read_touchstone(path).s, result.data["DUT"], rtol=1e-6, atol=1e-9)

    load_ambient_conditions(library, record, tph_workbook(tmp_path / "tph.xlsx", record.started, record.finished))
    with zipfile.ZipFile(save_test_package(record, tmp_path / "cable.zip")) as package:
        summary = package.read(f"{record.id}/{SUMMARY_FILE}").decode("utf-8")
        assert f"{record.id}/condiciones/tph.xlsx" in package.namelist()
    assert "Temperatura: 23 – 23 °C (media 23)" in summary and "Estado: completa" in summary and "Humedad: " in summary


def test_package_of_a_test_without_snapshots(library, tmp_path):
    record = run_cable(library).record
    for name in (SETUP_SNAPSHOT, TEMPLATE_SNAPSHOT):  # a test made with an older version
        (record.folder / name).unlink()
        record.files.remove(name)
    with zipfile.ZipFile(save_test_package(record, tmp_path / "antiguo.zip")) as package:
        template = yaml.safe_load(io.BytesIO(package.read(f"{record.id}/{TEMPLATE_SNAPSHOT}")))
        setup = yaml.safe_load(io.BytesIO(package.read(f"{record.id}/{SETUP_SNAPSHOT}")))
    assert template["id"] == "cable_CAB-DUT-0002" and setup["setup"]["id"] == record.setup["id"]
