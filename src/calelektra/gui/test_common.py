"""Helpers shared by the test template editor, the test window and the test explorer."""

from __future__ import annotations

import html
from pathlib import Path

import numpy as np
from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import QFileDialog, QLabel, QMessageBox, QWidget

from calelektra.catalog.library import Library
from calelektra.catalog.model import Equipment, SpecFile
from calelektra.core.touchstone import TouchstoneError, read_touchstone
from calelektra.core.units import format_frequency
from calelektra.gui import style
from calelektra.gui.plots import CouplerPlots, SParameterPlot
from calelektra.instruments.factory import is_simulated
from calelektra.procedures.ambient import AmbientError
from calelektra.procedures.cable_results import DEFAULT_APERTURE
from calelektra.procedures.cable_specs import CableSpecification
from calelektra.procedures.coupler import nominal_values_from_record
from calelektra.procedures.engine import RunPlan
from calelektra.procedures.model import ALGORITHMS, TRACES, SweepSettings
from calelektra.procedures.package import save_test_package
from calelektra.procedures.record import TestRecord
from calelektra.procedures.record_uncertainty import (
    conditions_loaded,
    is_pending,
    load_ambient_conditions,
)
from calelektra.procedures.results import read_results_csv_s21, read_uncertainty_csv
from calelektra.procedures.specs import VERDICTS, SpecCurve, spec_curves
from calelektra.setups.paths import describe_path_record
from calelektra.uncertainty.engine import UncertaintyCalculationError
from calelektra.uncertainty.impedance import impedance_text

SETTINGS = ("CAL-ELEKTRA", "CAL-ELEKTRA")
PENDING_TOOLTIP = (
    "La incertidumbre se ha calculado sin la componente de temperatura y queda pendiente de las condiciones "
    "ambientales del ensayo: cárguelas con «Condiciones ambientales…» (fichero .xlsx de la sonda TPH)."
)


def equipment_text(library: Library, equipment: Equipment) -> str:
    model = library.model_of(equipment)
    text = f"{equipment.id} — {model.designation}" if model else equipment.id
    if equipment.serial_number:
        text += f" · nº serie {equipment.serial_number}"
    return text


def trace_text(sweep: SweepSettings, traces: tuple[str, ...]) -> str:
    """IFBW, averaging and display smoothing of all traces, grouped when they are set per trace."""

    def one(ifbw: float, averages: int, smoothing: float) -> str:
        text = f"IFBW {format_frequency(ifbw)}, {averages} promedio{'s' if averages != 1 else ''}"
        return text + (f", visualización suavizada {smoothing:g} %" if smoothing else "")

    if not sweep.per_trace:
        return one(sweep.if_bandwidth, sweep.averages, sweep.smoothing_pct)
    groups: dict[tuple, list[str]] = {}
    for trace in traces:
        groups.setdefault(sweep.trace(trace), []).append(trace)
    if len(groups) == 1:
        return one(*next(iter(groups)))
    return "; ".join(f"{', '.join(names)}: {one(*settings)}" for settings, names in groups.items())


def plan_html(library: Library, plan: RunPlan) -> str:
    """Summary of what a launch of the template will do."""
    procedure = plan.procedure
    sweep = procedure.sweep
    muted = "<span style='color:#5b6b80'>{}</span>"
    lines = [f"<b>Plantilla:</b> {html.escape(procedure.name)} " + muted.format(html.escape(ALGORITHMS[procedure.algorithm].split(" (")[0]))]
    if plan.setup is not None:
        lines.append(f"<b>Hardware setup:</b> {html.escape(library.setups[plan.setup.id].name if plan.setup.id in library.setups else plan.setup.id)}")
    if plan.expanded is not None:
        for path in plan.expanded.paths:
            lines.append(f"&nbsp;&nbsp;<b>{html.escape(path.label)}:</b> {html.escape(path.describe())}")
        lines.append(f"&nbsp;&nbsp;<b>Conectores de DUT admitidos:</b> {html.escape(plan.expanded.dut_connectors_text())}")
    if plan.frequency_plan is not None:
        lines.append(f"<b>Frecuencias</b> (del setup): {html.escape(plan.frequency_plan.describe())}")
    lines.append(f"<b>Medida:</b> {sweep.power_dbm:g} dBm · {html.escape(trace_text(sweep, procedure.traces))}")
    if plan.vna is not None:
        simulated = " <span style='color:#6c3483'><b>(SIMULADO)</b></span>" if is_simulated(plan.vna, library) else ""
        lines.append(f"<b>VNA:</b> {plan.vna.id}{simulated}")
    lines.append(f"<b>EUT:</b> {html.escape(equipment_text(library, plan.dut)) if plan.dut else '—'}")
    if plan.dut is not None and plan.measurements and plan.expanded is not None:
        if procedure.algorithm == "vna_acoplador":
            pairs = ", ".join(f"{m.title} ({m.dut_ports[0]} en PR1, {m.dut_ports[1]} en PR2)" for m in plan.measurements)
            lines.append(f"<b>Pares de puertos</b> (los otros dos en las cargas de 50 Ω): {html.escape(pairs)}")
        else:
            ports = ", ".join(f"{port} en {path.plane}" for path, port in zip(plan.expanded.paths, plan.measurements[0].dut_ports))
            lines.append(f"<b>Puertos del EUT:</b> {html.escape(ports)}")
    design = plan.design
    if procedure.calibration.method == "existente":
        lines.append("<b>Calibración:</b> se usa la calibración activa del VNA")
    elif design is not None and design.method == "normalizacion":
        through = f"through «{design.through.id}»" if design.through else "through plano"
        lines.append(f"<b>Calibración:</b> normalización de transmisión con {through}<br>&nbsp;&nbsp;{html.escape(design.reason)}")
    elif design is not None and design.method:
        planes = ", ".join(f"{p.name} → P{p.vna_port} ({p.connector.label() if p.connector else '?'})" for p in design.planes)
        kit = f" · kit {design.kit.id}" if design.kit else " · kit del VNA"
        lines.append(f"<b>Calibración:</b> {design.method}{kit}<br>&nbsp;&nbsp;{html.escape(design.reason)}<br>&nbsp;&nbsp;Planos: {html.escape(planes)}")
    if plan.coupler_limits:
        limits = "; ".join(f"{key.replace('_', ' ')} {limit.describe()}" for key, limit in plan.coupler_limits.items())
        lines.append(f"<b>Valores nominales del acoplador</b> (pasa / falla): {html.escape(limits)}")
    elif plan.specs or plan.cable_spec is not None:
        if plan.specs:
            specs = "; ".join(f"{spec.name} ({spec.describe()})" for spec, _ in plan.specs)
            lines.append(f"<b>Especificaciones</b> (del modelo del EUT): {html.escape(specs)}")
        cable = plan.cable_spec
        if cable is not None:
            parts = []
            if cable.has_attenuation:
                parts.append(cable.insertion_loss_criterion())
            if cable.has_return_loss and "S11" in procedure.traces:
                parts.append(cable.return_loss_criterion())
            lines.append(f"<b>Especificación por fórmula</b> (modelo del EUT y tolerancias de la plantilla): {html.escape('; '.join(parts))}")
    elif plan.dut is not None and procedure.algorithm == "vna_acoplador":
        lines.append("<b>Valores nominales:</b> el modelo del acoplador no tiene (sin veredicto pasa / falla)")
    elif plan.dut is not None:
        lines.append("<b>Especificaciones:</b> el modelo del EUT no tiene (sin veredicto pasa / falla)")
    if plan.cable_delay is not None:
        lines.append(f"<b>Retardo nominal</b> (informativo, se compara con el medido): {html.escape(plan.cable_delay.text())}")
    if procedure.uncertainty.active and plan.vna is not None:
        if plan.vna.uncertainty_file:
            k = f"{procedure.uncertainty.coverage_factor:g}"
            name = html.escape(plan.vna.uncertainty_file.replace("\\", "/").rsplit("/", 1)[-1])
            lines.append(f"<b>Incertidumbre</b> (k={k}): fichero de incertidumbres de {plan.vna.id} ({name})")
            if procedure.uncertainty.temperature_stability:
                lines.append(
                    "<b>Condiciones ambientales:</b> de la sonda TPH (.xlsx), se cargan al terminar el ensayo; "
                    "hasta entonces la incertidumbre queda pendiente (sin la componente de temperatura)"
                )
        else:
            lines.append(f"<b>Incertidumbre:</b> {plan.vna.id} no tiene fichero de incertidumbres")
    return "<br>".join(lines)


def setup_state(template) -> str | None:
    states = template.connection_states if template else {}
    return "medida" if "medida" in states else next(iter(states), None)


def verdict_chip(verdict: str | None) -> QLabel | None:
    if not verdict or verdict == "SIN_ESPECIFICACIONES":
        return None
    label = style.chip(VERDICTS.get(verdict, verdict).upper(), style.VERDICT_COLORS.get(verdict, style.NEUTRAL))
    label.setToolTip("Veredicto frente a las especificaciones del modelo del EUT")
    return label


# ── records ────────────────────────────────────────────────────────────────
def is_logarithmic(record: TestRecord) -> bool:
    return record.sweep.get("modo", record.sweep.get("tipo")) == "logaritmico"


def dataset_label(file_name: str) -> str:
    """Name of the measurement of a result file: «DUT» for an .s2p, the line (last part of the name) for an .s1p."""
    return file_name.rsplit(".", 1)[0].rsplit("_", 1)[-1] if file_name.lower().endswith(".s1p") else "DUT"


def record_datasets(record: TestRecord) -> list[tuple[str, np.ndarray, np.ndarray]]:
    """Measured data of a test, read back from its result files (``medidas`` of the record).

    Tests made before 0.8 have no ``medidas``: their results are the Touchstone
    files at the top of the test folder (the specification files kept in
    ``especificaciones/`` are not measurements).
    """
    datasets = []
    folder = record.folder
    if folder is None:
        return datasets
    if record.measurements:
        for measurement in record.measurements:
            path = folder / str(measurement.get("fichero", ""))
            try:
                if measurement.get("formato") == "csv_s21":
                    frequencies, s = read_results_csv_s21(path)
                else:
                    data = read_touchstone(path)
                    frequencies, s = data.frequencies, data.s
            except (TouchstoneError, OSError, ValueError):
                continue
            datasets.append((str(measurement.get("nombre", "DUT")), frequencies, s))
        return datasets
    for name in record.files:
        if "/" in name or not name.lower().endswith((".s1p", ".s2p")):
            continue
        try:
            data = read_touchstone(folder / name)
        except (TouchstoneError, OSError):
            continue
        datasets.append((dataset_label(name), data.frequencies, data.s))
    return datasets


def record_uncertainty(record: TestRecord) -> dict[str, dict[str, tuple[np.ndarray, np.ndarray]]]:
    """Expanded uncertainty of the measurements of a test (labels as in ``record_datasets``)."""
    bands: dict[str, dict[str, tuple[np.ndarray, np.ndarray]]] = {}
    if record.folder is None:
        return bands
    for entry in record.uncertainty.get("medidas", []):
        result_file, file_name = entry.get("fichero_resultado"), entry.get("fichero")
        if not result_file or not file_name:
            continue
        label = str(entry.get("medida")) if record.measurements and entry.get("medida") else dataset_label(result_file)
        try:
            bands[label] = read_uncertainty_csv(record.folder / file_name)
        except (OSError, ValueError):
            continue
    return bands


def setup_rows(record: TestRecord) -> list[tuple[str, str]]:
    """Rows of the results table about the paths of the hardware setup and the measurements of the EUT."""
    rows: list[tuple[str, str]] = []
    for path in record.paths:
        rows.append((str(path.get("etiqueta", "Camino")), describe_path_record(path)))
    if record.setup.get("conectores_dut_admitidos"):
        rows.append(("Conectores de DUT admitidos", str(record.setup["conectores_dut_admitidos"])))
    for measurement in record.measurements:
        ports = ", ".join(f"{port} en {path}" for path, port in measurement.get("puertos_eut", {}).items())
        loads = f"; en carga: {', '.join(measurement['puertos_en_carga'])}" if measurement.get("puertos_en_carga") else ""
        name = measurement.get("nombre", "DUT")
        rows.append(("Medida" if name == "DUT" else f"Medida {name}", f"{measurement.get('fichero')} ({ports}{loads})"))
    return rows


def conditions_rows(record: TestRecord) -> list[tuple[str, str]]:
    """Rows of the results table about the ambient conditions of the test."""
    conditions = record.conditions
    rows: list[tuple[str, str]] = []
    if conditions_loaded(conditions):
        for key, label, unit in (("temperatura_c", "Temperatura ambiente", "°C"), ("humedad_pct", "Humedad relativa", "%"), ("presion_hpa", "Presión", "hPa")):
            values = conditions.get(key)
            if values:
                rows.append((label, f"{values['min']:g} – {values['max']:g} {unit} (media {values['media']:g})"))
        rows.append(
            (
                "Condiciones ambientales",
                f"{conditions.get('lecturas')} lecturas de la sonda TPH ({conditions.get('lecturas_desde')} – {conditions.get('lecturas_hasta')})"
                + (f" · {conditions['fichero']}" if conditions.get("fichero") else ""),
            )
        )
    elif conditions.get("temperatura_min_c") is not None and conditions.get("temperatura_max_c") is not None:  # tests of version 0.6
        rows.append(("Temperatura ambiente", f"{conditions['temperatura_min_c']:g} – {conditions['temperatura_max_c']:g} °C"))
    elif conditions.get("estado") == "pendiente" and record.status != "en_curso":
        rows.append(("Condiciones ambientales", "pendientes: cargue el fichero .xlsx de la sonda TPH"))
    return rows


def uncertainty_rows(record: TestRecord) -> list[tuple[str, str]]:
    """Rows of the results table about the ambient conditions and the uncertainty of the traces."""
    data = record.uncertainty
    rows = conditions_rows(record)
    if not data:
        return rows
    k = data.get("k", 2)
    rows.append(("Incertidumbre", f"k = {k:g} · {data.get('documento', '—')} · {data.get('vna', '')}"))
    if is_pending(record):
        rows.append(("Incertidumbre · estado", "PENDIENTE de las condiciones ambientales: provisional, sin la componente de temperatura"))
    elif isinstance(data.get("temperatura_c"), list):
        low, high = data["temperatura_c"]
        rows.append(("Incertidumbre · estado", f"completa, con la temperatura del ensayo ({low:g} – {high:g} °C)"))
    for warning in data.get("avisos", []):
        rows.append(("Incertidumbre · aviso", str(warning)))
    several = len(data.get("medidas", [])) > 1
    for entry in data.get("medidas", []):
        prefix = f"Incertidumbre {entry.get('medida')}" if several else "Incertidumbre"
        for parameter, trace in entry.get("trazas", {}).items():
            db, deg = trace.get("U_db"), trace.get("U_grados")
            text = f"{db['min']:g} – {db['max']:g} dB" if db else "—"
            if deg:
                text += f" · {deg['min']:g} – {deg['max']:g}°"
            if trace.get("puntos_sin_incertidumbre"):
                text += f" · {trace['puntos_sin_incertidumbre']} de {trace.get('puntos')} puntos sin incertidumbre"
            rows.append((f"{prefix} · {parameter} (U, k={k:g})", text))
            for problem in trace.get("problemas", []):
                rows.append((f"{prefix} · {parameter} · fuera de rango", str(problem)))
        if entry.get("impedancia"):
            rows.append((f"{prefix} · Z (U, k={k:g})", impedance_text(entry["impedancia"])))
    return rows


def pending_chip(record: TestRecord | None) -> QLabel | None:
    """«INCERTIDUMBRE PENDIENTE» (or «CONDICIONES PENDIENTES») while the ambient conditions are not loaded."""
    if record is None or record.status == "en_curso":
        return None
    if is_pending(record):
        label = style.chip("INCERTIDUMBRE PENDIENTE", style.WARNING)
        label.setToolTip(PENDING_TOOLTIP)
        return label
    if record.conditions.get("estado") == "pendiente":
        label = style.chip("CONDICIONES PENDIENTES", style.NEUTRAL)
        label.setToolTip("Cargue las condiciones ambientales del ensayo con «Condiciones ambientales…» (fichero .xlsx de la sonda TPH).")
        return label
    return None


def notify(parent: QWidget, title: str, text: str, warning: bool = False) -> QMessageBox:
    """Message to the operator that does not block (the box is closed by the operator)."""
    box = QMessageBox(QMessageBox.Icon.Warning if warning else QMessageBox.Icon.Information, title, text, QMessageBox.StandardButton.Ok, parent)
    box.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
    box.open()
    return box


def load_conditions(parent: QWidget, library: Library, record: TestRecord, path: str | Path | None = None) -> bool:
    """Load the ambient conditions of a test from a workbook of the TPH probe (asks for it if ``path`` is None).

    The uncertainty of the test is calculated again with them.
    """
    settings = QSettings(*SETTINGS)
    if path is None:
        start = str(settings.value("carpeta_condiciones", "") or (record.folder or ""))
        path, _ = QFileDialog.getOpenFileName(parent, "Condiciones ambientales del ensayo", start, "Datos de la sonda TPH (*.xlsx)")
        if not path:
            return False
    settings.setValue("carpeta_condiciones", str(Path(path).parent))
    try:
        message = load_ambient_conditions(library, record, path)
    except (AmbientError, UncertaintyCalculationError, OSError, ValueError) as exc:
        notify(parent, "Condiciones ambientales", str(exc), warning=True)
        return False
    notify(parent, "Condiciones ambientales", message)
    return True


def save_package(parent: QWidget, record: TestRecord, path: str | Path | None = None) -> Path | None:
    """Save all the information of a test in a ZIP file (asks where if ``path`` is None)."""
    if record.folder is None:
        return None
    settings = QSettings(*SETTINGS)
    if path is None:
        start = Path(str(settings.value("carpeta_paquetes", "") or record.folder.parent)) / f"{record.id}.zip"
        path, _ = QFileDialog.getSaveFileName(parent, "Guardar ensayo", str(start), "Paquete del ensayo (*.zip)")
        if not path:
            return None
    try:
        saved = save_test_package(record, path)
    except (OSError, ValueError) as exc:
        notify(parent, "Guardar ensayo", str(exc), warning=True)
        return None
    settings.setValue("carpeta_paquetes", str(saved.parent))
    text = (
        f"Ensayo guardado en {saved}:\nhardware setup, plantilla de ensayo, horas de inicio y fin, condiciones ambientales, "
        "registro, resultados (Touchstone sin suavizado, CSV) e incertidumbre."
    )
    if is_pending(record) or record.conditions.get("estado") == "pendiente":
        text += "\n\nAtención: las condiciones ambientales no están cargadas y la incertidumbre es provisional (sin la componente de temperatura)."
    notify(parent, "Guardar ensayo", text, warning=is_pending(record))
    return saved


def record_coverage_factor(record: TestRecord) -> float:
    try:
        return float(record.uncertainty.get("k", 2.0))
    except (TypeError, ValueError):
        return 2.0


def record_spec_curves(record: TestRecord) -> list[SpecCurve]:
    """Curves of the specification files used by a test (its own copies)."""
    curves: list[SpecCurve] = []
    cable = CableSpecification.from_record(record.cable_specification)
    if cable is not None:
        curves += cable.curves(TRACES.get(record.algorithm or "", TRACES["vna_2_puertos"]))
    if record.folder is None:
        return curves
    for item in record.specifications:
        try:
            spec = SpecFile.model_validate(item)
        except ValueError:
            continue
        curves += spec_curves(spec, record.folder / spec.file)
    return curves


def show_specifications(plot: SParameterPlot | CouplerPlots, record: TestRecord) -> None:
    """Specifications of a test on its chart: the S2P specifications of the model of the EUT or the nominal values of a coupler."""
    if isinstance(plot, CouplerPlots):
        plot.set_nominal_values(*nominal_values_from_record(record.nominal_values))
    else:
        plot.set_limits(record_spec_curves(record))


def show_plan_specifications(plot: SParameterPlot | CouplerPlots, plan: RunPlan) -> None:
    """The same before the test is made, from what the run plan resolved."""
    if isinstance(plot, CouplerPlots):
        plot.set_nominal_values(plan.coupler_limits, plan.coupler_range)
    else:
        curves = [curve for spec, path in plan.specs for curve in spec_curves(spec, path)]
        if plan.cable_spec is not None:
            curves += plan.cable_spec.curves(plan.procedure.traces)
        plot.set_limits(curves)


def record_smoothing(record: TestRecord) -> dict[str, float]:
    """Display smoothing of the traces, from the template as launched."""
    template = record.launched_template()
    if template is None:
        return {}
    return template.sweep.smoothing(TRACES[template.algorithm])


def record_group_delay_aperture(record: TestRecord) -> int:
    """Frequency steps of the group delay of a test: those of its results, else those of the template as launched."""
    stored = (record.results.get("sentidos") or {}).get("apertura_retardo_pasos")
    if isinstance(stored, int) and stored >= 1:
        return stored
    template = record.launched_template()
    return template.report.aperture if template is not None else DEFAULT_APERTURE


def show_display_settings(plot: SParameterPlot | CouplerPlots, record: TestRecord) -> None:
    """Smoothing of the traces and aperture of the group delay of a test on its chart."""
    plot.set_smoothing(record_smoothing(record))
    if isinstance(plot, SParameterPlot):
        plot.set_group_delay_aperture(record_group_delay_aperture(record))
