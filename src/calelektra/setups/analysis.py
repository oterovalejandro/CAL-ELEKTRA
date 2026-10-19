"""Validation of a configured hardware setup.

Checks, per role: cardinality, category, service state, calibration validity,
ports, allowed types and S-parameter data. Per frequency segment: the
effective range (intersection of every measuring equipment) must contain the
required range; otherwise an alarm names the equipment limiting each edge.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, timedelta

from calelektra.catalog.library import Library
from calelektra.catalog.model import Equipment
from calelektra.core.frequency import CoverageResult, FrequencyRange, RangeContribution, analyze_coverage
from calelektra.core.issues import Issue, Severity, has_errors
from calelektra.core.touchstone import TouchstoneError
from calelektra.core.units import format_frequency
from calelektra.setups.model import ConfiguredSetup, Role, Segment, SetupTemplate

CALIBRATION_WARNING_DAYS = 30


@dataclass
class SegmentAnalysis:
    segment: Segment
    coverage: CoverageResult


@dataclass
class SetupAnalysis:
    setup: ConfiguredSetup
    template: SetupTemplate | None
    issues: list[Issue] = field(default_factory=list)
    segments: list[SegmentAnalysis] = field(default_factory=list)
    role_severity: dict[str, Severity] = field(default_factory=dict)

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
            self._check_coverage(segment, label)
        return self.result

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
        if not ids and role.required:
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
            count = len(model.port_names(category)) or int(model.parameters.get("num_puertos", 0))
            if count < role.min_ports:
                add(Severity.ERROR, "equipo.puertos", f"«{label}» necesita al menos {role.min_ports} puertos y «{equipment_id}» tiene {count}")
        if role.allowed_types:
            kind = equipment.parameters.get("tipo", model.parameters.get("tipo"))
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
        if category.kind == "posicionador" and model.parameters.get("motorizado") is False:
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
