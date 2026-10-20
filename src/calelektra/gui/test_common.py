"""Helpers shared by the test template editor, the test window and the test explorer."""

from __future__ import annotations

import html

import numpy as np
from PySide6.QtWidgets import QLabel

from calelektra.catalog.library import Library
from calelektra.catalog.model import Equipment, SpecFile
from calelektra.core.touchstone import TouchstoneError, read_touchstone
from calelektra.core.units import format_frequency
from calelektra.gui import style
from calelektra.instruments.factory import is_simulated
from calelektra.procedures.engine import RunPlan
from calelektra.procedures.model import ALGORITHMS, TRACES, SweepSettings
from calelektra.procedures.record import TestRecord
from calelektra.procedures.specs import VERDICTS, SpecCurve, spec_curves


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
    if plan.frequency_plan is not None:
        lines.append(f"<b>Frecuencias</b> (del setup): {html.escape(plan.frequency_plan.describe())}")
    lines.append(f"<b>Medida:</b> {sweep.power_dbm:g} dBm · {html.escape(trace_text(sweep, procedure.traces))}")
    if plan.vna is not None:
        simulated = " <span style='color:#6c3483'><b>(SIMULADO)</b></span>" if is_simulated(plan.vna, library) else ""
        lines.append(f"<b>VNA:</b> {plan.vna.id}{simulated}")
    lines.append(f"<b>EUT:</b> {html.escape(equipment_text(library, plan.dut)) if plan.dut else '—'}")
    design = plan.design
    if procedure.calibration.method == "existente":
        lines.append("<b>Calibración:</b> se usa la calibración activa del VNA")
    elif design is not None and design.method:
        planes = ", ".join(f"{p.name} → P{p.vna_port} ({p.connector.label() if p.connector else '?'})" for p in design.planes)
        kit = f" · kit {design.kit.id}" if design.kit else " · kit del VNA"
        lines.append(f"<b>Calibración:</b> {design.method}{kit}<br>&nbsp;&nbsp;{html.escape(design.reason)}<br>&nbsp;&nbsp;Planos: {html.escape(planes)}")
    if plan.specs:
        specs = "; ".join(f"{spec.name} ({spec.describe()})" for spec, _ in plan.specs)
        lines.append(f"<b>Especificaciones</b> (del modelo del EUT): {html.escape(specs)}")
    elif plan.dut is not None:
        lines.append("<b>Especificaciones:</b> el modelo del EUT no tiene (sin veredicto pasa / falla)")
    if plan.lines != ["—"]:
        lines.append(f"<b>Líneas:</b> {', '.join(plan.lines)}")
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


def record_datasets(record: TestRecord) -> list[tuple[str, np.ndarray, np.ndarray]]:
    """Measured data of a test, read back from its result Touchstone files.

    Only the files at the top of the test folder are results: the
    specification files kept in ``especificaciones/`` are not measurements.
    """
    datasets = []
    folder = record.folder
    if folder is None:
        return datasets
    for name in record.files:
        if "/" in name or not name.lower().endswith((".s1p", ".s2p")):
            continue
        try:
            data = read_touchstone(folder / name)
        except (TouchstoneError, OSError):
            continue
        label = name.rsplit(".", 1)[0].rsplit("_", 1)[-1] if name.lower().endswith(".s1p") else "DUT"
        datasets.append((label, data.frequencies, data.s))
    return datasets


def record_spec_curves(record: TestRecord) -> list[SpecCurve]:
    """Curves of the specification files used by a test (its own copies)."""
    curves: list[SpecCurve] = []
    if record.folder is None:
        return curves
    for item in record.specifications:
        try:
            spec = SpecFile.model_validate(item)
        except ValueError:
            continue
        curves += spec_curves(spec, record.folder / spec.file)
    return curves


def record_smoothing(record: TestRecord) -> dict[str, float]:
    """Display smoothing of the traces, from the template as launched."""
    template = record.launched_template()
    if template is None:
        return {}
    return template.sweep.smoothing(TRACES[template.algorithm])
