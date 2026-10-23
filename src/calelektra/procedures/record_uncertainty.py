"""Uncertainty of the traces of a test and the ambient conditions it depends on.

At the end of the run the uncertainty is calculated **without the temperature
component** and stays «pendiente» until the ambient conditions of the test are
loaded (workbook of the TPH probe, ``procedures/ambient.py``). Then it is
calculated again with the temperature of the test: the stability with
temperature counts when the temperature leaves the reference range of the
uncertainty file of the VNA. The rest of the calculation is the same, so a
test whose temperature stays inside the reference range keeps its values and
only changes from «pendiente» to «completa».
"""

from __future__ import annotations

import re
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np

from calelektra.core.touchstone import read_touchstone
from calelektra.procedures.ambient import AmbientLog, read_tph_workbook
from calelektra.procedures.model import ProcedureTemplate
from calelektra.procedures.record import TestRecord
from calelektra.procedures.results import (
    uncertainty_csv_path,
    write_results_csv_1p,
    write_results_csv_2p,
    write_uncertainty_csv,
)
from calelektra.uncertainty.engine import UncertaintyCalculationError, UncertaintyEngine
from calelektra.uncertainty.traces import (
    TraceUncertainty,
    UncertaintyOptions,
    dataset_uncertainty,
)

if TYPE_CHECKING:
    from calelektra.catalog.library import Library

PENDING, COMPLETE = "pendiente", "completa"
CONDITIONS_FOLDER = "condiciones"
PENDING_NOTE = " — incertidumbre pendiente de las condiciones ambientales"


@dataclass
class UncertaintyRun:
    """Uncertainty of the measurements of a test, with the section of the record that describes it."""

    entry: dict[str, Any]
    traces: dict[str, dict[str, TraceUncertainty]]  # measurement → trace → uncertainty
    status: str  # step status: ok | aviso
    detail: str
    problems: list[str] = field(default_factory=list)


def conditions_loaded(record_or_conditions: TestRecord | dict) -> bool:
    conditions = record_or_conditions.conditions if isinstance(record_or_conditions, TestRecord) else record_or_conditions
    return conditions.get("estado") == "cargadas"


def is_pending(record: TestRecord) -> bool:
    """The uncertainty of the test waits for its ambient conditions."""
    return record.uncertainty.get("estado") == PENDING


def temperature_of(conditions: dict) -> tuple[float, float] | None:
    temperature = conditions.get("temperatura_c") if conditions_loaded(conditions) else None
    return (float(temperature["min"]), float(temperature["max"])) if temperature else None


def _largest(arrays: list[np.ndarray]) -> float | None:
    values = np.concatenate(arrays) if arrays else np.array([])
    values = values[np.isfinite(values)]
    return float(values.max()) if len(values) else None


def compute_uncertainty(
    engine: UncertaintyEngine,
    designation: str,
    frequencies: np.ndarray,
    data: dict[str, np.ndarray],
    template: ProcedureTemplate,
    conditions: dict,
    test_day: date,
    on_measurement: Callable[[str, dict[str, TraceUncertainty]], None] | None = None,
) -> UncertaintyRun:
    """Uncertainty of every measurement of a test with the settings of its template and its conditions."""
    settings = template.uncertainty
    temperature = temperature_of(conditions)
    state = COMPLETE if temperature is not None or not settings.temperature_stability else PENDING
    options = UncertaintyOptions(
        settings.coverage_factor, settings.trace_noise, settings.temperature_stability, temperature if settings.temperature_stability else None
    )
    due, _valid, warnings = engine.calibration_status(test_day)
    if temperature is not None and settings.temperature_stability:
        temperature_text: Any = list(temperature)
    elif state == PENDING:
        temperature_text = "pendiente de las condiciones ambientales (sin componente de temperatura)"
    else:
        temperature_text = "no se aplica (estabilidad con la temperatura desactivada en la plantilla)"
    entry: dict[str, Any] = {
        "estado": state,
        "documento": engine.document,
        "designacion": designation,
        "k": settings.coverage_factor,
        "ruido_traza": settings.trace_noise,
        "estabilidad_temperatura": settings.temperature_stability,
        "temperatura_c": temperature_text,
        "calibracion_vna_hasta": due,
        "dia_comprobacion": test_day.isoformat(),  # day the calibration of the uncertainty file is checked on
        "calculada": f"{datetime.now():%Y-%m-%d %H:%M:%S}",
    }
    if warnings:
        entry["avisos"] = warnings
    traces: dict[str, dict[str, TraceUncertainty]] = {}
    measurements = []
    problems: list[str] = list(warnings)
    missing = 0
    for name, s in data.items():
        traces[name] = dataset_uncertainty(engine, designation, frequencies, s, lambda trace: template.sweep.trace(trace)[0], options, test_day)
        if on_measurement is not None:
            on_measurement(name, traces[name])
        measurements.append({"medida": name, "trazas": {p: t.summary() for p, t in traces[name].items()}})
        for trace in traces[name].values():
            missing += trace.missing
            problems += [f"{name}: {problem}" if len(data) > 1 else problem for problem in trace.problems]
    entry["medidas"] = measurements
    largest = []
    for parameter in dict.fromkeys(p for found in traces.values() for p in found):
        found = [t[parameter] for t in traces.values() if parameter in t]
        db = _largest([t.magnitude_db for t in found])
        deg = _largest([t.phase_deg for t in found])
        if db is not None:
            largest.append(f"{parameter} {db:.3g} dB" + (f" / {deg:.3g}°" if deg is not None else ""))
    detail = f"U (k={settings.coverage_factor:g}) máx.: " + ("; ".join(largest) or "ningún punto")
    if state == PENDING:
        detail += " — PROVISIONAL, sin la componente de temperatura: pendiente de las condiciones ambientales"
    elif temperature is not None and settings.temperature_stability:
        detail += f" — con las condiciones ambientales ({temperature[0]:g} – {temperature[1]:g} °C)"
    if missing:
        detail += f" — {missing} punto(s) sin incertidumbre (fuera de los rangos del fichero)"
    if warnings:
        detail += " — VNA fuera de calibración según el fichero"
    status = "aviso" if state == PENDING or missing or warnings else "ok"
    return UncertaintyRun(entry, traces, status, detail, problems)


def designation_of(entry: dict) -> str:
    """Manufacturer and model of the VNA kept in the record (older records only have the «vna» text)."""
    if entry.get("designacion"):
        return str(entry["designacion"])
    match = re.match(r".*? — (.*?)(?: nº .*)?$", str(entry.get("vna", "")))
    return match.group(1) if match else str(entry.get("vna", ""))


def recalculate_uncertainty(record: TestRecord) -> str:
    """Calculate again the uncertainty of a test with its current conditions; rewrites its CSV files."""
    entry = record.uncertainty
    folder = record.folder
    if folder is None or not entry.get("medidas"):
        raise UncertaintyCalculationError("El ensayo no tiene incertidumbre calculada")
    template = record.launched_template()
    if template is None:
        raise UncertaintyCalculationError("El registro no guarda la plantilla de ensayo tal como se lanzó")
    engine = UncertaintyEngine(folder / entry["fichero"])  # the copy kept with the test
    measurements: dict[str, tuple[Path, np.ndarray, np.ndarray]] = {}
    for item in entry["medidas"]:
        path = folder / item["fichero_resultado"]
        data = read_touchstone(path)
        measurements[item["medida"]] = (path, data.frequencies, data.s)
    frequencies = next(iter(measurements.values()))[1]
    try:
        test_day = date.fromisoformat(str(entry["dia_comprobacion"]))
    except (KeyError, ValueError):
        test_day = record.started.date()
    run = compute_uncertainty(
        engine, designation_of(entry), frequencies, {name: s for name, (_p, _f, s) in measurements.items()}, template, record.conditions, test_day
    )
    k = template.uncertainty.coverage_factor
    network = record.results.get("red_nominal") if isinstance(record.results, dict) else None
    for item, new in zip(entry["medidas"], run.entry["medidas"]):
        path, f, s = measurements[item["medida"]]
        traces = run.traces[item["medida"]]
        write_uncertainty_csv(uncertainty_csv_path(path), f, traces, k)
        results_csv = path.with_suffix(".csv")
        if results_csv.is_file():
            if s.shape[1] == 2:
                write_results_csv_2p(results_csv, f, s, traces, k)
            else:
                write_results_csv_1p(results_csv, f, s[:, 0, 0], network, traces, k)
        new["fichero_resultado"], new["fichero"] = item["fichero_resultado"], item.get("fichero", uncertainty_csv_path(path).name)
    record.uncertainty = {**run.entry, **{key: entry[key] for key in ("fichero", "vna", "equipo_vna") if key in entry}}
    for step in record.steps:
        if step.key == "incertidumbre":
            step.status, step.detail = run.status, run.detail
    if record.message:
        record.message = record.message.replace(PENDING_NOTE, "")
        if is_pending(record):
            record.message += PENDING_NOTE
    return run.detail


def load_ambient_conditions(library: Library, record: TestRecord, workbook: str | Path | AmbientLog) -> str:
    """Ambient conditions of a test from the probe workbook; the uncertainty is calculated again with them.

    The workbook is kept in ``condiciones/`` of the test. Raises ``AmbientError`` if it does not
    cover the test.
    """
    if record.folder is None:
        raise ValueError("El ensayo no tiene carpeta")
    log = workbook if isinstance(workbook, AmbientLog) else read_tph_workbook(workbook)
    start = record.started
    end = record.finished or record.started
    conditions = log.conditions(start, end)
    target = record.folder / CONDITIONS_FOLDER / log.source.name
    target.parent.mkdir(parents=True, exist_ok=True)
    if log.source.resolve() != target.resolve():
        shutil.copy2(log.source, target)
    conditions["fichero"] = f"{CONDITIONS_FOLDER}/{log.source.name}"
    record.conditions = conditions
    if conditions["fichero"] not in record.files:
        record.files.append(conditions["fichero"])
    temperature = conditions["temperatura_c"]
    message = f"Condiciones ambientales: {temperature['min']:g} – {temperature['max']:g} °C"
    if conditions.get("humedad_pct"):
        message += f", {conditions['humedad_pct']['min']:g} – {conditions['humedad_pct']['max']:g} % HR"
    if conditions.get("presion_hpa"):
        message += f", {conditions['presion_hpa']['media']:g} hPa"
    message += f" ({conditions['lecturas']} lecturas)"
    if record.uncertainty.get("medidas"):
        message += f".\nIncertidumbre recalculada: {recalculate_uncertainty(record)}"
    library.save_test(record)
    return message
