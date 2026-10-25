"""Package with all the information of a test, to keep or deliver it (ZIP).

Contents, under a folder named after the test:

- ``resumen_ensayo.txt``: test, template, hardware setup and its equipment,
  start and end times, ambient conditions, uncertainty, verdict and files;
- ``ensayo.yaml``: the record;
- ``plantilla_ensayo.yaml`` and ``hardware_setup.yaml``: the template and the
  hardware setup as launched;
- the results: Touchstone files with the traces as measured (no smoothing),
  CSV, uncertainty CSV, and the files kept with the test (uncertainty file of
  the VNA, specifications, frequency list, probe workbook, log).
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import yaml

from calelektra.procedures.record import (
    RECORD_FILE,
    SETUP_SNAPSHOT,
    TEMPLATE_SNAPSHOT,
    TestRecord,
)
from calelektra.procedures.record_uncertainty import conditions_loaded, is_pending
from calelektra.procedures.specs import VERDICTS
from calelektra.uncertainty.impedance import impedance_text
from calelektra.setups.paths import describe_path_record

SUMMARY_FILE = "resumen_ensayo.txt"


def _duration(record: TestRecord) -> str:
    if record.finished is None:
        return "—"
    seconds = int((record.finished - record.started).total_seconds())
    return f"{seconds // 3600:d} h {seconds % 3600 // 60:02d} min {seconds % 60:02d} s"


def summary_text(record: TestRecord) -> str:
    """Readable summary of a test."""
    lines = [f"CAL-ELEKTRA — ensayo {record.title} [{record.id}]", ""]
    if record.simulated:
        lines += ["DATOS SIMULADOS (VNA simulado): no es una medida real", ""]
    template = record.template
    lines += [
        f"Estado: {record.status_name}" + (f" — {record.message}" if record.message else ""),
        f"Veredicto: {VERDICTS.get(record.verdict, record.verdict) if record.verdict else '—'}",
        f"Operador: {record.operator or '—'}",
        f"Inicio: {record.started:%Y-%m-%d %H:%M:%S}",
        f"Fin: {record.finished:%Y-%m-%d %H:%M:%S}" if record.finished else "Fin: —",
        f"Duración: {_duration(record)}",
        "",
        "Plantilla de ensayo",
        f"  {template.get('nombre', record.procedure.get('nombre', '—'))} (id {template.get('id', '—')}, v{template.get('version', '—')}) — {TEMPLATE_SNAPSHOT}",
        "",
        "Hardware setup",
        f"  {record.setup.get('nombre', '—')} (id {record.setup.get('id', '—')}, plantilla {record.setup.get('plantilla', '—')}) — {SETUP_SNAPSHOT}",
    ]
    frequencies = record.sweep or {}
    if frequencies:
        lines.append(f"  Frecuencias: {frequencies.get('modo', '')} {frequencies.get('inicio')} – {frequencies.get('fin')}, {frequencies.get('puntos')} puntos")
    if record.paths:
        lines.append("  Caminos (del puerto del VNA al plano de referencia):")
        for path in record.paths:
            lines.append(f"    {path.get('etiqueta', 'Camino')}: {describe_path_record(path)}")
            for element in path.get("elementos", []):
                text = f"      {element.get('posicion')}. {element.get('id')} — {element.get('modelo', '')}"
                if element.get("numero_serie"):
                    text += f", nº {element['numero_serie']}"
                if element.get("certificado"):
                    text += f", certificado {element['certificado']}"
                if element.get("datos_s"):
                    text += f", datos S {element['datos_s']}"
                lines.append(text)
        if record.setup.get("conectores_dut_admitidos"):
            lines.append(f"  Conectores de DUT admitidos: {record.setup['conectores_dut_admitidos']}")
    for equipment in record.equipment:
        if equipment.get("camino"):
            continue  # listed with its path
        text = f"  {equipment.get('rol_nombre', equipment.get('rol'))}: {equipment.get('id')} — {equipment.get('modelo')}"
        if equipment.get("numero_serie"):
            text += f", nº {equipment['numero_serie']}"
        if equipment.get("certificado"):
            text += f", certificado {equipment['certificado']}" + (f" (vence {equipment['calibracion_vence']})" if equipment.get("calibracion_vence") else "")
        lines.append(text)
    for measurement in record.measurements:
        ports = ", ".join(f"{port} en {path}" for path, port in measurement.get("puertos_eut", {}).items())
        loads = f"; en carga: {', '.join(measurement['puertos_en_carga'])}" if measurement.get("puertos_en_carga") else ""
        lines.append(f"  Medida {measurement.get('nombre')}: {measurement.get('fichero')} ({ports}{loads})")
    if record.dut and record.dut.get("id") not in {equipment.get("id") for equipment in record.equipment}:
        lines.append(f"  EUT: {record.dut.get('id')} — {record.dut.get('modelo')}" + (f", nº {record.dut['numero_serie']}" if record.dut.get("numero_serie") else ""))
    lines += ["", "Condiciones ambientales"]
    conditions = record.conditions
    if conditions_loaded(conditions):
        for key, label, unit in (("temperatura_c", "Temperatura", "°C"), ("humedad_pct", "Humedad", "% HR"), ("presion_hpa", "Presión", "hPa")):
            values = conditions.get(key)
            if values:
                lines.append(f"  {label}: {values['min']:g} – {values['max']:g} {unit} (media {values['media']:g})")
        lines.append(f"  {conditions.get('lecturas')} lecturas de la sonda TPH ({conditions.get('lecturas_desde')} – {conditions.get('lecturas_hasta')}), fichero {conditions.get('fichero', '—')}")
    else:
        lines.append("  PENDIENTES: no se ha cargado el fichero de la sonda TPH")
    lines += ["", "Incertidumbre"]
    uncertainty = record.uncertainty
    if uncertainty:
        state = "PENDIENTE de las condiciones ambientales (provisional, sin componente de temperatura)" if is_pending(record) else "completa"
        lines += [
            f"  Estado: {state}",
            f"  k = {uncertainty.get('k', 2):g} · {uncertainty.get('documento', '—')} · {uncertainty.get('vna', '—')}",
        ]
        for measurement in uncertainty.get("medidas", []):
            for parameter, trace in measurement.get("trazas", {}).items():
                db, deg = trace.get("U_db"), trace.get("U_grados")
                text = f"  {measurement.get('medida')} {parameter}: U {db['min']:g} – {db['max']:g} dB" if db else f"  {measurement.get('medida')} {parameter}: —"
                if deg:
                    text += f", {deg['min']:g} – {deg['max']:g}°"
                lines.append(text)
            if measurement.get("impedancia"):
                lines.append(f"  {measurement.get('medida')} Z: {impedance_text(measurement['impedancia'])}")
    else:
        lines.append("  No calculada")
    lines += ["", "Ficheros (trazas tal como se midieron, sin suavizado)"]
    lines += [f"  {name}" for name in [RECORD_FILE, *record.files]]
    return "\n".join(lines) + "\n"


def save_test_package(record: TestRecord, target: str | Path) -> Path:
    """ZIP with the whole test folder, the snapshots and the summary; returns its path."""
    folder = record.folder
    if folder is None or not folder.is_dir():
        raise ValueError("El ensayo no tiene carpeta")
    target = Path(target)
    if target.suffix.lower() != ".zip":
        target = target.with_suffix(".zip")
    target.parent.mkdir(parents=True, exist_ok=True)
    root = record.id
    written: set[str] = set()
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as package:
        package.writestr(f"{root}/{SUMMARY_FILE}", summary_text(record))
        written.add(SUMMARY_FILE)
        for path in sorted(folder.rglob("*")):
            if path.is_file() and path.resolve() != target.resolve():
                relative = path.relative_to(folder).as_posix()
                package.write(path, f"{root}/{relative}")
                written.add(relative)
        if TEMPLATE_SNAPSHOT not in written and record.template:  # tests made before the snapshots
            package.writestr(f"{root}/{TEMPLATE_SNAPSHOT}", yaml.safe_dump(record.template, sort_keys=False, allow_unicode=True))
        if SETUP_SNAPSHOT not in written and record.setup:
            data = {"setup": record.setup, "equipos": record.equipment}
            package.writestr(f"{root}/{SETUP_SNAPSHOT}", yaml.safe_dump(data, sort_keys=False, allow_unicode=True))
    return target
