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
  comes from the reference planes of the setup, at the end of the paths of
  the VNA ports (``setups/paths.py``, ``setups/connections.py``);
* the operator is asked to connect each standard and the EUT, naming the
  plane, the equipment at the end of the path and the connector genders;
* algorithms: 2 ports (S → .s2p), transmission with normalization
  (S21 → .csv), impedance in 1 port (S11 → .s1p) and directional coupler
  (one .s2p per port pair, the other ports on 50 Ω loads);
* pass / fail compares the measurement with the S2P specification files of
  the model of the EUT (``procedures/specs.py``) or, for a coupler, with the
  nominal values of its model (``procedures/coupler.py``).

Operator actions are requests to an :class:`Operator` (dialogs in the GUI,
automatic in simulation and tests). When the VNA is simulated the engine also
connects the corresponding device on the simulated bench, so the whole flow
can be exercised without hardware.
"""

from __future__ import annotations

import re
import shutil
import threading
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Callable, Protocol

import numpy as np

from calelektra.catalog.fields import NumberRange
from calelektra.catalog.library import Library, LibraryError, dump_yaml
from calelektra.catalog.model import Equipment
from calelektra.core.connectors import FAMILIES, GENDERS, Connector
from calelektra.core.frequency import FrequencyRange
from calelektra.core.issues import Issue, Severity, has_errors
from calelektra.core.networks import gamma_to_impedance
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
from calelektra.instruments.simulation import pair_model, simulated_dut, unknown_through_model
from calelektra.catalog.model import SpecFile
from calelektra.procedures.coupler import (
    CouplerLimit,
    coupler_limits,
    coupler_quantities,
    coupler_summary,
    coupler_verdict,
    evaluate_coupler,
    nominal_values_record,
    pair_name,
    port_return_losses,
)
from calelektra.procedures.model import ALGORITHMS, COUPLER_PORTS, PLANES_NEEDED, ProcedureTemplate
from calelektra.procedures.record import SETUP_SNAPSHOT, TEMPLATE_SNAPSHOT, StepRecord, TestRecord
from calelektra.procedures.specs import VERDICTS, SpecResult, evaluate_spec, verdict
from calelektra.setups.analysis import SetupAnalysis, analyze_setup
from calelektra.setups.connections import CalibrationDesign, PlaneInfo, check_connections, design_calibration, method_name
from calelektra.setups.frequencies import FrequencyPlan
from calelektra.setups.model import SWEEP_MODES, ConfiguredSetup, SetupTemplate
from calelektra.setups.paths import ExpandedSetup, default_dut_ports, dut_role_of, expand
from calelektra.procedures.record_uncertainty import PENDING_NOTE, compute_uncertainty, is_pending
from calelektra.procedures.results import (
    db as _db,
    uncertainty_csv_path,
    write_coupler_csv,
    write_results_csv_1p,
    write_results_csv_2p,
    write_results_csv_s21,
    write_uncertainty_csv,
)
from calelektra.uncertainty.engine import UncertaintyCalculationError
from calelektra.uncertainty.traces import TraceUncertainty

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


def with_dut(setup: ConfiguredSetup, role: str | None, dut_id: str | None) -> ConfiguredSetup:
    """Copy of the setup with the DUT of the test assigned to its DUT role."""
    if not role or not dut_id:
        return setup
    copy = setup.model_copy(deep=True)
    copy.assignments[role] = dut_id
    copy.set_source_path(setup.source_path)
    return copy


def setup_suits(library: Library, algorithm: str, setup: ConfiguredSetup) -> bool:
    """The template of the setup can be used by the algorithm (``algoritmos``) and has a DUT role."""
    template = library.resolved_template(setup.template)
    return template is not None and algorithm in template.algorithms and dut_role_of(template) is not None


def suitable_setups(library: Library, algorithm: str) -> list[ConfiguredSetup]:
    return sorted((s for s in library.setups.values() if setup_suits(library, algorithm, s)), key=lambda s: s.name)


def eut_choices(library: Library, setup: ConfiguredSetup | None) -> tuple[str | None, str, list[Equipment]]:
    """DUT role of a setup, its label and the equipment of its categories (the possible EUTs).

    The equipment already used by the setup (its paths, the VNA, the kit, the loads…) cannot be the EUT.
    """
    template = library.resolved_template(setup.template) if setup is not None else None
    role_key = dut_role_of(template)
    if setup is None or template is None or role_key is None:
        return None, "EUT (equipo a calibrar)", []
    role = template.roles[role_key]
    used = setup.used_outside(role_key)
    equipment = sorted((e for e in library.equipment_in_categories(role.categories) if e.id not in used), key=lambda e: e.id)
    return role_key, role.display(role_key), equipment


# ── preparation: everything resolved from the template, its setup, its EUT and the equipment fields ──
@dataclass
class PlannedMeasurement:
    """One measurement of the EUT: «DUT», or a port pair of a directional coupler (e.g. «In-Out»)."""

    name: str
    dut_ports: list[str]  # ports of the EUT on P1, P2… then the others (``dut.#1``, ``dut.#2``…)
    expanded: ExpandedSetup | None = None  # the setup with these ports of the EUT

    @property
    def title(self) -> str:
        return self.name.replace("-", " – ")


@dataclass
class RunPlan:
    procedure: ProcedureTemplate
    setup: ConfiguredSetup | None  # the hardware setup of the template, with its EUT assigned
    template: SetupTemplate | None  # template of the setup (paths not expanded)
    analysis: SetupAnalysis | None
    design: CalibrationDesign | None
    vna: Equipment | None
    dut: Equipment | None
    dut_role: str | None
    frequency_plan: FrequencyPlan | None
    specs: list[tuple[SpecFile, Path]]  # specifications of the model of the EUT, with their files
    measurements: list[PlannedMeasurement]
    issues: list[Issue] = field(default_factory=list)
    expanded: ExpandedSetup | None = None  # the setup with its paths as roles (first measurement)
    coupler_limits: dict[str, CouplerLimit] = field(default_factory=dict)  # nominal values of a coupler
    coupler_range: FrequencyRange | None = None  # where they apply: nominal range of the coupler

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
        return (1,) if PLANES_NEEDED[self.procedure.algorithm] == 1 else (1, 2)

    @property
    def method(self) -> str:
        if self.procedure.calibration.method == "existente":
            return "existente"
        return self.design.method if self.design and self.design.method else "—"


def _planned_measurements(
    library: Library, procedure: ProcedureTemplate, dut: Equipment | None, error: Callable[[str, str], None]
) -> list[PlannedMeasurement]:
    """The measurements of the test and the ports of the EUT on each path."""
    ports = default_dut_ports(library, dut)
    if procedure.algorithm == "vna_acoplador":
        planned = []
        for first, second in procedure.measured_pairs():
            if first not in COUPLER_PORTS or second not in COUPLER_PORTS or first == second:
                error(f"Par de puertos «{first}–{second}» no válido: los puertos del acoplador son {', '.join(COUPLER_PORTS)}", "ensayo.pares")
                continue
            others = [port for port in COUPLER_PORTS if port not in (first, second)]
            planned.append(PlannedMeasurement(pair_name(first, second), [first, second, *others]))
        if not planned:
            error("La plantilla de ensayo no tiene pares de puertos que medir", "ensayo.pares")
        return planned
    needed = PLANES_NEEDED[procedure.algorithm]
    chosen = list(procedure.eut_ports) or ports[:needed]
    if dut is not None and ports:
        unknown = [port for port in chosen if port not in ports]
        if unknown:
            error(f"El EUT «{dut.id}» no tiene los puertos {', '.join(unknown)} (puertos: {', '.join(ports)})", "ensayo.puertos_eut")
        elif len(chosen) < needed:
            error(f"La medida necesita {needed} puertos del EUT y «{dut.id}» tiene {len(ports)}", "ensayo.puertos_eut")
    return [PlannedMeasurement("DUT", chosen + [port for port in ports if port not in chosen])]


COUPLER_FIELDS = (
    "acoplamiento_nominal_db",
    "tolerancia_acoplamiento_db",
    "directividad_min_db",
    "perdidas_insercion_max_db",
    "aislamiento_min_db",
    "perdidas_retorno_min_db",
)


def prepare_run(library: Library, procedure: ProcedureTemplate, today: date | None = None) -> RunPlan:
    """Check that the test template (its hardware setup and its EUT) can be launched and resolve what it needs."""
    issues: list[Issue] = []

    def error(message: str, code: str = "ensayo") -> None:
        issues.append(Issue(Severity.ERROR, code, message))

    def warning(message: str, code: str = "ensayo") -> None:
        issues.append(Issue(Severity.WARNING, code, message))

    algorithm = procedure.algorithm
    setup = library.setups.get(procedure.setup_id) if procedure.setup_id else None
    if setup is None:
        error(
            f"El hardware setup «{procedure.setup_id}» de la plantilla no existe"
            if procedure.setup_id
            else "Elija el hardware setup de la plantilla de ensayo",
            "ensayo.setup",
        )
        return RunPlan(procedure, None, None, None, None, None, None, None, None, [], [], issues)
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
    if template is not None and algorithm not in template.algorithms:
        admitted = ", ".join(ALGORITHMS.get(a, a) for a in template.algorithms)
        error(
            f"El hardware setup «{setup.name}» (plantilla «{template.name}») no sirve para «{ALGORITHMS[algorithm]}»"
            + (f"; sirve para: {admitted}" if admitted else "; no tiene todavía algoritmo de ensayo"),
            "ensayo.setup",
        )
    effective = with_dut(setup, dut_role, eut_id)
    dut = library.equipment.get(eut_id) if eut_id else None
    measurements = _planned_measurements(library, procedure, dut, error)
    first_ports = measurements[0].dut_ports if measurements else None
    analysis = analyze_setup(effective, library, today, dut_ports=first_ports) if template is not None else None
    expanded = analysis.expanded if analysis is not None else None
    if analysis is not None:
        issues.extend(i for i in analysis.issues if i.severity is not Severity.INFO)
    else:
        error(f"La plantilla «{setup.template}» del setup no existe")
    vna_ids = effective.ids_for("vna")
    vna = library.equipment.get(vna_ids[0]) if vna_ids else None
    if vna is None:
        error("El setup no tiene VNA asignado")

    # every port pair of a coupler changes the connections of the EUT: check them all
    if template is not None:
        seen = {issue.message for issue in issues}
        for index, measurement in enumerate(measurements):
            measurement.expanded = expanded if index == 0 else expand(template, effective, library, measurement.dut_ports)
            if index == 0 or measurement.expanded is None:
                continue
            for check in check_connections(measurement.expanded.template, measurement.expanded.setup, library):
                if check.state != "medida" or check.severity not in (Severity.ERROR, Severity.WARNING):
                    continue
                if dut_role not in (check.a.endpoint.role, check.b.endpoint.role):
                    continue
                message = f"Par {measurement.title}: {check.a.text()} ↔ {check.b.text()}: {check.message}"
                if message not in seen:
                    seen.add(message)
                    issues.append(Issue(check.severity, "conexion.conectores", message))

    design = None
    method = procedure.calibration.method
    if expanded is not None:
        override = None if method in ("automatico", "existente") else method
        design = design_calibration(expanded.template, expanded.setup, library, method_override=override)
        if override:
            issues.extend(Issue(s, "calibracion.vna", m) for s, m in design.checks if s in (Severity.ERROR, Severity.WARNING))
        planes_needed = PLANES_NEEDED[algorithm]
        if len(design.planes) != planes_needed:
            error(
                f"El algoritmo «{ALGORITHMS[algorithm]}» necesita {planes_needed} camino(s) con su plano de referencia "
                f"y la plantilla «{template.name}» del setup define {len(design.planes)}"
            )
        elif method != "existente" and design.method is None:
            error(f"La plantilla «{template.name}» no calibra los puertos del VNA: {design.reason}")
        if algorithm in ("vna_2_puertos", "vna_acoplador") and method in ("OSM", "normalizacion"):
            error(f"«{method}» no corrige los 4 parámetros S de 2 puertos: use TOSM, UOSM o automático")
        if algorithm == "vna_1_puerto" and method in ("TOSM", "UOSM", "normalizacion"):
            error(f"{method} calibra 2 puertos: use OSM para la medida de 1 puerto")
        if algorithm == "vna_transmision" and method in ("OSM", "TOSM", "UOSM"):
            error(f"El ensayo de transmisión se calibra con normalización (through entre los planos), no con {method}")

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
            if algorithm == "vna_transmision":
                warning(
                    "Incertidumbre con normalización de transmisión: el fichero del VNA da la de una calibración completa de 2 puertos",
                    "ensayo.incertidumbre",
                )

    # pass / fail: S2P specifications of the model of the EUT, or the nominal values of a coupler
    specs: list[tuple[SpecFile, Path]] = []
    limits: dict[str, CouplerLimit] = {}
    nominal_range = None
    if dut is not None and algorithm == "vna_acoplador":
        limits = coupler_limits({key: library.field(dut, key) for key in COUPLER_FIELDS})
        nominal_range = library.frequency_range_of(dut).range
        if not limits:
            warning(f"El modelo de «{dut.id}» no tiene valores nominales de acoplador: el ensayo no dará veredicto", "ensayo.especificacion")
    elif dut is not None:
        for spec, path in library.specifications_of(dut):
            try:
                data = read_touchstone(path)
            except TouchstoneError as exc:
                error(f"Especificación «{spec.name}» del modelo de «{dut.id}»: {exc}", "ensayo.especificacion")
                continue
            measured = [p for p in spec.parameters if p in procedure.traces]
            missing = [p for p in spec.parameters if p not in procedure.traces]
            beyond = [p for p in spec.parameters if int(p[1]) > data.n_ports or int(p[2]) > data.n_ports]
            if beyond:
                error(f"Especificación «{spec.name}»: {', '.join(beyond)} no está en el fichero", "ensayo.especificacion")
                continue
            if missing and algorithm == "vna_transmision":
                if not measured:
                    warning(f"Especificación «{spec.name}»: no se evalúa (el ensayo de transmisión solo mide S21)", "ensayo.especificacion")
                    continue
                warning(f"Especificación «{spec.name}»: solo se evalúa {', '.join(measured)} (el ensayo de transmisión solo mide S21)", "ensayo.especificacion")
                spec = spec.model_copy(update={"parameters": measured})
            elif missing:
                error(f"Especificación «{spec.name}»: {', '.join(missing)} no lo mide la plantilla", "ensayo.especificacion")
            if frequency_plan is not None:
                overlap = data.frequency_range.intersect(frequency_plan.range)
                if overlap is None:
                    warning(f"Especificación «{spec.name}» ({data.frequency_range}) fuera de las frecuencias del setup: no se evaluará", "ensayo.especificacion")
                elif not data.frequency_range.contains(frequency_plan.range):
                    warning(f"Especificación «{spec.name}»: solo se evalúa en {overlap}", "ensayo.especificacion")
            specs.append((spec, path))
    return RunPlan(
        procedure, effective, template, analysis, design, vna, dut, dut_role, frequency_plan, specs, measurements, issues, expanded, limits,
        nominal_range,
    )


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
        record.conditions = {"estado": "pendiente"}  # until the workbook of the TPH probe is loaded
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
            if is_pending(record):
                record.message += PENDING_NOTE
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
        assert plan.setup is not None and plan.expanded is not None
        setup = self.library.setups[plan.setup.id]
        record.setup = {
            "id": setup.id,
            "nombre": setup.name,
            "plantilla": setup.template,
            "rango_requerido": setup.required_range.to_yaml(),
            "frecuencias": setup.frequencies.model_dump(by_alias=True, exclude_none=True, mode="json"),
            "caminos": {key: [item.to_yaml() for item in items] for key, items in setup.paths.items()},
            "conectores_dut_admitidos": plan.expanded.dut_connectors_text(),
        }
        record.paths = [path.to_record(self.library) for path in plan.expanded.paths]
        record.equipment = [self._equipment_entry(role, eq_id) for role, eq_id in self._assigned()]
        if plan.dut is not None:
            record.dut = self._equipment_entry(plan.dut_role or "dut", plan.dut.id)
            record.dut["puertos_medidos"] = {m.name: m.dut_ports[: len(plan.expanded.paths)] for m in plan.measurements}
        if plan.frequency_plan is not None:
            record.frequency_range = [format_frequency(plan.frequency_plan.start, decimal="."), format_frequency(plan.frequency_plan.stop, decimal=".")]
        self._write_snapshots()
        detail = "Plantilla, hardware setup y EUT compatibles"
        if warnings:
            detail += f" ({len(warnings)} avisos)"
        return ("aviso" if warnings else "ok"), detail

    def _write_snapshots(self) -> None:
        """The test template and the hardware setup as launched, as YAML files of the test."""
        assert self.plan is not None and self.plan.setup is not None and self.result is not None
        record = self.result.record
        if record.folder is None:
            return
        setup = {
            "formato": "calelektra/hardware_setup_ensayo@0.4",
            "ensayo": record.id,
            "setup": self.plan.setup.to_yaml_dict(),  # with the EUT of the template in its role
            "plantilla_setup": self.plan.template.model_dump(by_alias=True, exclude_none=True, mode="json") if self.plan.template else None,
            "caminos": record.paths,  # each path resolved: equipment in order, orientation, connectors, plane
            "conectores_dut_admitidos": record.setup.get("conectores_dut_admitidos"),
            "equipos": record.equipment,
        }
        for name, data, title in (
            (SETUP_SNAPSHOT, setup, "Hardware setup del ensayo, tal como se lanzó"),
            (TEMPLATE_SNAPSHOT, self.procedure.to_yaml_dict(), "Plantilla de ensayo, tal como se lanzó"),
        ):
            dump_yaml(data, record.folder / name, header=f"{title} — ensayo {record.id}")
            if name not in record.files:
                record.files.append(name)

    def _assigned(self) -> list[tuple[str, str]]:
        """Roles and equipment of the setup, the equipment of the paths included (in their order)."""
        assert self.plan is not None and self.plan.expanded is not None
        expanded = self.plan.expanded
        pairs = []
        for role in expanded.template.roles:
            for equipment_id in expanded.setup.ids_for(role):
                pairs.append((role, equipment_id))
        return pairs

    def _equipment_entry(self, role: str, equipment_id: str) -> dict:
        eq = self.library.equipment[equipment_id]
        model = self.library.model_of(eq)
        entry: dict = {"rol": role, "id": eq.id, "modelo": model.display_name if model else eq.model_id}
        expanded = self.plan.expanded if self.plan is not None else None
        if expanded is not None and role in expanded.template.roles:
            entry["rol_nombre"] = expanded.template.roles[role].display(role)
            path = expanded.path_of_role(role)
            if path is not None:
                entry["camino"] = path.key
                entry["posicion"] = next(e.index for e in path.elements if e.role == role)
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
        if method == "normalizacion":
            kit_text = "through"
            self.vna.begin_calibration(method, ports, design.connectors)
        else:
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
            elif standard == "THROUGH" and method == "normalizacion":
                a, b = planes[standard_ports[0]], planes[standard_ports[1]]
                title, message, simulate = self._normalization_through(a, b, standard_ports)
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
            "kit": kit.id if kit and method != "normalizacion" else None,
            "kit_en_vna": design.kit_name if method != "normalizacion" else None,
            "certificado_kit": kit.calibration.certificate if kit and kit.calibration and method != "normalizacion" else None,
            "through": design.through.id if design.through else None,
            "estado_vna": status,
            "fecha": datetime.now().isoformat(timespec="seconds"),
        }
        record.calibration = {key: value for key, value in record.calibration.items() if value is not None or key == "kit"}
        if self.procedure.calibration.save_on_vna:
            name = self.procedure.calibration.vna_cal_name.format(ensayo=record.id, setup=self.plan.setup.id if self.plan.setup else "")
            self.vna.save_calibration(name)
            record.calibration["guardada_en_vna"] = name
        return "ok", f"{method_name(method)} en {', '.join(f'P{p}' for p in ports)} ({CALIBRATION_STATUS[status]})"

    def _normalization_through(self, a: PlaneInfo, b: PlaneInfo, ports: tuple[int, ...]) -> tuple[str, str, Callable[[DummyVna], None]]:
        """Request and simulation of the through of a transmission normalization."""
        design = self.plan.design if self.plan else None
        through = design.through if design else None
        if through is None:
            title = "Normalización de transmisión: through plano"
            message = f"Una directamente los planos {self._plane_text(a)} y {self._plane_text(b)} (through plano, sin adaptador)."
            return title, message, lambda vna: vna.connect_through(ports)
        title = f"Normalización de transmisión: through «{through.id}»"
        message = (
            f"Conecte el through de normalización «{through.id}» entre {self._plane_text(a)} y {self._plane_text(b)}. "
            "Sus pérdidas quedan dentro de la referencia."
        )
        n_ports, model = simulated_dut(self.library, through)
        if n_ports == 2:
            return title, message, lambda vna: vna.connect_network(model, ports, through.id)
        return title, message, lambda vna: vna.connect_through(ports)

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

    def _measure_dut(self, ports: tuple[int, ...], traces: tuple[str, ...] | None = None) -> tuple[np.ndarray, np.ndarray]:
        """Measure the traces of the DUT with their own IFBW / averaging (not smoothed: smoothing is only for display).

        Traces that share IFBW and averaging are read from the same sweep; each
        other combination needs its own sweep (on the VNA they are channel
        settings). The «all traces» settings are restored afterwards. Only
        ``traces`` are read (e.g. S21 in the transmission test); the other
        elements of the S matrix are NaN.
        """
        assert self.vna is not None
        sweep = self.procedure.sweep
        n = len(ports)
        names = [f"S{i + 1}{j + 1}" for i in range(n) for j in range(n)]
        if traces:
            names = [name for name in names if name in traces]
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
                s = np.full((len(frequencies), n, n), np.nan, dtype=complex)
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
        if method == "normalizacion":
            a, b = planes[ports[0]], planes[ports[1]]
            title, message, simulate = self._normalization_through(a, b, ports)
            self._ask(f"Verificación: {title[0].lower()}{title[1:]}", f"Vuelva a conectar: {message[0].lower()}{message[1:]}", image="through", simulate=simulate, step="verificacion")
            f, s = self._measure(ports)
            self.listener.data("Verificación (through)", f, s)
            deviation = float(np.max(np.abs(_db(s[:, 1, 0]))))
            checks.append({"comprobacion": "|S21| del through normalizado", "valor_db": round(deviation, 4), "limite_db": settings.s21_tolerance_db, "correcto": deviation <= settings.s21_tolerance_db})
        elif method == "TOSM":
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
        for port in ports if method != "normalizacion" else ():
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
        ports = self.plan.ports
        traces = self.procedure.traces
        n_ports, model = simulated_dut(self.library, dut)
        category_ports = [port.name for port in self.library.ports_of(dut)]
        total = len(self.plan.measurements)
        for index, measurement in enumerate(self.plan.measurements):
            self._check_stop()
            if total > 1:
                self.listener.progress(index / total, f"Par {measurement.title}")
            title = "Conectar el DUT" if total == 1 else f"Par {measurement.title} ({index + 1}/{total})"
            simulate = self._simulated_connection(model, n_ports, category_ports, measurement, ports)
            self._ask(title, self._connection_text(measurement), image="dut", simulate=simulate, step="medida")
            f, s = self._measure_dut(ports, traces)
            result.frequencies, result.data[measurement.name] = f, s
            self.listener.data(measurement.name, f, s)
            if total > 1:
                self._log(f"Medida {measurement.title}: {len(f)} puntos")
        text = f"{len(result.frequencies)} puntos, {' '.join(traces)}" if result.frequencies is not None else "sin datos"
        if total > 1:
            text = f"{total} pares de puertos, " + text
        return "ok", f"{text} ({CALIBRATION_STATUS[status]})"

    def _connection_text(self, measurement: PlannedMeasurement) -> str:
        """Where each port of the EUT goes: the plane at the end of a path, or a load of the setup."""
        assert self.plan is not None and self.plan.dut is not None
        dut = self.plan.dut
        expanded = measurement.expanded
        if expanded is None:
            return f"Conecte el DUT «{dut.id}»."
        planes = {plane.name: plane for plane in self.plan.design.planes} if self.plan.design else {}
        ends = {path.end_endpoint: path for path in expanded.paths}
        state = expanded.template.connection_states.get("medida")
        parts = []
        for connection in state.connections if state else []:
            for own, other in ((connection.a, connection.b), (connection.b, connection.a)):
                role, _, port = own.partition(".")
                if role != self.plan.dut_role or not port:
                    continue
                connector = self.library.port_connector(dut, port)
                port_text = port + (f" ({connector.label()})" if connector else "")
                if other in ends:
                    plane = planes.get(ends[other].plane)
                    parts.append(f"{port_text} en {self._plane_text(plane) if plane else ends[other].plane}")
                else:
                    other_role = other.partition(".")[0]
                    ids = expanded.setup.ids_for(other_role)
                    role_def = expanded.template.roles.get(other_role)
                    label = role_def.display(other_role) if role_def else other_role
                    parts.append(f"{port_text} en «{ids[0] if ids else other_role}» ({label})")
        return f"Conecte el DUT «{dut.id}»: " + "; ".join(parts) + "." if parts else f"Conecte el DUT «{dut.id}»."

    def _simulated_connection(
        self, model: Callable[[np.ndarray], np.ndarray], n_ports: int, category_ports: list[str], measurement: PlannedMeasurement, ports: tuple[int, ...]
    ) -> Callable[[DummyVna], None]:
        """What the simulated bench sees with the ports of the EUT of this measurement."""
        assert self.plan is not None and self.plan.dut is not None
        label = self.plan.dut.id + ("" if measurement.name == "DUT" else f" {measurement.name}")
        indexes = [category_ports.index(p) for p in measurement.dut_ports[: len(ports)] if p in category_ports]
        if n_ports == 4 and len(indexes) == 2:
            return lambda vna: vna.connect_network(pair_model(model, indexes[0], indexes[1]), ports, label)
        if len(ports) == 1:
            if n_ports == 1:
                return lambda vna: vna.connect_one_port(model, ports[0], label)
            first = indexes[0] if indexes else 0
            return lambda vna: vna.connect_one_port(lambda f: model(f)[:, first, first], ports[0], label)
        if n_ports == 2 and indexes == [1, 0]:
            return lambda vna: vna.connect_network(lambda f: model(f)[:, ::-1, ::-1], ports, label)
        return lambda vna: vna.connect_network(model, ports, label)

    def _step_incertidumbre(self) -> tuple[str, str]:
        assert self.plan is not None and self.result is not None
        result, record = self.result, self.result.record
        vna = self.plan.vna
        if not self.procedure.uncertainty.active:
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
        path = self.library.uncertainty_path(vna)
        assert path is not None

        def measured(name: str, traces: dict[str, TraceUncertainty]) -> None:
            self._check_stop()
            result.uncertainty[name] = traces
            self.listener.uncertainty(name, result.frequencies, traces)

        run = compute_uncertainty(
            engine,
            designation,
            result.frequencies,
            result.data,
            self.procedure,
            record.conditions,
            self.today or date.today(),
            measured,
            calibration_method=record.calibration.get("metodo"),
        )
        serial = f" nº {engine.serial_number}" if engine.serial_number else ""
        record.uncertainty = {
            "fichero": self._keep_file(path, "incertidumbres"),
            "vna": f"{vna.id} — {designation}{serial}",
            "equipo_vna": vna.id,
            **run.entry,
        }
        for problem in run.problems:
            self._log(f"Incertidumbre: {problem}", "aviso")
        if is_pending(record):
            self._log("Incertidumbre provisional (sin la componente de temperatura): cargue las condiciones ambientales del ensayo", "aviso")
        return run.status, run.detail

    def _step_evaluacion(self) -> tuple[str, str]:
        assert self.result is not None
        assert self.plan is not None
        result, record = self.result, self.result.record
        if self.procedure.algorithm == "vna_acoplador":
            return self._evaluate_coupler()
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

    def _evaluate_coupler(self) -> tuple[str, str]:
        """Pass / fail of a directional coupler against the nominal values of its model."""
        assert self.plan is not None and self.result is not None and self.plan.dut is not None
        result, record = self.result, self.result.record
        assert result.frequencies is not None
        limits = self.plan.coupler_limits
        if not limits:
            record.verdict = "SIN_ESPECIFICACIONES"
            return "omitido", f"El modelo de «{self.plan.dut.id}» no tiene valores nominales de acoplador"
        record.nominal_values = nominal_values_record(limits, self.plan.coupler_range)  # also drawn in the charts
        quantities = coupler_quantities(result.data)
        entries = evaluate_coupler(result.frequencies, quantities, limits, self.plan.coupler_range)
        record.spec_results = entries
        record.verdict = coupler_verdict(entries)
        for entry in entries:
            self._log(f"{entry['nombre']}: {entry['resultado']}", "info" if entry["resultado"] == "PASA" else "aviso")
        detail = "; ".join(f"{entry['nombre']}: {VERDICTS[entry['resultado']].lower()}" for entry in entries)
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
        suffix = re.sub(r"[^\w-]+", "_", suffix).strip("_")  # «In-Coupl. Fwd» → «In-Coupl_Fwd»
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
            *(f"Camino {path.key}: {path.describe()}" for path in (self.plan.expanded.paths if self.plan.expanded else [])),
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
        algorithm = self.procedure.algorithm
        f = result.frequencies
        files: list[Path] = []
        measurements: list[dict] = []
        summary: dict = {}
        if algorithm in ("vna_2_puertos", "vna_acoplador"):
            for name, s in result.data.items():
                suffix = "" if name == "DUT" else name
                uncertainty = result.uncertainty.get(name)
                path = folder / f"{self._file_stem(suffix)}.s2p"
                comments = [f"Par de puertos del acoplador: {name.replace('-', ' en P1, ')} en P2; los otros dos en 50 Ω"] if suffix else []
                comments += self._uncertainty_comment(path)
                write_touchstone(path, Touchstone(2, f, s, comments=self._comments(comments)), export.data_format, export.frequency_unit)
                files.append(path)
                if export.csv:
                    files.append(write_results_csv_2p(path.with_suffix(".csv"), f, s, uncertainty, self._k))
                if uncertainty:
                    files.append(self._write_uncertainty(name, path, f, uncertainty))
                measurements.append(self._measurement_entry(name, path, "touchstone"))
            if algorithm == "vna_2_puertos":
                summary = self._summary_2p(f, result.data["DUT"])
            else:
                quantities = coupler_quantities(result.data)
                files.append(write_coupler_csv(folder / f"{self._file_stem('acoplador')}.csv", f, quantities, port_return_losses(result.data)))
                summary = coupler_summary(quantities)
                summary["pares"] = [m.title for m in self.plan.measurements]
        elif algorithm == "vna_transmision":
            s = result.data["DUT"]
            uncertainty = result.uncertainty.get("DUT")
            path = folder / f"{self._file_stem()}.csv"  # the result of the test: S21 and its uncertainty
            files.append(write_results_csv_s21(path, f, s[:, 1, 0], uncertainty, self._k))
            if uncertainty:
                files.append(self._write_uncertainty("DUT", path, f, uncertainty))
            measurements.append(self._measurement_entry("DUT", path, "csv_s21"))
            summary = self._summary_s21(f, s[:, 1, 0])
        else:
            s = result.data["DUT"]
            uncertainty = result.uncertainty.get("DUT")
            path = folder / f"{self._file_stem()}.s1p"
            port = self.plan.measurements[0].dut_ports[0] if self.plan.measurements and self.plan.measurements[0].dut_ports else None
            comments = ([f"Puerto del EUT: {port}"] if port else []) + self._uncertainty_comment(path)
            write_touchstone(path, Touchstone(1, f, s, comments=self._comments(comments)), export.data_format, export.frequency_unit)
            files.append(path)
            if export.csv:
                files.append(write_results_csv_1p(path.with_suffix(".csv"), f, s[:, 0, 0], uncertainty, self._k))
            if uncertainty:
                files.append(self._write_uncertainty("DUT", path, f, uncertainty))
            measurements.append(self._measurement_entry("DUT", path, "touchstone"))
            summary = self._summary_1p(f, s[:, 0, 0])
        log_path = folder / "registro.txt"
        files.append(log_path)
        record.results = summary
        record.measurements = measurements
        record.files = [p.name for p in files] + [name for name in record.files if name not in {p.name for p in files}]
        result.files = files
        self._write_log(log_path)
        return "ok", ", ".join(p.name for p in files if p.suffix != ".txt")

    def _measurement_entry(self, name: str, path: Path, data_format: str) -> dict:
        """Measurement of the record: its file and the ports of the EUT on each path."""
        assert self.plan is not None
        entry: dict = {"nombre": name, "fichero": path.name, "formato": data_format}
        planned = next((m for m in self.plan.measurements if m.name == name), None)
        paths = self.plan.expanded.paths if self.plan.expanded else []
        if planned is not None:
            entry["puertos_eut"] = {path.key: port for path, port in zip(paths, planned.dut_ports)}
            others = planned.dut_ports[len(paths) :]
            if self.procedure.algorithm == "vna_acoplador" and others:
                entry["puertos_en_carga"] = others
        return entry

    @property
    def _k(self) -> float:
        return self.procedure.uncertainty.coverage_factor

    def _uncertainty_comment(self, result_path: Path) -> list[str]:
        assert self.result is not None
        if not self.result.uncertainty:
            return []
        return [f"Incertidumbre expandida (k={self._k:g}) de cada punto: {uncertainty_csv_path(result_path).name}"]

    def _write_uncertainty(self, dataset: str, result_path: Path, f: np.ndarray, uncertainty: dict[str, TraceUncertainty]) -> Path:
        """Uncertainty CSV of a measurement; the record keeps both file names."""
        assert self.result is not None
        path = write_uncertainty_csv(uncertainty_csv_path(result_path), f, uncertainty, self._k)
        for entry in self.result.record.uncertainty.get("medidas", []):
            if entry.get("medida") == dataset:
                entry["fichero_resultado"] = result_path.name
                entry["fichero"] = path.name
        return path

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
    def _summary_s21(f: np.ndarray, s21: np.ndarray) -> dict:
        s21_db = _db(s21)
        phase = np.unwrap(np.angle(s21))
        delay = -np.polyfit(2 * np.pi * f, phase, 1)[0] if len(f) > 2 else float("nan")
        return {
            "perdidas_insercion_db": {"min": round(float(-s21_db.max()), 4), "max": round(float(-s21_db.min()), 4)},
            "retardo_ns": round(float(delay * 1e9), 4),
        }

    @staticmethod
    def _summary_1p(f: np.ndarray, gamma: np.ndarray) -> dict:
        z = gamma_to_impedance(gamma)
        return {
            "perdidas_retorno_min_db": round(float(-np.max(_db(gamma))), 2),
            "z_min_ohm": round(float(np.min(np.abs(z))), 3),
            "z_max_ohm": round(float(np.max(np.abs(z))), 3),
        }

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
