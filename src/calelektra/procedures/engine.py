"""Execution of a measurement procedure on a configured hardware setup (the «test» of ELEKTRA).

A run goes through these steps, using the fields of the assigned equipment:

    validación → conexión → configuración → calibración → verificación →
    medida → exportación → cierre

* the limits of the VNA (range, points, IFBW, power, calibration methods) and
  its connectors come from its fields;
* the calibration (method, ports, connectors, cal kit and its name in the VNA)
  comes from the reference planes of the setup (``setups/connections.py``);
* the operator is asked to connect each standard and the DUT, naming the
  plane, the equipment and the connector genders;
* the DUT fields drive the measurement (LISN lines and nominal network) and,
  with a simulated VNA, the simulated device itself.

Operator actions are requests to an :class:`Operator` (dialogs in the GUI,
automatic in simulation and tests). When the VNA is simulated the engine also
connects the corresponding device on the simulated bench, so the whole flow
can be exercised without hardware.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Callable, Protocol

import numpy as np

from calelektra.catalog.library import Library, dump_yaml
from calelektra.catalog.model import Equipment
from calelektra.core.connectors import FAMILIES, GENDERS, Connector
from calelektra.core.frequency import FrequencyRange
from calelektra.core.issues import Issue, Severity, has_errors
from calelektra.core.networks import LISN_NETWORKS, gamma_to_impedance, lisn_impedance
from calelektra.core.touchstone import Touchstone, write_touchstone
from calelektra.core.units import format_frequency
from calelektra.catalog.fields import NumberRange
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
from calelektra.procedures.model import ALGORITHMS, ProcedureTemplate
from calelektra.procedures.record import RECORD_FILE, StepRecord, TestRecord
from calelektra.setups.analysis import SetupAnalysis, analyze_setup
from calelektra.setups.connections import CalibrationDesign, PlaneInfo, design_calibration
from calelektra.setups.model import ConfiguredSetup, SetupTemplate

STEPS: dict[str, str] = {
    "validacion": "Validación del setup y del procedimiento",
    "conexion": "Conexión con el VNA",
    "configuracion": "Configuración del barrido",
    "calibracion": "Calibración de los puertos",
    "verificacion": "Verificación de la calibración",
    "medida": "Medida del DUT",
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

    def data(self, name: str, frequencies: np.ndarray, s: np.ndarray) -> None:
        """New measured data: ``s`` has shape ``(points, n, n)``."""

    def progress(self, fraction: float, text: str = "") -> None: ...


# ── preparation: everything resolved from procedure, setup and equipment fields ──
@dataclass
class RunPlan:
    procedure: ProcedureTemplate
    setup: ConfiguredSetup
    template: SetupTemplate | None
    analysis: SetupAnalysis | None
    design: CalibrationDesign | None
    vna: Equipment | None
    dut: Equipment | None
    frequency_range: FrequencyRange
    lines: list[str]
    issues: list[Issue] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not has_errors(self.issues)

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


def _template_chain(library: Library, template_id: str) -> list[str]:
    chain = [template_id]
    while True:
        template = library.templates.get(chain[-1])
        if template is None or not template.extends or template.extends in chain:
            return chain
        chain.append(template.extends)


def prepare_run(library: Library, procedure: ProcedureTemplate, setup: ConfiguredSetup, today: date | None = None) -> RunPlan:
    """Check that the procedure can run on the setup and resolve what it needs."""
    issues: list[Issue] = []

    def error(message: str, code: str = "ensayo") -> None:
        issues.append(Issue(Severity.ERROR, code, message))

    def warning(message: str, code: str = "ensayo") -> None:
        issues.append(Issue(Severity.WARNING, code, message))

    template = library.resolved_template(setup.template)
    analysis = analyze_setup(setup, library, today) if template is not None else None
    if analysis is not None:
        issues.extend(i for i in analysis.issues if i.severity is not Severity.INFO)
    else:
        error(f"La plantilla «{setup.template}» del setup no existe")
    chain = _template_chain(library, setup.template)
    if procedure.compatible_templates and not set(chain) & set(procedure.compatible_templates):
        error(
            f"El procedimiento «{procedure.name}» es para las plantillas {', '.join(procedure.compatible_templates)}; "
            f"el setup usa «{setup.template}»"
        )
    vna_ids = setup.ids_for("vna")
    vna = library.equipment.get(vna_ids[0]) if vna_ids else None
    dut_ids = setup.ids_for("dut")
    dut = library.equipment.get(dut_ids[0]) if dut_ids else None
    if vna is None:
        error("El setup no tiene VNA asignado")
    if dut is None:
        error("El setup no tiene DUT asignado")

    design = None
    method = procedure.calibration.method
    if template is not None:
        override = None if method in ("automatico", "existente") else method
        design = design_calibration(template, setup, library, method_override=override)
        if override:
            issues.extend(Issue(s, "calibracion.vna", m) for s, m in design.checks if s in (Severity.ERROR, Severity.WARNING))
        planes_needed = 2 if procedure.algorithm == "vna_2_puertos" else 1
        if len(design.planes) != planes_needed:
            error(
                f"El algoritmo «{ALGORITHMS[procedure.algorithm]}» necesita {planes_needed} plano(s) de referencia "
                f"y la plantilla «{template.name}» define {len(design.planes)}"
            )
        elif method != "existente" and design.method is None:
            error(f"La plantilla «{template.name}» no calibra los puertos del VNA: {design.reason}")
        if procedure.algorithm == "vna_2_puertos" and method == "OSM":
            error("OSM es una calibración de 1 puerto: no sirve para medir 2 puertos")
        if procedure.algorithm == "vna_1_puerto" and method in ("TOSM", "UOSM"):
            error(f"{method} calibra 2 puertos: use OSM para la medida de 1 puerto")

    sweep = procedure.sweep
    frequency_range = setup.required_range
    if vna is not None:
        max_points = library.field(vna, "puntos_max")
        if max_points and sweep.points > int(max_points):
            error(f"El procedimiento pide {sweep.points} puntos y «{vna.id}» admite {max_points} (campo «Nº máximo de puntos»)")
        ifbw = library.field(vna, "ifbw")
        if isinstance(ifbw, FrequencyRange) and not ifbw.start <= sweep.if_bandwidth <= ifbw.stop:
            error(f"IFBW de {format_frequency(sweep.if_bandwidth)} fuera del rango de «{vna.id}» ({ifbw})")
        power = library.field(vna, "potencia_salida_dbm")
        if isinstance(power, NumberRange) and not power.low <= sweep.power_dbm <= power.high:
            error(f"Potencia de {sweep.power_dbm:g} dBm fuera del rango de «{vna.id}» ({power} dBm)")
    if sweep.sweep_type == "logaritmico" and frequency_range.start <= 0:
        error("El barrido logarítmico no puede empezar en DC: ajuste el rango requerido del setup")

    lines = ["—"]
    if dut is not None and library.category_id_of(dut) == "lisn":
        lines = list(library.field(dut, "lineas", []) or [])
        wanted = procedure.options.get("lineas", "todas")
        if isinstance(wanted, (list, tuple)):
            unknown = [line for line in wanted if line not in lines]
            if unknown:
                error(f"La LISN «{dut.id}» no tiene las líneas {', '.join(unknown)} (campo «Líneas»: {', '.join(lines)})")
            lines = [line for line in lines if line in wanted]
        if not lines:
            error(f"La LISN «{dut.id}» no indica sus líneas (campo «Líneas»)")
        network = library.field(dut, "red_nominal")
        if network not in LISN_NETWORKS:
            warning(f"La red nominal de «{dut.id}» ({network}) no es conocida: no se calcularán desviaciones")
    return RunPlan(procedure, setup, template, analysis, design, vna, dut, frequency_range, lines, issues)


@dataclass
class RunResult:
    record: TestRecord
    frequencies: np.ndarray | None = None
    data: dict[str, np.ndarray] = field(default_factory=dict)  # name → complex (points, n, n)
    files: list[Path] = field(default_factory=list)

    @property
    def status(self) -> str:
        return self.record.status


def _db(values: np.ndarray) -> np.ndarray:
    with np.errstate(divide="ignore"):
        return 20 * np.log10(np.maximum(np.abs(values), 1e-15))


def _csv_number(value: float) -> str:
    return f"{value:.7g}".replace(".", ",")


class ProcedureRunner:
    """Runs a procedure on a configured setup. ``run()`` blocks; ``stop()`` may be called from another thread."""

    def __init__(
        self,
        library: Library,
        procedure: ProcedureTemplate,
        setup: ConfiguredSetup,
        operator: Operator | None = None,
        listener: RunListener | None = None,
        operator_name: str | None = None,
        today: date | None = None,
        seed: int | None = None,
    ):
        self.library = library
        self.procedure = procedure
        self.setup = setup
        self.operator = operator or AutoOperator()
        self.listener = listener or RunListener()
        self.operator_name = operator_name
        self.today = today
        self.seed = seed
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
        started = datetime.now()
        base = f"{started:%Y%m%d-%H%M%S}_{self.setup.id}"
        test_id, counter = base, 2
        while (self.library.tests_folder / test_id).exists():
            test_id, counter = f"{base}_{counter}", counter + 1
        record = TestRecord(
            id=test_id,
            started=started,
            operator=self.operator_name,
            procedure={"id": self.procedure.id, "nombre": self.procedure.name, "version": self.procedure.version, "algoritmo": self.procedure.algorithm},
            setup={"id": self.setup.id, "nombre": self.setup.name, "plantilla": self.setup.template},
            frequency_range=[format_frequency(self.setup.required_range.start, decimal="."), format_frequency(self.setup.required_range.stop, decimal=".")],
        )
        folder = self.library.tests_folder / test_id
        folder.mkdir(parents=True, exist_ok=True)
        record.set_folder(folder)
        return record

    def run(self) -> RunResult:
        record = self._new_record()
        self.result = RunResult(record=record)
        self._log(f"Ensayo {record.id}: «{self.procedure.name}» sobre el setup «{self.setup.name}»")
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
        except RunAborted as exc:
            record.status, record.message = "abortado", str(exc)
            if current:
                self._finish_step(current, "abortado", str(exc))
            self._log(str(exc), "aviso")
        except (StepError, InstrumentError, OSError, ValueError) as exc:
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
        plan = prepare_run(self.library, self.procedure, self.setup, self.today)
        self.plan = plan
        record = self.result.record  # type: ignore[union-attr]
        warnings = [i for i in plan.issues if i.severity is Severity.WARNING]
        for issue in plan.issues:
            self._log(issue.message, "error" if issue.severity is Severity.ERROR else "aviso")
        if not plan.ok:
            errors = [i.message for i in plan.issues if i.severity is Severity.ERROR]
            raise StepError(f"No se puede ejecutar: {errors[0]}" + (f" (y {len(errors) - 1} errores más)" if len(errors) > 1 else ""))
        record.equipment = [self._equipment_entry(role, eq_id) for role, eq_id in self._assigned()]
        if plan.dut is not None:
            record.dut = self._equipment_entry("dut", plan.dut.id)
        detail = "Setup y procedimiento compatibles"
        if warnings:
            detail += f" ({len(warnings)} avisos)"
        return ("aviso" if warnings else "ok"), detail

    def _assigned(self) -> list[tuple[str, str]]:
        assert self.plan is not None and self.plan.template is not None
        pairs = []
        for role in self.plan.template.roles:
            for equipment_id in self.setup.ids_for(role):
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

    def _step_configuracion(self) -> tuple[str, str]:
        assert self.plan is not None and self.vna is not None
        sweep = self.procedure.sweep
        r = self.plan.frequency_range
        self.vna.preset()
        self.vna.configure_sweep(r.start, r.stop, sweep.points, sweep.sweep_type)
        self.vna.set_if_bandwidth(sweep.if_bandwidth)
        self.vna.set_power(sweep.power_dbm)
        self.vna.set_averaging(sweep.averages)
        self.result.record.sweep = {  # type: ignore[union-attr]
            "inicio": format_frequency(r.start, decimal="."),
            "fin": format_frequency(r.stop, decimal="."),
            "puntos": sweep.points,
            "tipo": sweep.sweep_type,
            "ifbw": format_frequency(sweep.if_bandwidth, decimal="."),
            "potencia_dbm": sweep.power_dbm,
            "promedios": sweep.averages,
        }
        return "ok", f"{r}, {sweep.points} puntos {sweep.sweep_type}, IFBW {format_frequency(sweep.if_bandwidth)}, {sweep.power_dbm:g} dBm"

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
            name = self.procedure.calibration.vna_cal_name.format(ensayo=record.id, setup=self.setup.id)
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
        assert self.vna is not None
        self.vna.single_sweep()
        frequencies = self.vna.frequencies()
        return frequencies, self.vna.read_s_matrix(ports)

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
            f, s = self._measure(ports)
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
            f, s = self._measure((port,))
            name = line if line != "—" else "DUT"
            result.frequencies, result.data[name] = f, s
            self.listener.data(name, f, s)
            self._log(f"Medida {name}: {len(f)} puntos")
        return "ok", f"{len(self.plan.lines)} medida(s) de 1 puerto ({CALIBRATION_STATUS[status]})"

    # ── export ─────────────────────────────────────────────────────────────
    def _file_stem(self, suffix: str = "") -> str:
        assert self.plan is not None and self.result is not None
        record = self.result.record
        template = self.procedure.export.filename or "{dut}_{fecha}"
        values = {
            "dut": self.plan.dut.id if self.plan.dut else "DUT",
            "fecha": f"{record.started:%Y%m%d-%H%M%S}",
            "setup": self.setup.id,
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
        lines = [
            "CAL-ELEKTRA — fichero de resultados",
            f"Ensayo: {record.id}",
            f"Procedimiento: {self.procedure.name} (v{self.procedure.version})",
            f"Setup: {self.setup.name} [{self.setup.template}]",
            f"DUT: {self.plan.dut.id if self.plan.dut else '—'}",
            f"VNA: {record.instrument.get('identificacion', '—')}",
            f"Calibración: {calibration.get('metodo', '—')}" + (f", kit {calibration['kit']}" if calibration.get("kit") else ""),
            f"Barrido: {record.sweep.get('inicio')} – {record.sweep.get('fin')}, {record.sweep.get('puntos')} puntos, IFBW {record.sweep.get('ifbw')}, {record.sweep.get('potencia_dbm')} dBm",
            f"Fecha: {record.started:%Y-%m-%d %H:%M:%S}",
        ]
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
            path = folder / f"{self._file_stem()}.s2p"
            write_touchstone(path, Touchstone(2, f, s, comments=self._comments([])), export.data_format, export.frequency_unit)
            files.append(path)
            if export.csv:
                files.append(self._write_csv_2p(folder / f"{self._file_stem()}.csv", f, s))
            summary = self._summary_2p(f, s)
        else:
            dut = self.plan.dut
            network = self.library.field(dut, "red_nominal") if dut is not None else None
            per_line = {}
            for name, s in result.data.items():
                suffix = name if name != "DUT" else ""
                path = folder / f"{self._file_stem(suffix)}.s1p"
                write_touchstone(path, Touchstone(1, f, s, comments=self._comments([f"Línea: {name}"] if suffix else [])), export.data_format, export.frequency_unit)
                files.append(path)
                if export.csv:
                    files.append(self._write_csv_1p(folder / f"{self._file_stem(suffix)}.csv", f, s[:, 0, 0], network))
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
        record.files = [p.name for p in files]
        result.files = files
        self._write_log(log_path)
        return "ok", ", ".join(p.name for p in files if p.suffix != ".txt")

    def _write_csv_2p(self, path: Path, f: np.ndarray, s: np.ndarray) -> Path:
        header = ["Frecuencia (Hz)"]
        columns = [f]
        for name, (i, j) in (("S11", (0, 0)), ("S21", (1, 0)), ("S12", (0, 1)), ("S22", (1, 1))):
            header += [f"{name} (dB)", f"{name} (°)"]
            columns += [_db(s[:, i, j]), np.degrees(np.angle(s[:, i, j]))]
        self._write_csv(path, header, columns)
        return path

    def _write_csv_1p(self, path: Path, f: np.ndarray, gamma: np.ndarray, network: str | None) -> Path:
        z = gamma_to_impedance(gamma)
        header = ["Frecuencia (Hz)", "S11 (dB)", "S11 (°)", "|Z| (Ω)", "fase Z (°)"]
        columns = [f, _db(gamma), np.degrees(np.angle(gamma)), np.abs(z), np.degrees(np.angle(z))]
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
        if "registro.txt" not in record.files:
            self._write_log(folder / "registro.txt")
            record.files.append("registro.txt")
        else:
            self._write_log(folder / "registro.txt")
        dump_yaml(record.to_yaml_dict(), folder / RECORD_FILE, header=f"Ensayo {record.id} — registrado por CAL-ELEKTRA")
