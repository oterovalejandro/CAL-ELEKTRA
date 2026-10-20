"""Validation of a configured hardware setup.

Checks, per role: cardinality, category, service state, calibration validity,
ports, allowed types and S-parameter data. Then the field-based requirements
of the template (``requisitos``), the connectors of every connection and the
VNA calibration (reference planes, method, cal kit). Per frequency segment:
the effective range (intersection of every measuring equipment) must contain
the required range; otherwise an alarm names the equipment limiting each edge.
Finally the frequency points of the setup (``frecuencias``) are computed and
checked against the VNA.
"""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass, field
from datetime import date, timedelta

from calelektra.catalog.library import Library
from calelektra.catalog.model import Equipment
from calelektra.core.frequency import CoverageResult, FrequencyRange, RangeContribution, analyze_coverage
from calelektra.core.issues import Issue, Severity, has_errors
from calelektra.core.touchstone import TouchstoneError
from calelektra.core.units import format_frequency
from calelektra.setups.connections import CalibrationDesign, ConnectionCheck, check_connections, design_calibration
from calelektra.setups.frequencies import FrequencyError, FrequencyPlan, plan_frequencies
from calelektra.setups.model import ConfiguredSetup, Role, Segment, SetupTemplate
from calelektra.setups.requirements import RequirementResult, evaluate

REQUIREMENT_SEVERITY = {"error": Severity.ERROR, "aviso": Severity.WARNING, "info": Severity.INFO}

CALIBRATION_WARNING_DAYS = 30


@dataclass
class SegmentAnalysis:
    segment: Segment
    coverage: CoverageResult


@dataclass
class Check:
    """One line of the compatibility table shown with the setup."""

    group: str  # Requisito | Conexión | Calibración
    subject: str
    status: str  # ok | error | aviso | info | sin_datos
    detail: str
    segment: str | None = None


STATUS_OF = {None: "ok", Severity.ERROR: "error", Severity.WARNING: "aviso", Severity.INFO: "info"}


@dataclass
class SetupAnalysis:
    setup: ConfiguredSetup
    template: SetupTemplate | None
    issues: list[Issue] = field(default_factory=list)
    segments: list[SegmentAnalysis] = field(default_factory=list)
    role_severity: dict[str, Severity] = field(default_factory=dict)
    checks: list[Check] = field(default_factory=list)
    requirements: list[RequirementResult] = field(default_factory=list)
    connections: list[ConnectionCheck] = field(default_factory=list)
    calibration: CalibrationDesign | None = None
    frequency_plan: FrequencyPlan | None = None

    @property
    def ok(self) -> bool:
        return not has_errors(self.issues)

    @property
    def covered(self) -> bool:
        return bool(self.segments) and all(s.coverage.covered for s in self.segments)

    def add(self, issue: Issue) -> None:
        self.issues.append(issue)
        if issue.role:
            current = self.role_severity.get(issue.role)
            if current is None or issue.severity > current:
                self.role_severity[issue.role] = issue.severity


def _join_ids(contributions: list[RangeContribution]) -> str:
    return ", ".join(f"«{c.equipment_id}» ({c.role_label})" for c in contributions)


class _Analyzer:
    def __init__(self, setup: ConfiguredSetup, library: Library, today: date):
        self.setup = setup
        self.library = library
        self.today = today
        self.template = library.resolved_template(setup.template)
        self.result = SetupAnalysis(setup=setup, template=self.template)
        self._checked: set[tuple[str, str]] = set()

    def run(self) -> SetupAnalysis:
        if self.template is None:
            self.result.add(
                Issue(Severity.ERROR, "setup.plantilla", f"La plantilla «{self.setup.template}» no existe en la biblioteca")
            )
            return self.result
        self._check_unknown_roles()
        self._check_segments_layout()
        named = bool(self.setup.segments)
        for key, role in self.template.roles.items():
            if not role.per_segment:
                self._check_role(key, role, None, None)
        for segment in self.setup.effective_segments():
            label = segment.name if named else None
            for key, role in self.template.roles.items():
                if role.per_segment:
                    self._check_role(key, role, segment, label)
        self._check_requirements(named)
        self._check_connections(named)
        self._check_vna_calibration()
        for segment in self.setup.effective_segments():
            self._check_coverage(segment, segment.name if named else None)
        self._check_frequencies()
        return self.result

    # ── frequency points ───────────────────────────────────────────────────
    def _check_frequencies(self) -> None:
        assert self.template is not None
        settings = self.setup.frequencies
        list_path = self.library.setup_list_path(self.setup) if settings.mode == "lista" else None
        try:
            plan = plan_frequencies(settings, self.setup.required_range, list_path)
        except FrequencyError as exc:
            self.result.add(Issue(Severity.ERROR, "frecuencias.definicion", f"Frecuencias: {exc}"))
            return
        self.result.frequency_plan = plan
        if plan.note:
            self.result.add(Issue(Severity.WARNING, "frecuencias.aviso", f"Frecuencias: {plan.note}"))
        self.result.add(Issue(Severity.INFO, "frecuencias.plan", f"Frecuencias de medida: {plan.describe()}"))
        for key, role in self.template.roles.items():
            if "vna" not in role.categories:
                continue
            for equipment_id in self.setup.ids_for(key):
                vna = self.library.equipment.get(equipment_id)
                max_points = self.library.field(vna, "puntos_max") if vna is not None else None
                if max_points and plan.points > int(max_points):
                    self.result.add(
                        Issue(
                            Severity.ERROR,
                            "frecuencias.puntos",
                            f"Las frecuencias tienen {plan.points} puntos y «{equipment_id}» admite {max_points} (campo «Nº máximo de puntos»)",
                            role=key,
                            equipment_id=equipment_id,
                        )
                    )

    # ── requirements on the fields of the assigned equipment ───────────────
    def _check_requirements(self, named: bool) -> None:
        assert self.template is not None
        labels = {key: role.display(key) for key, role in self.template.roles.items()}
        seen: set[tuple[str, str, bool | None]] = set()
        for requirement in self.template.requirements:
            roles = [r for r in requirement.roles() if r in self.template.roles]
            per_segment = any(self.template.roles[r].per_segment for r in roles)
            contexts: list[Segment | None] = list(self.setup.effective_segments()) if per_segment else [None]
            for segment in contexts:
                choices = []
                for role_key in roles:
                    role = self.template.roles[role_key]
                    ids = self.setup.ids_for(role_key, segment if role.per_segment else None)
                    choices.append([(role_key, self.library.equipment[i]) for i in ids if i in self.library.equipment])
                if not all(choices):
                    continue  # a role is not assigned: reported elsewhere (or optional)
                required_range = segment.range if segment is not None else self.setup.required_range
                segment_label = segment.name if (segment is not None and named) else None
                for combination in itertools.product(*choices):
                    result = evaluate(requirement, dict(combination), self.library, labels, required_range)
                    key = (result.subject, result.detail, result.ok)
                    if key in seen:
                        continue
                    seen.add(key)
                    self.result.requirements.append(result)
                    equipment_id = combination[0][1].id
                    if result.ok:
                        status, severity = "ok", None
                    elif result.ok is None:
                        status, severity = "sin_datos", Severity.WARNING
                    else:
                        severity = REQUIREMENT_SEVERITY[requirement.severity]
                        status = STATUS_OF[severity]
                    self.result.checks.append(Check("Requisito", result.subject, status, result.detail, segment_label))
                    if severity is not None:
                        prefix = "No se puede comprobar un requisito" if result.ok is None else "Requisito no cumplido"
                        self.result.add(
                            Issue(
                                severity,
                                "requisito",
                                f"{prefix}: {result.subject} — {result.detail}",
                                role=requirement.role,
                                equipment_id=equipment_id,
                                segment=segment_label,
                            )
                        )

    # ── connectors of every connection ─────────────────────────────────────
    def _check_connections(self, named: bool) -> None:
        assert self.template is not None
        per_segment = any(role.per_segment for role in self.template.roles.values())
        contexts: list[Segment | None] = list(self.setup.effective_segments()) if per_segment else [None]
        seen: set[tuple[frozenset[str], str]] = set()
        for segment in contexts:
            segment_label = segment.name if (segment is not None and named) else None
            for check in check_connections(self.template, self.setup, self.library, segment):
                key = (frozenset((check.a.text(), check.b.text())), check.message)
                if key in seen:
                    continue
                seen.add(key)
                self.result.connections.append(check)
                state = self.template.connection_states[check.state].description or check.state
                bridged = f" (sin {', '.join(check.bridged)})" if check.bridged else ""
                subject = f"{check.a.text()} ↔ {check.b.text()}{bridged}"
                self.result.checks.append(
                    Check("Conexión", subject, STATUS_OF[check.severity], f"{check.message} — estado «{state}»", segment_label)
                )
                if check.severity in (Severity.ERROR, Severity.WARNING):
                    role = check.a.endpoint.role if check.a.equipment else check.b.endpoint.role
                    self.result.add(
                        Issue(
                            check.severity,
                            "conexion.conectores",
                            f"Conexión {subject}: {check.message}",
                            role=role,
                            segment=segment_label,
                        )
                    )
                    other = check.b.endpoint.role
                    if other != role:
                        current = self.result.role_severity.get(other)
                        if current is None or check.severity > current:
                            self.result.role_severity[other] = check.severity

    # ── calibration of the VNA ports ───────────────────────────────────────
    def _check_vna_calibration(self) -> None:
        assert self.template is not None
        design = design_calibration(self.template, self.setup, self.library)
        self.result.calibration = design
        if design.method is None:
            if self.template.vna_calibration:
                self.result.checks.append(Check("Calibración", "Calibración del VNA", "info", design.reason))
            return
        planes = "; ".join(plane.text() for plane in design.planes)
        self.result.checks.append(Check("Calibración", f"Método {design.method}", "ok" if design.ok else "error", f"{design.reason}. {planes}"))
        kit_role = self.template.vna_calibration.get("kit")
        for severity, message in design.checks:
            self.result.checks.append(Check("Calibración", "Calibración del VNA", STATUS_OF[severity], message))
            if severity in (Severity.ERROR, Severity.WARNING):
                self.result.add(Issue(severity, "calibracion.vna", message, role=kit_role if "kit" in message.lower() else None))

    # ── roles ──────────────────────────────────────────────────────────────
    def _check_unknown_roles(self) -> None:
        assert self.template is not None
        keys = set(self.setup.assignments)
        for segment in self.setup.segments:
            keys |= set(segment.assignments)
            for key in segment.assignments:
                role = self.template.roles.get(key)
                if role is not None and not role.per_segment:
                    self.result.add(
                        Issue(
                            Severity.WARNING,
                            "rol.no_por_segmento",
                            f"El rol «{role.display(key)}» es común a todo el setup; su asignación en el segmento «{segment.name}» se ignora",
                            role=key,
                            segment=segment.name,
                        )
                    )
        for key in sorted(keys - set(self.template.roles)):
            self.result.add(
                Issue(Severity.WARNING, "rol.desconocido", f"La plantilla no tiene el rol «{key}»; su asignación se ignora", role=key)
            )

    def _check_role(self, key: str, role: Role, segment: Segment | None, segment_label: str | None) -> None:
        ids = self.setup.ids_for(key, segment)
        where = f" en el segmento «{segment_label}»" if segment_label else ""
        if not ids and role.required and role.is_dut:
            self.result.add(
                Issue(
                    Severity.INFO,
                    "rol.dut_en_ensayo",
                    f"«{role.display(key)}» lo define cada plantilla de ensayo{where}",
                    role=key,
                    segment=segment_label,
                )
            )
        elif not ids and role.required:
            self.result.add(
                Issue(
                    Severity.ERROR,
                    "rol.sin_asignar",
                    f"Falta asignar el rol obligatorio «{role.display(key)}»{where}",
                    role=key,
                    segment=segment_label,
                )
            )
        if len(ids) > 1 and not role.multiple:
            self.result.add(
                Issue(
                    Severity.ERROR,
                    "rol.multiple",
                    f"El rol «{role.display(key)}» admite un solo equipo{where} y tiene {len(ids)}",
                    role=key,
                    segment=segment_label,
                )
            )
        for equipment_id in ids:
            if (key, equipment_id) not in self._checked:
                self._checked.add((key, equipment_id))
                self._check_equipment(key, role, equipment_id)

    def _check_equipment(self, key: str, role: Role, equipment_id: str) -> None:
        label = role.display(key)
        equipment = self.library.equipment.get(equipment_id)

        def add(severity: Severity, code: str, message: str) -> None:
            self.result.add(Issue(severity, code, message, role=key, equipment_id=equipment_id))

        if equipment is None:
            add(Severity.ERROR, "equipo.inexistente", f"El equipo «{equipment_id}» asignado a «{label}» no existe en el inventario")
            return
        model = self.library.model_of(equipment)
        category = self.library.category_of(equipment)
        if model is None or category is None:
            add(Severity.ERROR, "equipo.sin_modelo", f"«{equipment_id}»: su modelo o categoría no están en el catálogo")
            return
        if model.category not in role.categories:
            add(
                Severity.ERROR,
                "equipo.categoria",
                f"«{equipment_id}» es de categoría «{category.name}», pero «{label}» requiere: {', '.join(role.categories)}",
            )
        if equipment.status != "en_servicio":
            add(Severity.ERROR, "equipo.estado", f"«{equipment_id}» no está en servicio ({equipment.status.replace('_', ' ')})")
        if not role.is_dut:
            self._check_calibration(equipment, category.requires_calibration, add)
        if role.min_ports:
            count = len(self.library.ports_of(equipment))
            if count < role.min_ports:
                add(Severity.ERROR, "equipo.puertos", f"«{label}» necesita al menos {role.min_ports} puertos y «{equipment_id}» tiene {count}")
        if role.allowed_types:
            kind = self.library.field(equipment, "tipo")
            if kind not in role.allowed_types:
                add(
                    Severity.ERROR,
                    "equipo.tipo",
                    f"«{label}» admite tipos {', '.join(role.allowed_types)} y «{equipment_id}» es «{kind}»",
                )
        requires_s = role.requires_s_parameters
        if requires_s is None:
            requires_s = category.requires_s_parameters and not role.is_dut
        if requires_s:
            self._check_s_parameters(equipment, category.s_parameter_ports, label, add)
        if category.kind == "posicionador" and self.library.field(equipment, "motorizado") is False:
            add(
                Severity.INFO,
                "posicionador.manual",
                f"«{equipment_id}» es un soporte manual: antes de medir se pedirá al operador que confirme altura y polarización",
            )

    def _check_calibration(self, equipment: Equipment, required: bool, add) -> None:
        calibration = equipment.calibration
        if calibration is None or calibration.due is None:
            if required:
                add(Severity.WARNING, "calibracion.sin_datos", f"«{equipment.id}» no tiene fecha de próxima calibración")
            return
        if calibration.due < self.today:
            add(Severity.ERROR, "calibracion.caducada", f"La calibración de «{equipment.id}» caducó el {calibration.due:%d/%m/%Y}")
        elif calibration.due <= self.today + timedelta(days=CALIBRATION_WARNING_DAYS):
            add(Severity.WARNING, "calibracion.proxima", f"La calibración de «{equipment.id}» caduca el {calibration.due:%d/%m/%Y}")

    def _check_s_parameters(self, equipment: Equipment, expected_ports: int | None, label: str, add) -> None:
        if equipment.s_parameters is None:
            add(
                Severity.ERROR,
                "datos_s.faltan",
                f"«{label}» requiere datos S (fichero .sNp con su referencia) y «{equipment.id}» no los tiene",
            )
            return
        try:
            data = self.library.touchstone_of(equipment)
        except TouchstoneError as exc:
            add(Severity.ERROR, "datos_s.invalidos", f"Datos S de «{equipment.id}»: {exc}")
            return
        if expected_ports and data.n_ports != expected_ports:
            add(
                Severity.ERROR,
                "datos_s.puertos",
                f"Los datos S de «{equipment.id}» son de {data.n_ports} puertos y se esperaban {expected_ports}",
            )
        if not equipment.s_parameters.reference:
            add(Severity.WARNING, "datos_s.sin_referencia", f"Los datos S de «{equipment.id}» no indican su referencia (certificado o medida)")

    # ── frequency coverage ─────────────────────────────────────────────────
    def _contributions(self, segment: Segment) -> list[RangeContribution]:
        assert self.template is not None
        contributions = []
        for key, role in self.template.roles.items():
            for equipment_id in self.setup.ids_for(key, segment if role.per_segment else None):
                equipment = self.library.equipment.get(equipment_id)
                category = self.library.category_of(equipment) if equipment else None
                if equipment is None or category is None:
                    continue
                relevant = role.frequency_relevant if role.frequency_relevant is not None else category.rf
                if not relevant:
                    continue
                info = self.library.frequency_range_of(equipment)
                for issue in info.issues:
                    self.result.add(
                        Issue(issue.severity, issue.code, issue.message, role=key, equipment_id=equipment_id)
                    )
                if info.range is None:
                    self.result.add(
                        Issue(
                            Severity.WARNING,
                            "rango.sin_definir",
                            f"«{equipment_id}» no tiene rango de frecuencia definido; no se tiene en cuenta en la cobertura",
                            role=key,
                            equipment_id=equipment_id,
                        )
                    )
                    continue
                contributions.append(
                    RangeContribution(
                        role=key,
                        role_label=role.display(key),
                        equipment_id=equipment_id,
                        range=info.range,
                        sources=tuple(info.sources),
                        is_dut=role.is_dut,
                    )
                )
        return contributions

    def _check_coverage(self, segment: Segment, segment_label: str | None) -> None:
        coverage = analyze_coverage(segment.range, self._contributions(segment))
        self.result.segments.append(SegmentAnalysis(segment=segment, coverage=coverage))
        where = f" (segmento «{segment_label}»)" if segment_label else ""
        measuring = [c for c in coverage.contributions if not c.is_dut]

        def add(severity: Severity, code: str, message: str, role: str | None = None, equipment_id: str | None = None):
            self.result.add(Issue(severity, code, message, role=role, equipment_id=equipment_id, segment=segment_label))

        if not measuring:
            add(Severity.ERROR, "rango.sin_equipos", f"No hay equipos de medida con rango de frecuencia asignados{where}")
        elif coverage.effective is None:
            add(
                Severity.ERROR,
                "rango.sin_interseccion",
                f"Los equipos no tienen ningún rango de frecuencia común{where}: "
                f"{_join_ids(coverage.lower_limiting)} empieza en {format_frequency(coverage.lower_limiting[0].range.start)} y "
                f"{_join_ids(coverage.upper_limiting)} termina en {format_frequency(coverage.upper_limiting[0].range.stop)}",
            )
            for contribution in coverage.lower_limiting + coverage.upper_limiting:
                self.result.role_severity[contribution.role] = Severity.ERROR
        elif not coverage.covered:
            add(
                Severity.ERROR,
                "rango.no_cubierto",
                f"El rango efectivo del setup ({coverage.effective}) no cubre el rango requerido ({segment.range}){where}",
            )
            effective = coverage.effective
            if effective.start > segment.range.start and coverage.uncovered:
                for c in coverage.lower_limiting:
                    add(
                        Severity.ERROR,
                        "rango.limite_inferior",
                        f"Sin cubrir {FrequencyRange(segment.range.start, effective.start)}: «{c.equipment_id}» ({c.role_label}) empieza en {format_frequency(effective.start)}",
                        role=c.role,
                        equipment_id=c.equipment_id,
                    )
            if effective.stop < segment.range.stop and coverage.uncovered:
                for c in coverage.upper_limiting:
                    add(
                        Severity.ERROR,
                        "rango.limite_superior",
                        f"Sin cubrir {FrequencyRange(effective.stop, segment.range.stop)}: «{c.equipment_id}» ({c.role_label}) termina en {format_frequency(effective.stop)}",
                        role=c.role,
                        equipment_id=c.equipment_id,
                    )
        else:
            add(
                Severity.INFO,
                "rango.cubierto",
                f"Rango efectivo {coverage.effective} cubre el rango requerido {segment.range}{where}",
            )
        for c in coverage.dut_outside:
            add(
                Severity.WARNING,
                "rango.dut",
                f"El rango requerido sale del rango nominal del DUT «{c.equipment_id}» ({c.range}){where}",
                role=c.role,
                equipment_id=c.equipment_id,
            )

    def _check_segments_layout(self) -> None:
        segments = sorted(self.setup.segments, key=lambda s: s.range.start)
        if not segments:
            return
        required = self.setup.required_range
        for segment in segments:
            if not required.contains(segment.range):
                self.result.add(
                    Issue(
                        Severity.WARNING,
                        "segmento.fuera",
                        f"El segmento «{segment.name}» ({segment.range}) sale del rango requerido ({required})",
                        segment=segment.name,
                    )
                )
        cursor = required.start
        for segment in segments:
            if segment.range.start > cursor and not math.isclose(segment.range.start, cursor, rel_tol=1e-9):
                gap_stop = min(segment.range.start, required.stop)
                if gap_stop > cursor:
                    self.result.add(
                        Issue(Severity.ERROR, "segmento.hueco", f"Ningún segmento cubre {FrequencyRange(cursor, gap_stop)}")
                    )
            cursor = max(cursor, segment.range.stop)
        if cursor < required.stop and not math.isclose(cursor, required.stop, rel_tol=1e-9):
            self.result.add(
                Issue(Severity.ERROR, "segmento.hueco", f"Ningún segmento cubre {FrequencyRange(cursor, required.stop)}")
            )


def analyze_setup(setup: ConfiguredSetup, library: Library, today: date | None = None) -> SetupAnalysis:
    return _Analyzer(setup, library, today or date.today()).run()
