"""Calibration certificate of a cable (.docx) from a test and the Word template of the laboratory.

The template is a Word document with Jinja tags (docxtpl): by default
``informes/certificado_cable.docx`` of the library, made from the certificate
of the laboratory; a test template can name another one
(``informe.plantilla``). Its variables are in
``docs/13_certificado_de_cables.md``.

What goes in it comes from the test as it was made (``ensayo.yaml`` and its
files) and, for what the record does not keep (inventory references and
names of older records), from the library:

* the EUT (cover and header): reference, name, manufacturer, model, serial;
* the equipment of the hardware setup and the probe of the ambient
  conditions (``informe.sonda_ambiental``), with their next calibration;
* the procedure and the calibration interval (``informe`` of the template);
* the range, the tolerances and the nominal values of the cable (its
  specification by formula as evaluated: ``especificacion_cable``);
* the ambient conditions of the probe (minimum and maximum);
* the tables Fwd and Rev (``cable_results.py``): a row per report frequency
  (``informe.frecuencias``), with the values of the nearest measured point;
* the verdict of the test.

The charts keep the format of those of the laboratory: their code is not
integrated yet, so each one is a placeholder (``figures`` takes their PNG
files when there are).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

import numpy as np

from calelektra.catalog.library import Library
from calelektra.core.bands import BandValues, merge_limits
from calelektra.core.frequency import FrequencyRange
from calelektra.core.touchstone import TouchstoneError, read_touchstone
from calelektra.core.units import UnitError, format_frequency, parse_frequency
from calelektra.procedures.cable_results import CableResults, report_frequencies
from calelektra.procedures.cable_specs import CableSpecification
from calelektra.procedures.model import (
    DEFAULT_REPORT_TEMPLATE,
    ProcedureTemplate,
    ReportSettings,
)
from calelektra.procedures.record import TestRecord
from calelektra.procedures.results import read_uncertainty_csv

CERTIFICATE_FOLDER = "certificado"
FIGURES = {
    "vswr_fwd": "VSWR del puerto 1 (Fwd)",
    "vswr_rev": "VSWR del puerto 2 (Rev)",
    "perdida_fwd": "Pérdida de inserción Fwd",
    "perdida_rev": "Pérdida de inserción Rev",
    "retardo": "Retardo de grupo",
}
FIGURE_WIDTH_MM = 184.6  # the charts of the template, 184.6 × 46.2 mm
VERDICTS = {"PASA": "PASA", "FALLA": "FALLA", "NO_EVALUADO": "NO EVALUADO", "SIN_ESPECIFICACIONES": "SIN ESPECIFICACIONES"}
PER_LENGTH = {"1 m": "m", "100 m": "100 m", "1 ft": "ft", "100 ft": "100 ft", "1 in": "in"}
NONE = "—"


class ReportError(Exception):
    """The certificate cannot be made (the reason, in Spanish)."""


@dataclass
class Certificate:
    """The data of a certificate and what is missing in them (``warnings``)."""

    context: dict[str, Any]
    reference: str
    template: Path
    results: CableResults
    warnings: list[str] = field(default_factory=list)


# ── formats of the certificate ──────────────────────────────────────────────
def comma(value: float | None, digits: int | None = None) -> str:
    """``0,229``; ``24,7`` with ``digits=1``; a dash without value."""
    if value is None or not math.isfinite(value):
        return NONE
    text = f"{value:.{digits}f}" if digits is not None else f"{value:g}"
    return text.replace(".", ",")


def stripped(value: float, digits: int = 4) -> str:
    """``1.0748``, ``1.386``: as the VSWR columns of the tables (point, without the zeros at the end)."""
    if not math.isfinite(value):
        return NONE
    return f"{value:.{digits}f}".rstrip("0").rstrip(".")


def fixed(value: float, unit: str, digits: int = 4) -> str:
    """``-0.0630 dB``, ``11.0643 ns``: as the other columns of the tables."""
    return f"{value:.{digits}f} {unit}" if math.isfinite(value) else NONE


def day(value: date | None) -> str:
    return f"{value:%d/%m/%Y}" if value else NONE


def confidence_level(k: float) -> str:
    """Level of confidence of a normal distribution for the coverage factor ``k`` (k = 2: ``95,5%``)."""
    level = Decimal(f"{math.erf(k / math.sqrt(2)) * 100:.2f}").quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    return f"{level}%".replace(".", ",")


def frequency_span(low: float, high: float) -> str:
    """``10 MHz y 18 GHz``; ``18 y 26,5 GHz`` when both are in the same unit."""
    first, second = format_frequency(low), format_frequency(high)
    number, _, unit = first.partition(" ")
    return f"{number} y {second}" if second.endswith(f" {unit}") else f"{first} y {second}"


# ── the parts of the certificate ────────────────────────────────────────────
def _template_of(record: TestRecord, library: Library) -> ProcedureTemplate | None:
    return record.launched_template() or library.procedures.get(record.template_id or "")


def _equipment(library: Library, entry: dict[str, Any]) -> dict[str, str]:
    """A row of the table of equipment: what the record keeps and, for older records, the library."""
    equipment = library.equipment.get(str(entry.get("id")))
    model = library.model_of(equipment) if equipment is not None else None
    category = library.category_of(equipment) if equipment is not None else None
    due = entry.get("calibracion_vence")
    if due is None and equipment is not None and equipment.calibration is not None:
        due = equipment.calibration.due
    try:
        due = date.fromisoformat(str(due)) if due and not isinstance(due, date) else due
    except ValueError:
        pass
    return {
        "id": str(entry.get("id", "")),
        "inventario": str(entry.get("referencia") or (equipment.reference if equipment else None) or entry.get("id", NONE)),
        "nombre": str(entry.get("denominacion") or (equipment.name if equipment else None) or (category.name if category else entry.get("modelo", NONE))),
        "fabricante": str(entry.get("fabricante") or (model.manufacturer if model else NONE)),
        "modelo": str(entry.get("designacion") or (model.designation if model else entry.get("modelo", NONE))),
        "proxima_calibracion": day(due) if isinstance(due, date) else str(due or NONE),
    }


def _eut(library: Library, record: TestRecord) -> dict[str, str]:
    entry = record.dut or {"id": record.dut_id}
    row = _equipment(library, entry)
    equipment = library.equipment.get(row["id"])
    model = library.model_of(equipment) if equipment is not None else None
    name = entry.get("denominacion") or (equipment.name if equipment else None) or (model.display_name if model else row["nombre"])
    serial = entry.get("numero_serie") or (equipment.serial_number if equipment else None)
    return {
        "id": row["id"],
        "referencia": row["inventario"],
        "instrumento": str(name),
        "fabricante": row["fabricante"],
        "modelo": row["modelo"],
        "serie": str(serial or NONE),
    }


def _ambient(record: TestRecord) -> dict[str, dict[str, str]]:
    conditions = record.conditions or {}

    def span(key: str, digits: int, factor: float = 1.0) -> dict[str, str]:
        values = conditions.get(key) or {}
        return {
            bound: comma(float(values[bound]) * factor, digits) if isinstance(values.get(bound), (int, float)) else NONE
            for bound in ("min", "max")
        }

    return {"temperatura": span("temperatura_c", 1), "humedad": span("humedad_pct", 1), "presion": span("presion_hpa", 0, 100.0)}  # Pa


def _measurement(record: TestRecord) -> tuple[np.ndarray, np.ndarray, dict[str, tuple[np.ndarray, np.ndarray]]]:
    """Frequencies, S-parameters (points, 2, 2) and uncertainty per trace of the cable."""
    folder = record.folder
    if folder is None:
        raise ReportError("El ensayo no tiene carpeta")
    names = [str(m.get("fichero")) for m in record.measurements if str(m.get("fichero", "")).lower().endswith(".s2p")]
    names += [name for name in record.files if "/" not in name and name.lower().endswith(".s2p")]
    if not names:
        raise ReportError("El ensayo no tiene el fichero .s2p de la medida")
    try:
        data = read_touchstone(folder / names[0])
    except (TouchstoneError, OSError) as exc:
        raise ReportError(f"No se puede leer «{names[0]}»: {exc}") from exc
    if data.s.shape[1] != 2:
        raise ReportError(f"«{names[0]}» no es una medida de 2 puertos")
    uncertainty: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    for entry in record.uncertainty.get("medidas", []):
        if entry.get("fichero_resultado") == names[0] and entry.get("fichero"):
            uncertainty = read_uncertainty_csv(folder / str(entry["fichero"]))
    return data.frequencies, data.s, uncertainty


def _range(record: TestRecord, frequencies: np.ndarray) -> FrequencyRange:
    try:
        start, stop = (parse_frequency(value) for value in record.frequency_range)
        return FrequencyRange(start, stop)
    except (UnitError, ValueError, TypeError):
        return FrequencyRange(float(frequencies[0]), float(frequencies[-1]))


def _bands_inside(bands: BandValues, span: FrequencyRange) -> list[tuple[float, float, float]]:
    """(from, to, value) of each band inside ``span``."""
    found, lower = [], span.start
    for upper, value in bands.bands:
        top = span.stop if upper is None else min(upper, span.stop)
        if top > lower:
            found.append((lower, top, value))
        lower = max(lower, upper if upper is not None else span.stop)
        if lower >= span.stop:
            break
    return found


def vswr_note(spec: CableSpecification, span: FrequencyRange) -> str:
    """Footnote of the VSWR: what each connector declares and the maximum of the cable, by band."""
    connectors = spec.connector_vswr
    maximum = spec.max_vswr_bands()
    if connectors is None or maximum is None:
        return "El modelo del cable no declara el VSWR de sus conectores."
    a, b = connectors
    sentences = []
    if a == b:
        for low, high, value in _bands_inside(a, span):
            sentences.append(f"Entre {frequency_span(low, high)}, el VSWR máximo declarado de cada conector es {comma(value)}:1.")
    else:
        edges = BandValues(tuple((upper, 0.0) for upper in merge_limits(a, b)) + ((None, 0.0),))  # where either changes
        for low, high, _value in _bands_inside(edges, span):
            middle = (low + high) / 2
            first, second = (float(item.at([middle])[0]) for item in (a, b))
            sentences.append(
                f"Entre {frequency_span(low, high)}, el VSWR máximo declarado del conector A es {comma(first)}:1 y el del conector B, {comma(second)}:1."
            )
    limits = [f"{comma(round(value, 2))}:1 entre {frequency_span(low, high)}" for low, high, value in _bands_inside(maximum, span)]
    if limits:
        joined = limits[0] if len(limits) == 1 else ", ".join(limits[:-1]) + f", ni {limits[-1]}"
        sentences.append(f"El VSWR medido no deberá superar {joined}.")
    return " ".join(sentences)


def _cable(spec: CableSpecification | None) -> dict[str, str]:
    if spec is None:
        return {key: NONE for key in ("a", "b", "unidad", "por", "unidad_f", "longitud")}
    per = PER_LENGTH.get(str(spec.length_unit), str(spec.length_unit or "m"))
    return {
        "a": comma(spec.k1) if spec.k1 is not None else "0",
        "b": comma(spec.k2) if spec.k2 is not None else "0",
        "unidad": f"dB/{per}",
        "por": per,
        "unidad_f": str(spec.frequency_unit or "GHz"),
        "longitud": f"{comma(spec.length_m)} m" if spec.length_m is not None else NONE,
    }


def _rows(results: CableResults, nominal: list[float]) -> dict[str, list[dict[str, str]]]:
    rows: dict[str, list[dict[str, str]]] = {}
    for name, values in results.report_rows(nominal).items():
        rows[name.lower()] = [
            {
                "frecuencia": format_frequency(row["frecuencia"], decimal="."),
                "frecuencia_medida": format_frequency(row["frecuencia_medida"], decimal="."),
                "vswr": stripped(row["vswr"]),
                "u_vswr": stripped(row["u_vswr"]),
                "limite_vswr": stripped(row["limite_vswr"]),
                "perdida": fixed(row["perdida_insercion"], "dB"),
                "u_perdida": fixed(row["u_perdida_insercion"], "dB"),
                "limite_perdida": fixed(row["limite_perdida_insercion"], "dB"),
                "retardo": fixed(row["retardo_grupo"], "ns"),
                "u_retardo": fixed(row["u_retardo_grupo"], "ns"),
                "pasa": "PASA" if row["pasa"] else "FALLA",
            }
            for row in values
        ]
    return rows


def certificate_problem(record: TestRecord) -> str | None:
    """Why a test cannot give a certificate of a cable (None if it can)."""
    if record.algorithm != "vna_2_puertos":
        return "El certificado de cables es para los ensayos de 2 puertos (parámetros S → .s2p)"
    if record.folder is None:
        return "El ensayo no tiene carpeta"
    if record.status not in ("completado", "verificacion_fallida"):
        return f"El ensayo no está completado ({record.status_name})"
    return None


def default_reference(record: TestRecord, eut_reference: str | None = None) -> str:
    """``<referencia del EUT>_<año>_<mes>`` of the calibration, as the certificates of the laboratory."""
    base = eut_reference if eut_reference and eut_reference != NONE else (record.dut_id or record.id)
    return f"{base}_{record.started:%Y_%m}"


def prepare_certificate(
    library: Library,
    record: TestRecord,
    reference: str | None = None,
    report_date: date | None = None,
    figures: dict[str, Any] | None = None,
) -> Certificate:
    """The data of the certificate of the cable of ``record``, with what is missing in ``warnings``."""
    problem = certificate_problem(record)
    if problem:
        raise ReportError(problem)
    template = _template_of(record, library)
    settings = template.report if template is not None else ReportSettings()
    frequencies, s, uncertainty = _measurement(record)
    spec = CableSpecification.from_record(record.cable_specification)
    k = float(record.uncertainty.get("k", template.uncertainty.coverage_factor if template else 2.0))
    results = CableResults.compute(frequencies, s, uncertainty, spec, settings.aperture, k)
    span = _range(record, frequencies)
    criteria = span
    if spec is not None and spec.nominal_range is not None:
        low, high = max(span.start, spec.nominal_range.start), min(span.stop, spec.nominal_range.stop)
        criteria = FrequencyRange(low, high) if high > low else span
    nominal = report_frequencies(settings.frequencies, span.start, span.stop)
    eut = _eut(library, record)
    warnings = []
    if eut["referencia"] == eut["id"]:
        warnings.append(f"El EUT «{eut['id']}» no tiene referencia de inventario (LENNNN.NNN)")
    equipment = [_equipment(library, entry) for entry in record.equipment if entry.get("id") != eut["id"]]
    if settings.ambient_sensor:
        if settings.ambient_sensor in library.equipment:
            equipment.append(_equipment(library, {"id": settings.ambient_sensor}))
        else:
            warnings.append(f"La sonda ambiental «{settings.ambient_sensor}» de la plantilla no está en el inventario")
    unreferenced = [row["id"] for row in equipment if row["inventario"] == row["id"]]
    if unreferenced:
        warnings.append("Equipos sin referencia de inventario (se pone su identificador): " + ", ".join(unreferenced))
    if record.conditions.get("estado") != "cargadas":
        warnings.append("Faltan las condiciones ambientales del ensayo (fichero de la sonda TPH)")
    if record.uncertainty.get("estado") == "pendiente":
        warnings.append("La incertidumbre está pendiente: calculada sin la componente de temperatura")
    if not uncertainty:
        warnings.append("El ensayo no tiene incertidumbre calculada")
    if spec is None:
        warnings.append("El modelo del cable no tiene especificación por fórmula: sin límites")
    if not settings.procedure:
        warnings.append("La plantilla de ensayo no indica el procedimiento de calibración (informe.procedimiento)")
    if not settings.interval:
        warnings.append("La plantilla de ensayo no indica el intervalo de calibración (informe.intervalo)")
    figures = dict(figures or {})
    missing = [title for key, title in FIGURES.items() if key not in figures]
    if missing:
        warnings.append("Gráficas pendientes (se integrarán con el código de gráficas del laboratorio): " + ", ".join(missing))
    finished = record.finished.date() if record.finished else record.started.date()
    started = record.started.date()
    reference = (reference or "").strip() or default_reference(record, eut["referencia"])
    context: dict[str, Any] = {
        "referencia_certificado": reference,
        "fecha_informe": day(report_date or date.today()),
        "fecha_calibracion": day(started) if finished == started else f"{day(started)} – {day(finished)}",
        "eut": eut,
        "intervalo": settings.interval or NONE,
        "procedimiento": settings.procedure or NONE,
        "k": f"{k:g}",
        "nivel_confianza": confidence_level(k),
        "equipos": equipment,
        "rango": {"inicio": format_frequency(span.start), "fin": format_frequency(span.stop)},
        "rango_criterios": {"inicio": format_frequency(criteria.start), "fin": format_frequency(criteria.stop)},
        "tolerancia_perdida": f"{comma(spec.insertion_loss_tolerance_db if spec else 0.0)} dB",
        "tolerancia_vswr": f"{comma(spec.vswr_tolerance_pct if spec else 0.0)}%",
        "cable": _cable(spec),
        "nota_vswr": vswr_note(spec, criteria) if spec is not None else "El modelo del cable no declara el VSWR de sus conectores.",
        "ambiente": _ambient(record),
        "figuras": {key: figures.get(key, f"[Gráfica pendiente: {title}]") for key, title in FIGURES.items()},
        "resultado_final": VERDICTS.get(str(record.verdict), str(record.verdict or NONE)),
        **_rows(results, nominal),
    }
    path = library.root / settings.template_path
    if not path.is_file():
        path = library.root / DEFAULT_REPORT_TEMPLATE
    return Certificate(context, reference, path, results, warnings)


def default_certificate_path(record: TestRecord, reference: str) -> Path:
    assert record.folder is not None
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in reference)
    return record.folder / CERTIFICATE_FOLDER / f"{safe}.docx"


def generate_certificate(
    library: Library,
    record: TestRecord,
    output: Path | None = None,
    reference: str | None = None,
    report_date: date | None = None,
    figures: dict[str, Path] | None = None,
) -> tuple[Path, Certificate]:
    """Write the certificate (.docx) of the cable of ``record``; kept in the record when it is in its folder.

    ``figures``: PNG of the charts (keys of ``FIGURES``), placed with the size of those of the template.
    """
    try:
        from docx.shared import Mm
        from docxtpl import DocxTemplate, InlineImage, Listing
    except ImportError as exc:  # pragma: no cover - dependency of the package
        raise ReportError("Falta docxtpl para generar el certificado (pip install docxtpl)") from exc
    certificate = prepare_certificate(library, record, reference, report_date, figures)
    if not certificate.template.is_file():
        raise ReportError(f"No se encuentra la plantilla del certificado «{certificate.template}»")
    document = DocxTemplate(str(certificate.template))
    pictures = certificate.context["figuras"]
    for key in FIGURES:
        if key in (figures or {}):
            pictures[key] = InlineImage(document, str(figures[key]), width=Mm(FIGURE_WIDTH_MM))
        else:
            pictures[key] = Listing(f"\n{pictures[key]}\n")  # on its own line, as the chart it stands for
    target = Path(output) if output is not None else default_certificate_path(record, certificate.reference)
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        document.render(certificate.context, autoescape=True)
        document.save(str(target))
    except Exception as exc:  # docxtpl and jinja2 raise many kinds of errors on a wrong template
        raise ReportError(f"No se pudo generar el certificado con «{certificate.template.name}»: {exc}") from exc
    assert record.folder is not None
    try:
        name = target.resolve().relative_to(record.folder.resolve()).as_posix()
    except ValueError:
        name = None
    record.results["certificado"] = {
        "referencia": certificate.reference,
        "fecha": certificate.context["fecha_informe"],
        "fichero": name or str(target),
        "plantilla": certificate.template.name,
    }
    if name and name not in record.files:
        record.files.append(name)
    library.save_test(record)
    return target, certificate
