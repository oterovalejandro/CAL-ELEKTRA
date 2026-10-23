"""Execution of a test: a test template launched in a folder.

A test template calls a hardware setup and defines the EUT to calibrate.
Launching it creates the test record (``Library.new_test_record``) and runs
these steps, using the fields of the assigned equipment:

    validación → conexión → configuración → calibración → verificación →
    medida → evaluación pasa/falla → exportación → cierre

* the frequency points are those of the hardware setup (linear by points or
  step, logarithmic, or a CSV list), inside its required range;
* IFBW and averaging apply to all traces or per trace: traces with different
  IFBW/averaging are measured in separate sweeps. The smoothing of the
  template only changes how traces are displayed: the measured data, the
  pass / fail and the exported files are not smoothed;
* the limits of the VNA (range, points, IFBW, power, calibration methods) and
  its connectors come from its fields;
* the calibration (method, ports, connectors, cal kit and its name in the VNA)
  comes from the reference planes of the setup (``setups/connections.py``);
* the operator is asked to connect each standard and the EUT, naming the
  plane, the equipment and the connector genders;
* pass / fail compares the measurement with the S2P specification files of
  the model of the EUT (``procedures/specs.py``).

Operator actions are requests to an :class:`Operator` (dialogs in the GUI,
automatic in simulation and tests). When the VNA is simulated the engine also
connects the corresponding device on the simulated bench, so the whole flow
can be exercised without hardware.
"""

from __future__ import annotations

import shutil
import threading
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Callable, Protocol

import numpy as np

from calelektra.catalog.fields import NumberRange
from calelektra.catalog.library import Library, LibraryError
from calelektra.catalog.model import Equipment
from calelektra.core.connectors import FAMILIES, GENDERS, Connector
from calelektra.core.frequency import FrequencyRange
from calelektra.core.issues import Issue, Severity, has_errors
from calelektra.core.networks import LISN_NETWORKS, gamma_to_impedance, lisn_impedance
from calelektra.core.touchstone import Touchstone, TouchstoneError, read_touchstone, write_touchstone
from calelektra.core.units import format_frequency
from calelektra.instruments.base import (
    CALIBRATION_STATUS,
    STANDARD_NAMES,
    InstrumentError,
    VectorNetworkAnalyzer,
    calibration_plan,
)
from calelektra.instruments.dummy_vna import DummyVna
from calelektra.instruments.factory import driver_description, open_vna
from calelektra.instruments.simulation import simulated_dut, unknown_through_model
from calelektra.catalog.model import SpecFile
from calelektra.procedures.model import ALGORITHMS, PLANES_NEEDED, ProcedureTemplate
from calelektra.procedures.record import StepRecord, TestRecord
from calelektra.procedures.specs import VERDICTS, SpecResult, evaluate_spec, verdict
from calelektra.setups.analysis import SetupAnalysis, analyze_setup
from calelektra.setups.connections import CalibrationDesign, PlaneInfo, design_calibration
from calelektra.setups.frequencies import FrequencyPlan
from calelektra.setups.model import SWEEP_MODES, ConfiguredSetup, SetupTemplate
from calelektra.uncertainty.engine import UncertaintyCalculationError
from calelektra.uncertainty.traces import TraceUncertainty, UncertaintyOptions, dataset_uncertainty

STEPS: dict[str, str] = {
    "validacion": "Validación del setup y del procedimiento",
    "conexion": "Conexión con el VNA",
    "configuracion": "Configuración del barrido",
    "calibracion": "Calibración de los puertos",
    "verificacion": "Verificación de la calibración",
    "medida": "Medida del DUT",
    "incertidumbre": "Cálculo de incertidumbres",
    "evaluacion": "Evaluación pasa / falla",
    "exportacion": "Exportación de resultados",
    "cierre": "Cierre del ensayo",
}
STANDARD_IMAGES = {"OPEN": "open", "SHORT": "short", "MATCH": "match", "THROUGH": "through", "UNKNOWN_THROUGH": "through"}


class RunAborted(Exception):
    """The operator stopped the test."""


class StepError(Exception):
    """A step cannot be completed (the reason is shown to the operator)."""


@dataclass
class PromptRequest:
    title: str
    message: str
    kind: str = "conectar"  # conectar | confirmar
    details: list[str] = field(default_factory=list)
    step: str = ""
    image: str | None = None  # hint for the GUI: open, short, match, through, dut, lisn


class Operator(Protocol):
    def request(self, prompt: PromptRequest) -> bool:
        """Show the request; ``True`` to continue, ``False`` to stop the test."""


class AutoOperator:
    """Accepts every request (simulation and tests); keeps them for inspection."""

    def __init__(self, answers: Callable[[PromptRequest], bool] | None = None):
        self.requests: list[PromptRequest] = []
        self._answers = answers

    def request(self, prompt: PromptRequest) -> bool:
        self.requests.append(prompt)
        return self._answers(prompt) if self._answers else True


class RunListener:
    """Progress of a run (the GUI implements it with Qt signals)."""

    def step_changed(self, key: str, status: str, detail: str = "") -> None: ...

    def log(self, level: str, message: str) -> None: ...

    def uncertainty(self, name: str, frequencies: np.ndarray, traces: dict[str, TraceUncertainty]) -> None:
        """Expanded uncertainty of the traces of a measurement (after ``data``)."""

    def data(self, name: str, frequencies: np.ndarray, s: np.ndarray) -> None:
        """New measured data: ``s`` has shape ``(points, n, n)``."""

    def progress(self, fraction: float, text: str = "") -> None: ...


def dut_role_of(template: SetupTemplate | None) -> str | None:
    if template is None:
        return None
    return next((key for key, role in template.roles.items() if role.is_dut), None)


def with_dut(setup: ConfiguredSetup, role: str | None, dut_id: str | None) -> ConfiguredSetup:
    """Copy of the setup with the DUT of the test assigned to its DUT role."""
    if not role or not dut_id:
        return setup
    copy = setup.model_copy(deep=True)
    copy.assignments[role] = dut_id
    copy.set_source_path(setup.source_path)
    return copy


def setup_suits(library: Library, algorithm: str, setup: ConfiguredSetup) -> bool:
    """The setup has the reference planes the algorithm needs (2 for 2 ports, 1 for 1 port) and a DUT role."""
    template = library.resolved_template(setup.template)
    return (
        template is not None
        and len(template.reference_planes) == PLANES_NEEDED[algorithm]
        and dut_role_of(template) is not None
    )


def suitable_setups(library: Library, algorithm: str) -> list[ConfiguredSetup]:
    return sorted((s for s in library.setups.values() if setup_suits(library, algorithm, s)), key=lambda s: s.name)


def eut_choices(library: Library, setup: ConfiguredSetup | None) -> tuple[str | None, str, list[Equipment]]:
    """DUT role of a setup, its label and the equipment of its categories (the possible EUTs)."""
    template = library.resolved_template(setup.template) if setup is not None else None
    role_key = dut_role_of(template)
    if template is None or role_key is None:
        return None, "EUT (equipo a calibrar)", []
    role = template.roles[role_key]
    equipment = sorted(library.equipment_in_categories(role.categories), key=lambda e: e.id)
    return role_key, role.display(role_key), equipment


# ── preparation: everything resolved from the template, its setup, its EUT and the equipment fields ──
@dataclass
class RunPlan:
    procedure: ProcedureTemplate
    setup: ConfiguredSetup | None  # the hardware setup of the template, with its EUT assigned
    template: SetupTemplate | None
    analysis: SetupAnalysis | None
    design: CalibrationDesign | None
    vna: Equipment | None
    dut: Equipment | None
    dut_role: str | None
    frequency_plan: FrequencyPlan | None
    specs: list[tuple[SpecFile, Path]]  # specifications of the model of the EUT, with their files
    lines: list[str]
    issues: list[Issue] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not has_errors(self.issues)

    @property
    def frequency_range(self) -> FrequencyRange | None:
        if self.frequency_plan is not None:
            return self.frequency_plan.range
        return self.setup.required_range if self.setup is not None else None

    @property
    def ports(self) -> tuple[int, ...]:
        if self.design is not None and self.design.ports:
            return self.design.ports
        return (1, 2) if self.procedure.algorithm == "vna_2_puertos" else (1,)

    @property
    def method(self) -> str:
        if self.procedure.calibration.method == "existente":
            return "existente"
        return self.design.method if self.design and self.design.method else "—"


def prepare_run(library: Library, procedure: ProcedureTemplate, today: date | None = None) -> RunPlan:
    """Check that the test template (its hardware setup and its EUT) can be launched and resolve what it needs."""
    issues: list[Issue] = []

    def error(message: str, code: str = "ensayo") -> None:
        issues.append(Issue(Severity.ERROR, code, message))

    def warning(message: str, code: str = "ensayo") -> None:
        issues.append(Issue(Severity.WARNING, code, message))

    setup = library.setups.get(procedure.setup_id) if procedure.setup_id else None
    if setup is None:
        error(
            f"El hardware setup «{procedure.setup_id}» de la plantilla no existe"
            if procedure.setup_id
            else "Elija el hardware setup de la plantilla de ensayo",
            "ensayo.setup",
        )
        return RunPlan(procedure, None, None, None, None, None, None, None, None, [], ["—"], issues)
    template = library.resolved_template(setup.template)
    dut_role = dut_role_of(template)
    eut_id = procedure.eut_id
    if not eut_id:
        error("Elija el EUT (equipo a calibrar) de la plantilla de ensayo", "ensayo.eut")
    elif eut_id not in library.equipment:
        error(f"El EUT «{eut_id}» no existe en el inventario", "ensayo.eut")
        eut_id = None
    if template is not None and dut_role is None:
        error(f"La plantilla «{template.name}» del setup no tiene rol de equipo a calibrar (DUT)")
    effective = with_dut(setup, dut_role, eut_id)
    analysis = analyze_setup(effective, library, today) if template is not None else None
    if analysis is not None:
        issues.extend(i for i in analysis.issues if i.severity is not Severity.INFO)
    else:
        error(f"La plantilla «{setup.template}» del setup no existe")
    vna_ids = effective.ids_for("vna")
    vna = library.equipment.get(vna_ids[0]) if vna_ids else None
    dut = library.equipment.get(eut_id) if eut_id else None
    if vna is None:
        error("El setup no tiene VNA asignado")

    design = None
    method = procedure.calibration.method
    if template is not None:
        override = None if method in ("automatico", "existente") else method
        design = design_calibration(template, effective, library, method_override=override)
        if override:
            issues.extend(Issue(s, "calibracion.vna", m) for s, m in design.checks if s in (Severity.ERROR, Severity.WARNING))
        planes_needed = PLANES_NEEDED[procedure.algorithm]
        if len(design.planes) != planes_needed:
            error(
                f"El algoritmo «{ALGORITHMS[procedure.algorithm]}» necesita {planes_needed} plano(s) de referencia "
                f"y la plantilla «{template.name}» del setup define {len(design.planes)}"
            )
        elif method != "existente" and design.method is None:
            error(f"La plantilla «{template.name}» no calibra los puertos del VNA: {design.reason}")
        if procedure.algorithm == "vna_2_puertos" and method == "OSM":
            error("OSM es una calibración de 1 puerto: no sirve para medir 2 puertos")
        if procedure.algorithm == "vna_1_puerto" and method in ("TOSM", "UOSM"):
            error(f"{method} calibra 2 puertos: use OSM para la medida de 1 puerto")

    # frequency points: those of the hardware setup (checked by the setup analysis)
    frequency_plan = analysis.frequency_plan if analysis is not None else None
    if dut is not None and frequency_plan is not None:
        dut_range = library.frequency_range_of(dut).range
        if dut_range is not None and not dut_range.contains(frequency_plan.range):
            warning(f"Las frecuencias del setup ({frequency_plan.range}) salen del rango nominal de «{dut.id}» ({dut_range})", "ensayo.rango_dut")
    sweep = procedure.sweep
    if vna is not None:
        ifbw = library.field(vna, "ifbw")
        if isinstance(ifbw, FrequencyRange):
            for trace in procedure.traces:
                bandwidth = sweep.trace(trace)[0]
                if not ifbw.start <= bandwidth <= ifbw.stop:
                    error(f"IFBW de {format_frequency(bandwidth)} ({trace}) fuera del rango de «{vna.id}» ({ifbw})")
                    break
        power = library.field(vna, "potencia_salida_dbm")
        if isinstance(power, NumberRange) and not power.low <= sweep.power_dbm <= power.high:
            error(f"Potencia de {sweep.power_dbm:g} dBm fuera del rango de «{vna.id}» ({power} dBm)")

    # uncertainty of the traces: data file of the VNA unit
    if vna is not None and procedure.uncertainty.active:
        if not vna.uncertainty_file:
            warning(f"El VNA «{vna.id}» no tiene fichero de incertidumbres: los resultados no tendrán incertidumbre", "ensayo.incertidumbre")
        else:
            for _severity, message in library.uncertainty_problems(vna):
                warning(f"Incertidumbre de «{vna.id}»: {message}", "ensayo.incertidumbre")
            try:
                covered = library.uncertainty_engine(vna).frequency_range_hz
            except UncertaintyCalculationError:
                covered = None
            if covered and frequency_plan is not None and not (covered[0] <= frequency_plan.start and frequency_plan.stop <= covered[1]):
                warning(
                    f"El fichero de incertidumbres de «{vna.id}» cubre {FrequencyRange(*covered)}: fuera de ese rango no habrá incertidumbre",
                    "ensayo.incertidumbre",
                )

    # specifications of the model of the EUT (pass / fail)
    specs = library.specifications_of(dut) if dut is not None else []
    for spec, path in specs:
        try:
            data = read_touchstone(path)
        except TouchstoneError as exc:
            error(f"Especificación «{spec.name}» del modelo de «{dut.id}»: {exc}", "ensayo.especificacion")
            continue
        missing = [p for p in spec.parameters if p not in procedure.traces or int(p[1]) > data.n_ports or int(p[2]) > data.n_ports]
        if missing:
            error(f"Especificación «{spec.name}»: {', '.join(missing)} no está en el fichero o no lo mide la plantilla", "ensayo.especificacion")
        if frequency_plan is not None:
            overlap = data.frequency_range.intersect(frequency_plan.range)
            if overlap is None:
                warning(f"Especificación «{spec.name}» ({data.frequency_range}) fuera de las frecuencias del setup: no se evaluará", "ensayo.especificacion")
            elif not data.frequency_range.contains(frequency_plan.range):
                warning(f"Especificación «{spec.name}»: solo se evalúa en {overlap}", "ensayo.especificacion")

    lines = ["—"]
    if dut is not None and library.category_id_of(dut) == "lisn":
        lines = list(library.field(dut, "lineas", []) or [])
        wanted_lines = procedure.options.get("lineas", "todas")
        if isinstance(wanted_lines, (list, tuple)):
            unknown = [line for line in wanted_lines if line not in lines]
            if unknown:
                error(f"La LISN «{dut.id}» no tiene las líneas {', '.join(unknown)} (campo «Líneas»: {', '.join(lines)})")
            lines = [line for line in lines if line in wanted_lines]
        if not lines:
            error(f"La LISN «{dut.id}» no indica sus líneas (campo «Líneas»)")
        network = library.field(dut, "red_nominal")
        if network not in LISN_NETWORKS:
            warning(f"La red nominal de «{dut.id}» ({network}) no es conocida: no se calcularán desviaciones")
    return RunPlan(procedure, effective, template, analysis, design, vna, dut, dut_role, frequency_plan, specs, lines, issues)


@dataclass
class RunResult:
    record: TestRecord
    frequencies: np.ndarray | None = None
    data: dict[str, np.ndarray] = field(default_factory=dict)  # name → complex (points, n, n)
    files: list[Path] = field(default_factory=list)
    spec_results: list[tuple[str, SpecResult]] = field(default_factory=list)  # (data set, result)
    uncertainty: dict[str, dict[str, TraceUncertainty]] = field(default_factory=dict)  # data set → trace → U

    @property
    def status(self) -> str:
        return self.record.status


def _db(values: np.ndarray) -> np.ndarray:
    with np.errstate(divide="ignore"):
        return 20 * np.log10(np.maximum(np.abs(values), 1e-15))


def _largest(arrays: list[np.ndarray]) -> float | None:
    values = np.concatenate(arrays) if arrays else np.array([])
    values = values[np.isfinite(values)]
    return float(values.max()) if len(values) else None


def _csv_number(value: float) -> str:
    return f"{value:.7g}".replace(".", ",")


class ProcedureRunner:
    """Runs a test template: launching it creates the test record in ``folder``.

    ``run()`` blocks; ``stop()`` may be called from another thread. The
    record is created when the run starts (default folder: ``ensayos/`` of
    the library; default name: EUT and date).
    """

    def __init__(
        self,
        library: Library,
        procedure: ProcedureTemplate,
        operator: Operator | None = None,
        listener: RunListener | None = None,
        operator_name: str | None = None,
        today: date | None = None,
        seed: int | None = None,
        folder: Path | None = None,
        name: str | None = None,
        temperature: tuple[float, float] | None = None,
    ):
        self.library = library
        self.procedure = procedure
        self.operator = operator or AutoOperator()
        self.listener = listener or RunListener()
        self.operator_name = operator_name
        self.today = today
        self.seed = seed
        self.folder = folder
        self.name = name
        self.temperature = temperature  # minimum and maximum ambient temperature (°C) during the test
        self.plan: RunPlan | None = None
        self.vna: VectorNetworkAnalyzer | None = None
        self.result: RunResult | None = None
        self._stop = threading.Event()
        self._log_lines: list[str] = []
        self._verification_failed = False

    # ── control ────────────────────────────────────────────────────────────
    def stop(self) -> None:
        self._stop.set()

    def _check_stop(self) -> None:
        if self._stop.is_set():
            raise RunAborted("Ensayo detenido por el operador")

    def _log(self, message: str, level: str = "info") -> None:
        self._log_lines.append(f"{datetime.now():%H:%M:%S}  [{level}] {message}")
        self.listener.log(level, message)

    def _ask(
        self,
        title: str,
        message: str,
        kind: str = "conectar",
        details: list[str] | None = None,
        image: str | None = None,
        simulate: Callable[[DummyVna], None] | None = None,
        step: str = "",
    ) -> bool:
        self._check_stop()
        prompt = PromptRequest(title, message, kind, list(details or []), step, image)
        self._log(f"Operador: {title} — {message}", "operador")
        accepted = self.operator.request(prompt)
        self._check_stop()
        if not accepted and kind == "conectar":
            raise RunAborted(f"Cancelado por el operador en «{title}»")
        if accepted and simulate is not None and isinstance(self.vna, DummyVna):
            simulate(self.vna)
        return accepted

    # ── run ────────────────────────────────────────────────────────────────
    def steps(self) -> list[str]:
        return list(STEPS)

    def _new_record(self) -> TestRecord:
        name = self.name or f"{self.procedure.eut_id or self.procedure.id} {datetime.now():%Y-%m-%d %H%M%S}"
        record = self.library.new_test_record(self.folder or self.library.tests_folder, name, self.procedure, self.operator_name)
        if self.temperature is not None:
            record.conditions = {"temperatura_min_c": self.temperature[0], "temperatura_max_c": self.temperature[1]}
        return record

    def run(self) -> RunResult:
        record = self._new_record()
        self.result = RunResult(record=record)
        self._log(f"Ensayo {record.id}: plantilla «{self.procedure.name}», setup «{self.procedure.setup_id}», EUT «{self.procedure.eut_id}»")
        current = None
        try:
            for key in self.steps():
                self._check_stop()
                current = key
                self.listener.step_changed(key, "en_curso")
                self._log(f"── {STEPS[key]}")
                status, detail = getattr(self, f"_step_{key}")()
                self._finish_step(key, status, detail)
                current = None
            record.status = "verificacion_fallida" if self._verification_failed else "completado"
            record.message = "Ensayo completado" + (" con la verificación de la calibración fuera de tolerancia" if self._verification_failed else "")
            if record.verdict and record.verdict != "SIN_ESPECIFICACIONES":
                record.message += f" — {VERDICTS[record.verdict].upper()}"
        except RunAborted as exc:
            record.status, record.message = "abortado", str(exc)
            if current:
                self._finish_step(current, "abortado", str(exc))
            self._log(str(exc), "aviso")
        except (StepError, InstrumentError, OSError, ValueError, LibraryError) as exc:
            record.status, record.message = "error", str(exc)
            if current:
                self._finish_step(current, "error", str(exc))
            self._log(str(exc), "error")
        finally:
            self._close_instrument()
            record.finished = datetime.now()
            self._save_record()
        self._log(f"Fin del ensayo: {record.status_name}")
        return self.result

    def _finish_step(self, key: str, status: str, detail: str) -> None:
        assert self.result is not None
        self.result.record.steps.append(StepRecord(key=key, name=STEPS[key], status=status, detail=detail, finished=datetime.now()))
        self.listener.step_changed(key, status, detail)
        if detail:
            self._log(f"{STEPS[key]}: {detail}", "error" if status == "error" else "info")

    # ── steps ──────────────────────────────────────────────────────────────
    def _step_validacion(self) -> tuple[str, str]:
        record = self.result.record  # type: ignore[union-attr]
        plan = prepare_run(self.library, self.procedure, self.today)
        self.plan = plan
        warnings = [i for i in plan.issues if i.severity is Severity.WARNING]
        for issue in plan.issues:
            self._log(issue.message, "error" if issue.severity is Severity.ERROR else "aviso")
        if not plan.ok:
            errors = [i.message for i in plan.issues if i.severity is Severity.ERROR]
            raise StepError(f"No se puede ejecutar: {errors[0]}" + (f" (y {len(errors) - 1} errores más)" if len(errors) > 1 else ""))
        assert plan.setup is not None
        setup = self.library.setups[plan.setup.id]
        record.setup = {
            "id": setup.id,
            "nombre": setup.name,
            "plantilla": setup.template,
            "rango_requerido": setup.required_range.to_yaml(),
            "frecuencias": setup.frequencies.model_dump(by_alias=True, exclude_none=True, mode="json"),
        }
        record.equipment = [self._equipment_entry(role, eq_id) for role, eq_id in self._assigned()]
        if plan.dut is not None:
            record.dut = self._equipment_entry(plan.dut_role or "dut", plan.dut.id)
        if plan.frequency_plan is not None:
            record.frequency_range = [format_frequency(plan.frequency_plan.start, decimal="."), format_frequency(plan.frequency_plan.stop, decimal=".")]
        detail = "Plantilla, hardware setup y EUT compatibles"
        if warnings:
            detail += f" ({len(warnings)} avisos)"
        return ("aviso" if warnings else "ok"), detail

    def _assigned(self) -> list[tuple[str, str]]:
        assert self.plan is not None and self.plan.template is not None
        pairs = []
        for role in self.plan.template.roles:
            for equipment_id in self.plan.setup.ids_for(role):
                pairs.append((role, equipment_id))
        return pairs

    def _equipment_entry(self, role: str, equipment_id: str) -> dict:
        eq = self.library.equipment[equipment_id]
        model = self.library.model_of(eq)
        entry: dict = {"rol": role, "id": eq.id, "modelo": model.display_name if model else eq.model_id}
        if eq.serial_number:
            entry["numero_serie"] = eq.serial_number
        if eq.calibration is not None:
            entry["certificado"] = eq.calibration.certificate
            if eq.calibration.due:
                entry["calibracion_vence"] = eq.calibration.due.isoformat()
        if eq.s_parameters is not None:
            entry["datos_s"] = {"fichero": Path(eq.s_parameters.file).name, "referencia": eq.s_parameters.reference}
        fields = self.library.fields_of(eq)
        values = {key: resolved.text for key, resolved in fields.items() if resolved.value is not None and resolved.spec.group in ("RF", "Conectores", "Calibración", "General")}
        if values:
            entry["campos"] = values
        return entry

    def _step_conexion(self) -> tuple[str, str]:
        assert self.plan is not None and self.plan.vna is not None
        vna_eq = self.plan.vna
        self.vna = open_vna(vna_eq, self.library, seed=self.seed)
        self.vna.connect()
        identity = self.vna.identify()
        record = self.result.record  # type: ignore[union-attr]
        record.simulated = self.vna.simulated
        record.instrument = {
            "vna": vna_eq.id,
            "identificacion": identity,
            "driver": driver_description(vna_eq, self.library),
            "simulado": self.vna.simulated,
        }
        if self.vna.simulated:
            self._log("VNA SIMULADO: los resultados no son una medida real", "aviso")
        return "ok", f"{identity}" + (" (simulado)" if self.vna.simulated else "")

    def _trace_settings(self) -> dict[str, dict]:
        settings = {}
        for trace in self.procedure.traces:
            ifbw, averages, smoothing = self.procedure.sweep.trace(trace)
            settings[trace] = {"ifbw": format_frequency(ifbw, decimal="."), "promedios": averages, "suavizado_visualizacion_pct": smoothing}
        return settings

    def _step_configuracion(self) -> tuple[str, str]:
        assert self.plan is not None and self.vna is not None and self.plan.frequency_plan is not None
        sweep = self.procedure.sweep
        plan = self.plan.frequency_plan
        self.vna.preset()
        self.vna.set_if_bandwidth(sweep.if_bandwidth)
        self.vna.set_power(sweep.power_dbm)
        self.vna.set_averaging(sweep.averages)
        if plan.mode == "lista":
            self.vna.configure_frequency_list(plan.frequencies)  # segmented sweep on the ZNB, with the IFBW and power above
        else:
            self.vna.configure_sweep(plan.start, plan.stop, plan.points, "logaritmico" if plan.mode == "logaritmico" else "lineal")
        record = self.result.record  # type: ignore[union-attr]
        record.sweep = {
            "modo": plan.mode,
            "inicio": format_frequency(plan.start, decimal="."),
            "fin": format_frequency(plan.stop, decimal="."),
            "puntos": plan.points,
            "ifbw": format_frequency(sweep.if_bandwidth, decimal="."),
            "potencia_dbm": sweep.power_dbm,
            "promedios": sweep.averages,
            "suavizado_visualizacion_pct": sweep.smoothing_pct,
            "por_traza": sweep.per_trace,
        }
        if plan.mode == "lineal" and plan.points > 1:
            record.sweep["paso"] = format_frequency(plan.frequencies[1] - plan.frequencies[0], decimal=".")
        if plan.list_path is not None and record.folder is not None:
            record.sweep["lista"] = self._keep_file(plan.list_path, "listas")
        if sweep.per_trace:
            record.sweep["trazas"] = self._trace_settings()
        text = plan.describe() + f", IFBW {format_frequency(sweep.if_bandwidth)}, {sweep.power_dbm:g} dBm"
        if sweep.averages > 1:
            text += f", {sweep.averages} promedios"
        if sweep.per_trace:
            text += " (ajustes por traza)"
        return "ok", text

    def _plane_text(self, plane: PlaneInfo) -> str:
        if plane.endpoint is None or plane.endpoint.equipment is None:
            return plane.name
        eq = plane.endpoint.equipment
        connector = f", {plane.connector.label()}" if plane.connector else ""
        if self.plan and self.plan.vna is not None and eq.id == self.plan.vna.id:
            return f"{plane.name} (puerto {plane.endpoint.endpoint.port} del VNA{connector})"
        return f"{plane.name} (extremo {plane.endpoint.endpoint.port} de «{eq.id}»{connector})"

    def _standard_text(self, standard: str, plane: PlaneInfo) -> str:
        name = STANDARD_NAMES[standard]
        connector = plane.connector
        if connector is None or connector.gender == "sin_genero":
            return name
        return f"{name} {GENDERS[connector.mating_gender()]}"

    def _step_calibracion(self) -> tuple[str, str]:
        assert self.plan is not None and self.vna is not None
        record = self.result.record  # type: ignore[union-attr]
        if self.procedure.calibration.method == "existente":
            status = self.vna.calibration_status()
            record.calibration = {"metodo": "existente", "estado_vna": status}
            if status not in ("CAL", "CAI"):
                raise StepError(f"El VNA no tiene una calibración válida activa ({CALIBRATION_STATUS.get(status, status)})")
            return "ok", f"Se usa la calibración activa del VNA ({CALIBRATION_STATUS[status]})"
        design = self.plan.design
        assert design is not None and design.method is not None
        method, ports = design.method, design.ports
        planes = {plane.vna_port: plane for plane in design.planes}
        kit = design.kit
        kit_text = f"kit «{kit.id}»" + (f" ({design.kit_name})" if design.kit_name else "") if kit else "kit seleccionado en el VNA"
        self._log(f"{design.reason}")
        self.vna.begin_calibration(method, ports, design.connectors, design.kit_name, design.kit_family)
        steps = calibration_plan(method, ports)
        for index, (standard, standard_ports) in enumerate(steps, start=1):
            self._check_stop()
            self.listener.progress(index / (len(steps) + 1), f"Estándar {index}/{len(steps)}")
            if standard in ("OPEN", "SHORT", "MATCH"):
                plane = planes[standard_ports[0]]
                title = f"Calibración {method} — {index}/{len(steps)}: {STANDARD_NAMES[standard]} en {plane.name}"
                message = f"Conecte el estándar {self._standard_text(standard, plane)} del {kit_text} en el plano {self._plane_text(plane)}."
                simulate = lambda vna, s=standard, p=standard_ports[0]: vna.connect_standard(s, p)  # noqa: E731
            elif standard == "THROUGH":
                a, b = planes[standard_ports[0]], planes[standard_ports[1]]
                title = f"Calibración {method} — {index}/{len(steps)}: through plano"
                message = f"Una directamente los planos {self._plane_text(a)} y {self._plane_text(b)} (through plano, sin adaptador)."
                simulate = lambda vna, p=standard_ports: vna.connect_through(p)  # noqa: E731
            else:
                a, b = planes[standard_ports[0]], planes[standard_ports[1]]
                adapter = self._through_adapter_text(a.connector, b.connector)
                title = f"Calibración {method} — {index}/{len(steps)}: through desconocido"
                message = (
                    f"Conecte un through desconocido entre {self._plane_text(a)} y {self._plane_text(b)}: "
                    f"{adapter}. Debe ser recíproco y eléctricamente corto."
                )
                simulate = lambda vna, p=standard_ports: vna.connect_through(p, model=unknown_through_model(), label="adaptador (through desconocido)")  # noqa: E731
            self._ask(title, message, image=STANDARD_IMAGES[standard], simulate=simulate, step="calibracion")
            self.vna.acquire_standard(standard, standard_ports)
            self._log(f"Adquirido: {STANDARD_NAMES[standard]} en {'-'.join(f'P{p}' for p in standard_ports)}")
        self.vna.apply_calibration()
        status = self.vna.calibration_status()
        if status not in ("CAL", "CAI"):
            raise StepError(f"La calibración no ha quedado activa en el VNA ({CALIBRATION_STATUS.get(status, status)})")
        record.calibration = {
            "metodo": method,
            "motivo": design.reason,
            "puertos": list(ports),
            "planos": {plane.name: (plane.connector.label() if plane.connector else "—") for plane in design.planes},
            "kit": kit.id if kit else None,
            "kit_en_vna": design.kit_name,
            "certificado_kit": kit.calibration.certificate if kit and kit.calibration else None,
            "estado_vna": status,
            "fecha": datetime.now().isoformat(timespec="seconds"),
        }
        if self.procedure.calibration.save_on_vna:
            name = self.procedure.calibration.vna_cal_name.format(ensayo=record.id, setup=self.plan.setup.id if self.plan.setup else "")
            self.vna.save_calibration(name)
            record.calibration["guardada_en_vna"] = name
        return "ok", f"{method} en {', '.join(f'P{p}' for p in ports)} ({CALIBRATION_STATUS[status]})"

    @staticmethod
    def _through_adapter_text(a: Connector | None, b: Connector | None) -> str:
        if a is None or b is None:
            return "un adaptador o cable corto"
        if a.family == b.family:
            return f"un adaptador {FAMILIES[a.family]} {GENDERS[a.mating_gender()]}–{GENDERS[b.mating_gender()]}"
        return f"un adaptador {FAMILIES[a.family]} {GENDERS[a.mating_gender()]} – {FAMILIES[b.family]} {GENDERS[b.mating_gender()]}"

    def _measure(self, ports: tuple[int, ...]) -> tuple[np.ndarray, np.ndarray]:
        """One sweep with the settings of «all traces» (calibration checks)."""
        assert self.vna is not None
        self.vna.single_sweep()
        frequencies = self.vna.frequencies()
        return frequencies, self.vna.read_s_matrix(ports)

    def _measure_dut(self, ports: tuple[int, ...]) -> tuple[np.ndarray, np.ndarray]:
        """Measure the traces of the DUT with their own IFBW / averaging (not smoothed: smoothing is only for display).

        Traces that share IFBW and averaging are read from the same sweep; each
        other combination needs its own sweep (on the VNA they are channel
        settings). The «all traces» settings are restored afterwards.
        """
        assert self.vna is not None
        sweep = self.procedure.sweep
        n = len(ports)
        names = [f"S{i + 1}{j + 1}" for i in range(n) for j in range(n)]
        groups: dict[tuple[float, int], list[str]] = {}
        for name in names:
            ifbw, averages, _ = sweep.trace(name)
            groups.setdefault((ifbw, averages), []).append(name)
        frequencies: np.ndarray | None = None
        s = np.empty((0, n, n), dtype=complex)
        current = (sweep.if_bandwidth, sweep.averages)
        for (ifbw, averages), members in groups.items():
            if (ifbw, averages) != current:
                self.vna.set_if_bandwidth(ifbw)
                self.vna.set_averaging(averages)
                current = (ifbw, averages)
            self.vna.single_sweep()
            if frequencies is None:
                frequencies = self.vna.frequencies()
                s = np.empty((len(frequencies), n, n), dtype=complex)
            for name in members:
                i, j = int(name[1]) - 1, int(name[2]) - 1
                s[:, i, j] = self.vna.read_s_parameter(f"S{ports[i]}{ports[j]}")
            if len(groups) > 1:
                self._log(f"Barrido con IFBW {format_frequency(ifbw)} y {averages} promedios: {', '.join(members)}")
        if current != (sweep.if_bandwidth, sweep.averages):
            self.vna.set_if_bandwidth(sweep.if_bandwidth)
            self.vna.set_averaging(sweep.averages)
        assert frequencies is not None
        return frequencies, s

    def _step_verificacion(self) -> tuple[str, str]:
        assert self.plan is not None and self.vna is not None
        settings = self.procedure.verification
        method = self.plan.method
        record = self.result.record  # type: ignore[union-attr]
        if not settings.active or method == "existente":
            record.verification = {"realizada": False}
            return "omitido", "Verificación desactivada en el procedimiento" if not settings.active else "Calibración existente: sin verificación"
        design = self.plan.design
        assert design is not None
        planes = {plane.vna_port: plane for plane in design.planes}
        ports = design.ports
        checks: list[dict] = []
        if method == "TOSM":
            a, b = planes[ports[0]], planes[ports[1]]
            self._ask(
                "Verificación: through plano",
                f"Una de nuevo directamente los planos {self._plane_text(a)} y {self._plane_text(b)}.",
                image="through",
                simulate=lambda vna: vna.connect_through(ports),
                step="verificacion",
            )
            f, s = self._measure(ports)
            self.listener.data("Verificación (through)", f, s)
            deviation = float(np.max(np.abs(_db(s[:, 1, 0]))))
            reflection = float(np.max(_db(np.concatenate([s[:, 0, 0], s[:, 1, 1]]))))
            checks.append({"comprobacion": "|S21| del through plano", "valor_db": round(deviation, 4), "limite_db": settings.s21_tolerance_db, "correcto": deviation <= settings.s21_tolerance_db})
            checks.append({"comprobacion": "|S11|, |S22| del through plano", "valor_db": round(reflection, 2), "limite_db": settings.s11_max_db, "correcto": reflection <= settings.s11_max_db})
        elif method == "UOSM":
            a, b = planes[ports[0]], planes[ports[1]]
            self._ask(
                "Verificación: reciprocidad del through",
                f"Vuelva a conectar el through desconocido entre {self._plane_text(a)} y {self._plane_text(b)}.",
                image="through",
                simulate=lambda vna: vna.connect_through(ports, model=unknown_through_model(), label="adaptador (through desconocido)"),
                step="verificacion",
            )
            f, s = self._measure(ports)
            self.listener.data("Verificación (through desconocido)", f, s)
            reciprocity = float(np.max(np.abs(_db(s[:, 1, 0]) - _db(s[:, 0, 1]))))
            checks.append({"comprobacion": "Reciprocidad |S21| − |S12| del through", "valor_db": round(reciprocity, 4), "limite_db": settings.s21_tolerance_db, "correcto": reciprocity <= settings.s21_tolerance_db})
        for port in ports:
            plane = planes[port]
            self._ask(
                f"Verificación: carga adaptada en {plane.name}",
                f"Conecte de nuevo el estándar {self._standard_text('MATCH', plane)} en el plano {self._plane_text(plane)}.",
                image="match",
                simulate=lambda vna, p=port: vna.connect_standard("MATCH", p),
                step="verificacion",
            )
            f, s = self._measure((port,))
            value = float(np.max(_db(s[:, 0, 0])))
            checks.append({"comprobacion": f"|Γ| de la carga en {plane.name}", "valor_db": round(value, 2), "limite_db": settings.s11_max_db, "correcto": value <= settings.s11_max_db})
        passed = all(check["correcto"] for check in checks)
        record.verification = {"realizada": True, "correcta": passed, "comprobaciones": checks}
        summary = "; ".join(f"{c['comprobacion']}: {c['valor_db']:g} dB ({'OK' if c['correcto'] else 'FUERA'})" for c in checks)
        if not passed:
            self._verification_failed = True
            accepted = self._ask(
                "Verificación fuera de tolerancia",
                f"La verificación de la calibración no cumple: {summary}. ¿Continuar con la medida igualmente?",
                kind="confirmar",
                step="verificacion",
            )
            if not accepted:
                raise RunAborted("Verificación de la calibración fuera de tolerancia")
            return "aviso", summary
        return "ok", summary

    def _step_medida(self) -> tuple[str, str]:
        assert self.plan is not None and self.vna is not None and self.plan.dut is not None
        dut = self.plan.dut
        result = self.result
        assert result is not None
        status = self.vna.calibration_status()
        if status not in ("CAL", "CAI"):
            raise StepError(f"La corrección del VNA no está activa ({CALIBRATION_STATUS.get(status, status)})")
        if self.procedure.algorithm == "vna_2_puertos":
            ports = self.plan.ports
            planes = {plane.vna_port: plane for plane in self.plan.design.planes} if self.plan.design else {}
            a = self.library.port_connector(dut, "A")
            b = self.library.port_connector(dut, "B")
            ends = f" ({a.label() if a else '?'} – {b.label() if b else '?'})"
            where = " y ".join(
                f"extremo {end} en {self._plane_text(planes[port]) if port in planes else f'P{port}'}" for end, port in zip("AB", ports)
            )
            n_ports, model = simulated_dut(self.library, dut)
            self._ask(
                "Conectar el DUT",
                f"Conecte el DUT «{dut.id}»{ends}: {where}.",
                image="dut",
                simulate=lambda vna: vna.connect_network(model, ports, dut.id),
                step="medida",
            )
            f, s = self._measure_dut(ports)
            result.frequencies, result.data["DUT"] = f, s
            self.listener.data("DUT", f, s)
            return "ok", f"{len(f)} puntos, S11 S21 S12 S22 ({CALIBRATION_STATUS[status]})"
        # one port: one measurement per LISN line (or a single one)
        port = self.plan.ports[0]
        plane = self.plan.design.planes[0] if self.plan.design and self.plan.design.planes else None
        is_lisn = self.library.category_id_of(dut) == "lisn"
        control = self.library.field(dut, "control", "manual")
        for index, line in enumerate(self.plan.lines):
            self._check_stop()
            self.listener.progress(index / max(len(self.plan.lines), 1), f"Línea {line}")
            if is_lisn:
                title = f"LISN «{dut.id}» — línea {line}"
                message = f"Conecte el adaptador de medida al terminal EUT de la línea {line}"
                if control != "remoto":
                    message += f" y seleccione la línea {line} en la LISN"
                message += "; el puerto receptor terminado en 50 Ω."
            else:
                title = "Conectar el DUT"
                message = f"Conecte el DUT «{dut.id}» en el plano {self._plane_text(plane) if plane else f'P{port}'}."
            n_ports, model = simulated_dut(self.library, dut, index)
            self._ask(title, message, image="lisn" if is_lisn else "dut", simulate=lambda vna, m=model: vna.connect_one_port(m, port, f"{dut.id} {line}"), step="medida")
            f, s = self._measure_dut((port,))
            name = line if line != "—" else "DUT"
            result.frequencies, result.data[name] = f, s
            self.listener.data(name, f, s)
            self._log(f"Medida {name}: {len(f)} puntos")
        return "ok", f"{len(self.plan.lines)} medida(s) de 1 puerto ({CALIBRATION_STATUS[status]})"

    def _step_incertidumbre(self) -> tuple[str, str]:
        assert self.plan is not None and self.result is not None
        result, record = self.result, self.result.record
        settings = self.procedure.uncertainty
        vna = self.plan.vna
        if not settings.active:
            return "omitido", "Desactivada en la plantilla de ensayo"
        if vna is None or not vna.uncertainty_file:
            return "omitido", f"El VNA «{vna.id if vna else '—'}» no tiene fichero de incertidumbres"
        try:
            engine = self.library.uncertainty_engine(vna)
            designation = self.library.uncertainty_designation(vna)
            engine.validate_equipment(designation)
        except UncertaintyCalculationError as exc:
            self._log(f"Sin incertidumbre: {exc}", "aviso")
            return "aviso", f"No se calcula: {exc}"
        assert result.frequencies is not None
        today = self.today or date.today()
        options = UncertaintyOptions(settings.coverage_factor, settings.trace_noise, settings.temperature_stability, self.temperature)
        due, _valid, warnings = engine.calibration_status(today)
        path = self.library.uncertainty_path(vna)
        assert path is not None
        entry: dict = {
            "fichero": self._keep_file(path, "incertidumbres"),
            "documento": engine.document,
            "vna": f"{vna.id} — {designation}" + (f" nº {engine.serial_number}" if engine.serial_number else ""),
            "k": settings.coverage_factor,
            "ruido_traza": settings.trace_noise,
            "estabilidad_temperatura": settings.temperature_stability,
            "temperatura_c": list(self.temperature) if self.temperature else "no indicada",
            "calibracion_vna_hasta": due,
        }
        if warnings:
            entry["avisos"] = warnings
        for message in warnings:
            self._log(message, "aviso")
        measurements = []
        missing = 0
        for name, s in result.data.items():
            self._check_stop()
            traces = dataset_uncertainty(
                engine, designation, result.frequencies, s, lambda trace: self.procedure.sweep.trace(trace)[0], options, today
            )
            result.uncertainty[name] = traces
            self.listener.uncertainty(name, result.frequencies, traces)
            measurements.append({"medida": name, "trazas": {p: t.summary() for p, t in traces.items()}})
            for trace in traces.values():
                missing += trace.missing
                for problem in trace.problems:
                    self._log(f"Incertidumbre{f' ({name})' if len(result.data) > 1 else ''}: {problem}", "aviso")
        entry["medidas"] = measurements
        record.uncertainty = entry
        largest = []
        for parameter in dict.fromkeys(p for traces in result.uncertainty.values() for p in traces):
            traces = [t[parameter] for t in result.uncertainty.values() if parameter in t]
            db = _largest([t.magnitude_db for t in traces])
            deg = _largest([t.phase_deg for t in traces])
            if db is not None:
                largest.append(f"{parameter} {db:.3g} dB" + (f" / {deg:.3g}°" if deg is not None else ""))
        detail = f"U (k={settings.coverage_factor:g}) máx.: " + ("; ".join(largest) or "ningún punto")
        if missing:
            detail += f" — {missing} punto(s) sin incertidumbre (fuera de los rangos del fichero)"
        if warnings:
            detail += " — VNA fuera de calibración según el fichero"
        return ("aviso" if missing or warnings else "ok"), detail

    def _step_evaluacion(self) -> tuple[str, str]:
        assert self.result is not None
        assert self.plan is not None
        result, record = self.result, self.result.record
        specs = self.plan.specs
        if not specs:
            record.verdict = "SIN_ESPECIFICACIONES"
            model = self.library.model_of(self.plan.dut) if self.plan.dut else None
            return "omitido", f"El modelo del EUT{f' «{model.id}»' if model else ''} no tiene especificaciones S"
        assert result.frequencies is not None and record.folder is not None
        several = len(result.data) > 1
        for spec, source in specs:
            used = spec.model_dump(by_alias=True, exclude_none=True, mode="json")
            if source.is_file():
                used["fichero"] = self._keep_file(source, "especificaciones")  # the copy kept with the test
            record.specifications.append(used)
            for dataset, s in result.data.items():
                evaluation = evaluate_spec(spec, source, result.frequencies, s)
                result.spec_results.append((dataset, evaluation))
                entry = evaluation.to_record()
                if several:
                    entry["medida"] = dataset
                record.spec_results.append(entry)
                self._log(f"Especificación «{spec.name}»{f' ({dataset})' if several else ''}: {entry['resultado']}", "info" if evaluation.passed else "aviso")
        record.verdict = verdict([r for _, r in result.spec_results])
        detail = "; ".join(
            f"{r.spec.name}{f' ({d})' if several else ''}: {'PASA' if r.passed else ('FALLA' if r.passed is False else 'no evaluado')}"
            for d, r in result.spec_results
        )
        return ("ok" if record.verdict == "PASA" else "aviso"), f"{VERDICTS[record.verdict].upper()} — {detail}"

    def _keep_file(self, source: Path, subfolder: str) -> str:
        """Keep ``source`` in the test folder (copied if it is elsewhere); returns its path relative to the folder."""
        record = self.result.record  # type: ignore[union-attr]
        assert record.folder is not None
        try:
            relative = source.resolve().relative_to(record.folder.resolve()).as_posix()
        except ValueError:
            target = record.folder / subfolder / source.name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            relative = f"{subfolder}/{source.name}"
        if relative not in record.files:
            record.files.append(relative)
        return relative

    # ── export ─────────────────────────────────────────────────────────────
    def _file_stem(self, suffix: str = "") -> str:
        assert self.plan is not None and self.result is not None
        record = self.result.record
        template = self.procedure.export.filename or "{dut}_{fecha}"
        values = {
            "dut": self.plan.dut.id if self.plan.dut else "DUT",
            "fecha": f"{record.started:%Y%m%d-%H%M%S}",
            "setup": self.plan.setup.id if self.plan.setup else "",
            "procedimiento": self.procedure.id,
            "ensayo": record.id,
        }
        try:
            stem = template.format(**values)
        except (KeyError, IndexError):
            stem = "{dut}_{fecha}".format(**values)
        stem = "".join(c if c.isalnum() or c in "-_." else "_" for c in stem)
        return stem + (f"_{suffix}" if suffix else "")

    def _comments(self, extra: list[str]) -> list[str]:
        assert self.plan is not None and self.result is not None
        record = self.result.record
        calibration = record.calibration
        sweep = record.sweep
        lines = [
            "CAL-ELEKTRA — fichero de resultados",
            f"Ensayo: {record.title} [{record.id}]",
            f"Plantilla de ensayo: {self.procedure.name} (v{self.procedure.version})",
            f"Setup: {record.setup.get('nombre', '—')} [{record.setup.get('plantilla', '—')}]",
            f"EUT: {self.plan.dut.id if self.plan.dut else '—'}",
            f"VNA: {record.instrument.get('identificacion', '—')}",
            f"Calibración: {calibration.get('metodo', '—')}" + (f", kit {calibration['kit']}" if calibration.get("kit") else ""),
            f"Barrido: {SWEEP_MODES.get(sweep.get('modo', ''), sweep.get('modo', ''))} {sweep.get('inicio')} – {sweep.get('fin')}, "
            f"{sweep.get('puntos')} puntos, IFBW {sweep.get('ifbw')}, {sweep.get('potencia_dbm')} dBm, {sweep.get('promedios')} promedios"
            + (" (ajustes por traza: ver ensayo.yaml)" if sweep.get("por_traza") else "")
            + "; trazas sin suavizar",
            f"Fecha: {record.started:%Y-%m-%d %H:%M:%S}",
        ]
        if record.verdict and record.verdict != "SIN_ESPECIFICACIONES":
            lines.append(f"Veredicto frente a especificaciones: {VERDICTS[record.verdict].upper()}")
        if record.simulated:
            lines.insert(1, "DATOS SIMULADOS (VNA simulado): NO es una medida real")
        return lines + extra

    def _step_exportacion(self) -> tuple[str, str]:
        assert self.plan is not None and self.result is not None
        result, record = self.result, self.result.record
        folder = record.folder
        assert folder is not None and result.frequencies is not None
        export = self.procedure.export
        f = result.frequencies
        files: list[Path] = []
        summary: dict = {}
        if self.procedure.algorithm == "vna_2_puertos":
            s = result.data["DUT"]
            uncertainty = result.uncertainty.get("DUT")
            path = folder / f"{self._file_stem()}.s2p"
            write_touchstone(path, Touchstone(2, f, s, comments=self._comments(self._uncertainty_comment(""))), export.data_format, export.frequency_unit)
            files.append(path)
            if export.csv:
                files.append(self._write_csv_2p(folder / f"{self._file_stem()}.csv", f, s, uncertainty))
            if uncertainty:
                files.append(self._write_uncertainty_csv("DUT", path, f, uncertainty))
            summary = self._summary_2p(f, s)
        else:
            dut = self.plan.dut
            network = self.library.field(dut, "red_nominal") if dut is not None else None
            per_line = {}
            for name, s in result.data.items():
                suffix = name if name != "DUT" else ""
                uncertainty = result.uncertainty.get(name)
                path = folder / f"{self._file_stem(suffix)}.s1p"
                comments = ([f"Línea: {name}"] if suffix else []) + self._uncertainty_comment(suffix)
                write_touchstone(path, Touchstone(1, f, s, comments=self._comments(comments)), export.data_format, export.frequency_unit)
                files.append(path)
                if export.csv:
                    files.append(self._write_csv_1p(folder / f"{self._file_stem(suffix)}.csv", f, s[:, 0, 0], network, uncertainty))
                if uncertainty:
                    files.append(self._write_uncertainty_csv(name, path, f, uncertainty))
                per_line[name] = self._summary_1p(f, s[:, 0, 0], network)
            summary = {"lineas": per_line} if len(per_line) > 1 or "DUT" not in per_line else per_line["DUT"]
            if network in LISN_NETWORKS:
                tolerance_z = float(self.procedure.options.get("tolerancia_z_pct", 20.0))
                tolerance_phase = float(self.procedure.options.get("tolerancia_fase_grados", 11.5))
                verdicts = {
                    name: values["desviacion_z_max_pct"] <= tolerance_z and values["desviacion_fase_max_grados"] <= tolerance_phase
                    for name, values in per_line.items()
                }
                summary["red_nominal"] = network
                summary["tolerancias"] = {"z_pct": tolerance_z, "fase_grados": tolerance_phase}
                summary["dentro_de_tolerancia"] = verdicts
        log_path = folder / "registro.txt"
        files.append(log_path)
        record.results = summary
        record.files = [p.name for p in files] + [name for name in record.files if name not in {p.name for p in files}]
        result.files = files
        self._write_log(log_path)
        return "ok", ", ".join(p.name for p in files if p.suffix != ".txt")

    def _uncertainty_comment(self, suffix: str) -> list[str]:
        assert self.result is not None
        if not self.result.uncertainty:
            return []
        k = f"{self.procedure.uncertainty.coverage_factor:g}"
        return [f"Incertidumbre expandida (k={k}) de cada punto: {self._file_stem(suffix)}_incertidumbre.csv"]

    def _uncertainty_columns(self, uncertainty: dict[str, TraceUncertainty] | None, name: str) -> tuple[list[str], list[np.ndarray]]:
        if not uncertainty or name not in uncertainty:
            return [], []
        k = f"{self.procedure.uncertainty.coverage_factor:g}"
        trace = uncertainty[name]
        return [f"U {name} (dB, k={k})", f"U {name} (°, k={k})"], [trace.magnitude_db, trace.phase_deg]

    def _write_uncertainty_csv(self, dataset: str, result_path: Path, f: np.ndarray, uncertainty: dict[str, TraceUncertainty]) -> Path:
        """Expanded uncertainty of every point and trace, next to the result file (the charts read it back)."""
        assert self.result is not None
        path = result_path.with_name(f"{result_path.stem}_incertidumbre.csv")
        header, columns = ["Frecuencia (Hz)"], [f]
        for name in uncertainty:
            names, values = self._uncertainty_columns(uncertainty, name)
            header += names
            columns += values
        self._write_csv(path, header, columns)
        for entry in self.result.record.uncertainty.get("medidas", []):
            if entry.get("medida") == dataset:
                entry["fichero_resultado"] = result_path.name
                entry["fichero"] = path.name
        return path

    def _write_csv_2p(self, path: Path, f: np.ndarray, s: np.ndarray, uncertainty: dict[str, TraceUncertainty] | None = None) -> Path:
        header = ["Frecuencia (Hz)"]
        columns = [f]
        for name, (i, j) in (("S11", (0, 0)), ("S21", (1, 0)), ("S12", (0, 1)), ("S22", (1, 1))):
            header += [f"{name} (dB)", f"{name} (°)"]
            columns += [_db(s[:, i, j]), np.degrees(np.angle(s[:, i, j]))]
            names, values = self._uncertainty_columns(uncertainty, name)
            header += names
            columns += values
        self._write_csv(path, header, columns)
        return path

    def _write_csv_1p(
        self, path: Path, f: np.ndarray, gamma: np.ndarray, network: str | None, uncertainty: dict[str, TraceUncertainty] | None = None
    ) -> Path:
        z = gamma_to_impedance(gamma)
        names, values = self._uncertainty_columns(uncertainty, "S11")
        header = ["Frecuencia (Hz)", "S11 (dB)", "S11 (°)", *names, "|Z| (Ω)", "fase Z (°)"]
        columns = [f, _db(gamma), np.degrees(np.angle(gamma)), *values, np.abs(z), np.degrees(np.angle(z))]
        if network in LISN_NETWORKS:
            nominal = lisn_impedance(network, f)
            header += ["|Z| nominal (Ω)", "fase nominal (°)", "desviación |Z| (%)", "desviación fase (°)"]
            columns += [
                np.abs(nominal),
                np.degrees(np.angle(nominal)),
                100 * (np.abs(z) - np.abs(nominal)) / np.abs(nominal),
                np.degrees(np.angle(z / nominal)),
            ]
        self._write_csv(path, header, columns)
        return path

    @staticmethod
    def _write_csv(path: Path, header: list[str], columns: list[np.ndarray]) -> None:
        rows = [";".join(header)]
        for k in range(len(columns[0])):
            frequency = f"{float(columns[0][k]):.12g}"
            rows.append(";".join([frequency] + [_csv_number(float(column[k])) for column in columns[1:]]))
        path.write_text("\n".join(rows) + "\n", encoding="utf-8-sig")

    @staticmethod
    def _summary_2p(f: np.ndarray, s: np.ndarray) -> dict:
        s21_db = _db(s[:, 1, 0])
        return_loss = -np.maximum(_db(s[:, 0, 0]), _db(s[:, 1, 1]))
        phase = np.unwrap(np.angle(s[:, 1, 0]))
        delay = -np.polyfit(2 * np.pi * f, phase, 1)[0] if len(f) > 2 else float("nan")
        return {
            "perdidas_insercion_db": {"min": round(float(-s21_db.max()), 4), "max": round(float(-s21_db.min()), 4)},
            "perdidas_retorno_min_db": round(float(return_loss.min()), 2),
            "retardo_ns": round(float(delay * 1e9), 4),
            "reciprocidad_max_db": round(float(np.max(np.abs(s21_db - _db(s[:, 0, 1])))), 4),
        }

    @staticmethod
    def _summary_1p(f: np.ndarray, gamma: np.ndarray, network: str | None) -> dict:
        z = gamma_to_impedance(gamma)
        summary = {
            "perdidas_retorno_min_db": round(float(-np.max(_db(gamma))), 2),
            "z_min_ohm": round(float(np.min(np.abs(z))), 3),
            "z_max_ohm": round(float(np.max(np.abs(z))), 3),
        }
        if network in LISN_NETWORKS:
            nominal = lisn_impedance(network, f)
            summary["desviacion_z_max_pct"] = round(float(np.max(np.abs(100 * (np.abs(z) - np.abs(nominal)) / np.abs(nominal)))), 3)
            summary["desviacion_fase_max_grados"] = round(float(np.max(np.abs(np.degrees(np.angle(z / nominal))))), 3)
        return summary

    def _step_cierre(self) -> tuple[str, str]:
        self._close_instrument()
        return "ok", "VNA desconectado; ensayo registrado"

    # ── closing ────────────────────────────────────────────────────────────
    def _close_instrument(self) -> None:
        if self.vna is not None and self.vna.connected:
            try:
                self.vna.disconnect()
            except InstrumentError as exc:
                self._log(f"Error al desconectar el VNA: {exc}", "aviso")

    def _write_log(self, path: Path) -> None:
        lines = ["CAL-ELEKTRA — registro del ensayo", "", "## Ensayo", *self._log_lines, "", "## Instrumento (operaciones de alto nivel)"]
        if self.vna is not None:
            lines += [str(entry) for entry in self.vna.log]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _save_record(self) -> None:
        assert self.result is not None
        record = self.result.record
        folder = record.folder
        if folder is None:
            return
        folder.mkdir(parents=True, exist_ok=True)
        self._write_log(folder / "registro.txt")
        if "registro.txt" not in record.files:
            record.files.append("registro.txt")
        self.library.save_test(record)
