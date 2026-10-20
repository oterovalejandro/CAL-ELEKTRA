"""File-based library: the catalogue, hardware setups, procedures and test records.

Folder layout under the library root::

    catalogo/categorias/*.yaml          categories (typed fields, ports)
    catalogo/modelos/*.yaml             equipment models
    catalogo/equipos/*.yaml             physical equipment (inventory)
    catalogo/especificaciones/*.yaml    specification / uncertainty files (stored, not processed yet)
    catalogo/datos_s/*.sNp              S-parameter files referenced by equipment
    hardware_setups/plantillas/*.yaml   generic hardware setup templates
    hardware_setups/configurados/*.yaml configured setups
    procedimientos/*.yaml               measurement procedure templates
    ensayos/<id>/ensayo.yaml            executed tests (calibrations) with their result files
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, TypeVar

import yaml
from pydantic import BaseModel, ValidationError

from calelektra.catalog.fields import ResolvedField, missing_required, validate_values
from calelektra.catalog.model import Category, Equipment, EquipmentModel, PortDef, parse_format, resolved_fields
from calelektra.core.connectors import Connector
from calelektra.core.frequency import FrequencyRange, intersect_all
from calelektra.core.issues import Issue, Severity
from calelektra.core.touchstone import Touchstone, TouchstoneError, read_touchstone
from calelektra.core.units import UnitError
from calelektra.procedures.model import ProcedureTemplate
from calelektra.procedures.record import RECORD_FILE, TestRecord
from calelektra.setups.model import ConfiguredSetup, SetupTemplate

FOLDERS = {
    "categoria": Path("catalogo/categorias"),
    "modelo": Path("catalogo/modelos"),
    "equipo": Path("catalogo/equipos"),
    "especificaciones": Path("catalogo/especificaciones"),
    "plantilla_setup": Path("hardware_setups/plantillas"),
    "setup": Path("hardware_setups/configurados"),
    "procedimiento": Path("procedimientos"),
}
S_PARAMETER_FOLDER = Path("catalogo/datos_s")
TESTS_FOLDER = Path("ensayos")

T = TypeVar("T", bound=BaseModel)


@dataclass
class EquipmentRange:
    """Frequency range of an equipment and where each limit comes from."""

    range: FrequencyRange | None
    sources: list[str] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)


class LibraryError(ValueError):
    pass


def validation_message(exc: ValidationError) -> str:
    parts = []
    for error in exc.errors():
        location = ".".join(str(item) for item in error["loc"])
        parts.append(f"{location}: {error['msg']}")
    return "; ".join(parts)


def dump_yaml(data: dict[str, Any], path: Path, header: str | None = None) -> None:
    text = yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=100)
    if header:
        text = "".join(f"# {line}\n" for line in header.splitlines()) + text
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class Library:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.categories: dict[str, Category] = {}
        self.models: dict[str, EquipmentModel] = {}
        self.equipment: dict[str, Equipment] = {}
        self.templates: dict[str, SetupTemplate] = {}
        self.setups: dict[str, ConfiguredSetup] = {}
        self.procedures: dict[str, ProcedureTemplate] = {}
        self.specification_files: list[Path] = []
        self.load_issues: list[Issue] = []
        self._touchstone_cache: dict[Path, tuple[float, Touchstone]] = {}

    # ── loading ────────────────────────────────────────────────────────────
    @classmethod
    def load(cls, root: str | Path) -> "Library":
        library = cls(root)
        library.reload()
        return library

    def reload(self) -> None:
        if not self.root.is_dir():
            raise LibraryError(f"La biblioteca {self.root} no existe")
        self.load_issues = []
        self._touchstone_cache.clear()
        self.categories = self._load_kind("categoria", Category)
        self.models = self._load_kind("modelo", EquipmentModel)
        self.equipment = self._load_kind("equipo", Equipment)
        self.templates = self._load_kind("plantilla_setup", SetupTemplate)
        self.setups = self._load_kind("setup", ConfiguredSetup)
        self.procedures = self._load_kind("procedimiento", ProcedureTemplate)
        self.specification_files = sorted((self.root / FOLDERS["especificaciones"]).glob("*.yaml"))
        self._cross_check()

    def _issue(self, severity: Severity, code: str, message: str, path: Path | None = None, **kw: Any) -> None:
        self.load_issues.append(Issue(severity, code, message, source=str(path) if path else None, **kw))

    def _load_kind(self, kind: str, cls: type[T]) -> dict[str, T]:
        items: dict[str, T] = {}
        folder = self.root / FOLDERS[kind]
        for path in sorted(folder.glob("*.yaml")):
            try:
                data = yaml.safe_load(path.read_text(encoding="utf-8"))
            except (OSError, yaml.YAMLError) as exc:
                self._issue(Severity.ERROR, "yaml.ilegible", f"{path.name}: no se puede leer el YAML ({exc})", path)
                continue
            if not isinstance(data, dict):
                self._issue(Severity.ERROR, "yaml.vacio", f"{path.name}: el fichero no contiene un objeto YAML", path)
                continue
            fmt = parse_format(data.get("formato"))
            if fmt is None or fmt[0] != kind:
                self._issue(
                    Severity.ERROR,
                    "yaml.formato",
                    f"{path.name}: se esperaba 'formato: calelektra/{kind}@…' y se encontró {data.get('formato')!r}",
                    path,
                )
                continue
            try:
                item = cls.model_validate(data)
            except (ValidationError, UnitError, ValueError) as exc:
                detail = validation_message(exc) if isinstance(exc, ValidationError) else str(exc)
                self._issue(Severity.ERROR, "yaml.invalido", f"{path.name}: {detail}", path)
                continue
            item.set_source_path(path)  # type: ignore[attr-defined]
            item_id = item.id  # type: ignore[attr-defined]
            if item_id in items:
                self._issue(Severity.ERROR, "id.duplicado", f"{path.name}: el identificador «{item_id}» está repetido", path)
                continue
            items[item_id] = item
        return items

    def _cross_check(self) -> None:
        for model in self.models.values():
            category = self.categories.get(model.category)
            if category is None:
                self._issue(
                    Severity.ERROR,
                    "modelo.categoria",
                    f"Modelo «{model.id}»: la categoría «{model.category}» no existe",
                    model.source_path,
                )
                continue
            owner = f"Modelo «{model.id}»"
            self.load_issues.extend(
                validate_values(category.fields, model.values, "modelo", owner, str(model.source_path))
            )
            resolved = resolved_fields(category, model)
            self.load_issues.extend(missing_required(category.fields, resolved, owner, ("modelo",)))
        for eq in self.equipment.values():
            model = self.models.get(eq.model_id)
            if model is None:
                self._issue(
                    Severity.ERROR,
                    "equipo.modelo",
                    f"Equipo «{eq.id}»: el modelo «{eq.model_id}» no existe",
                    eq.source_path,
                    equipment_id=eq.id,
                )
                continue
            category = self.categories.get(model.category)
            if category is not None:
                owner = f"Equipo «{eq.id}»"
                self.load_issues.extend(validate_values(category.fields, eq.values, "equipo", owner, str(eq.source_path)))
                resolved = resolved_fields(category, model, eq)
                self.load_issues.extend(missing_required(category.fields, resolved, owner, ("equipo", "ambos")))
            if eq.s_parameters is not None and not self.s_parameter_path(eq).is_file():
                self._issue(
                    Severity.ERROR,
                    "equipo.datos_s",
                    f"Equipo «{eq.id}»: no se encuentra el fichero de parámetros S «{eq.s_parameters.file}»",
                    eq.source_path,
                    equipment_id=eq.id,
                )
        for template in self.templates.values():
            if template.extends and template.extends not in self.templates:
                self._issue(
                    Severity.ERROR,
                    "plantilla.extiende",
                    f"Plantilla «{template.id}»: extiende «{template.extends}», que no existe",
                    template.source_path,
                )
        for template_id in self.templates:
            resolved = self.resolved_template(template_id)
            for key, role in (resolved.roles.items() if resolved else []):
                if not role.categories:
                    self._issue(
                        Severity.ERROR,
                        "plantilla.rol_sin_categoria",
                        f"Plantilla «{template_id}»: el rol «{key}» no tiene categoría",
                        self.templates[template_id].source_path,
                    )
                for category_id in role.categories:
                    if category_id not in self.categories:
                        self._issue(
                            Severity.ERROR,
                            "plantilla.categoria",
                            f"Plantilla «{template_id}», rol «{key}»: la categoría «{category_id}» no existe",
                            self.templates[template_id].source_path,
                        )
        for setup in self.setups.values():
            if setup.template not in self.templates:
                self._issue(
                    Severity.ERROR,
                    "setup.plantilla",
                    f"Setup «{setup.id}»: la plantilla «{setup.template}» no existe",
                    setup.source_path,
                )
        for procedure in self.procedures.values():
            for template_id in procedure.compatible_templates:
                if template_id not in self.templates:
                    self._issue(
                        Severity.WARNING,
                        "procedimiento.plantilla",
                        f"Procedimiento «{procedure.id}»: la plantilla compatible «{template_id}» no existe",
                        procedure.source_path,
                    )

    # ── queries ────────────────────────────────────────────────────────────
    def model_of(self, equipment: Equipment) -> EquipmentModel | None:
        return self.models.get(equipment.model_id)

    def category_of(self, equipment: Equipment) -> Category | None:
        model = self.model_of(equipment)
        return self.categories.get(model.category) if model else None

    def category_id_of(self, equipment: Equipment) -> str | None:
        model = self.model_of(equipment)
        return model.category if model else None

    def equipment_in_categories(self, categories: list[str]) -> list[Equipment]:
        return [eq for eq in self.equipment.values() if self.category_id_of(eq) in categories]

    def fields_of(self, equipment: Equipment) -> dict[str, ResolvedField]:
        """Effective field values of an equipment (equipment → model → default)."""
        model = self.model_of(equipment)
        category = self.category_of(equipment)
        if model is None or category is None:
            return {}
        return resolved_fields(category, model, equipment)

    def field(self, equipment: Equipment, key: str, default: Any = None) -> Any:
        resolved = self.fields_of(equipment).get(key)
        return resolved.value if resolved is not None and resolved.value is not None else default

    def ports_of(self, equipment: Equipment) -> list[PortDef]:
        category = self.category_of(equipment)
        return category.port_defs(self.fields_of(equipment)) if category else []

    def port_connector(self, equipment: Equipment, port: str) -> Connector | None:
        for definition in self.ports_of(equipment):
            if definition.name == port and definition.connector_field:
                value = self.field(equipment, definition.connector_field)
                return value if isinstance(value, Connector) else None
        return None

    def resolved_template(self, template_id: str) -> SetupTemplate | None:
        template = self.templates.get(template_id)
        if template is None:
            return None
        chain = [template]
        while chain[-1].extends:
            parent = self.templates.get(chain[-1].extends)
            if parent is None or parent in chain:
                break
            chain.append(parent)
        resolved = chain[-1]
        for child in reversed(chain[:-1]):
            resolved = child.merged_over(resolved)
        return resolved

    def references_to(self, equipment_id: str) -> list[str]:
        """Configured setups that use an equipment."""
        users = []
        for setup in self.setups.values():
            ids = set()
            for value in setup.assignments.values():
                ids.update([value] if isinstance(value, str) else list(value or []))
            for segment in setup.segments:
                for value in segment.assignments.values():
                    ids.update([value] if isinstance(value, str) else list(value or []))
            if equipment_id in ids:
                users.append(setup.id)
        return users

    # ── S-parameters and frequency ranges ──────────────────────────────────
    def s_parameter_path(self, equipment: Equipment) -> Path:
        assert equipment.s_parameters is not None
        relative = Path(equipment.s_parameters.file)
        if relative.is_absolute():
            return relative
        base = equipment.source_path.parent if equipment.source_path else self.root / FOLDERS["equipo"]
        return (base / relative).resolve()

    def touchstone_of(self, equipment: Equipment) -> Touchstone:
        """Parsed S-parameter file of an equipment (cached until the file changes)."""
        if equipment.s_parameters is None:
            raise TouchstoneError(f"«{equipment.id}» no tiene datos S")
        path = self.s_parameter_path(equipment)
        try:
            mtime = path.stat().st_mtime
        except OSError as exc:
            raise TouchstoneError(f"No se encuentra {path.name}") from exc
        cached = self._touchstone_cache.get(path)
        if cached and cached[0] == mtime:
            return cached[1]
        data = read_touchstone(path)
        self._touchstone_cache[path] = (mtime, data)
        return data

    def frequency_range_of(self, equipment: Equipment) -> EquipmentRange:
        """Usable range: intersection of the model's range, the unit's own range and its S-parameter data."""
        result = EquipmentRange(range=None)
        ranges: list[FrequencyRange] = []
        model = self.model_of(equipment)
        category = self.category_of(equipment)
        spec = category.fields.get("rango_frecuencia") if category else None
        for owner, values, label in (
            ("modelo", model.values if model else {}, f"Modelo {model.display_name}" if model else ""),
            ("equipo", equipment.values, f"Equipo {equipment.id}"),
        ):
            raw = values.get("rango_frecuencia")
            if raw is None or (spec is not None and not spec.allowed_in(owner)):
                continue
            try:
                value = FrequencyRange.parse(raw)
            except (UnitError, ValueError) as exc:
                result.issues.append(
                    Issue(Severity.ERROR, "equipo.rango", f"«{equipment.id}»: {exc}", equipment_id=equipment.id)
                )
                continue
            ranges.append(value)
            result.sources.append(f"{label}: {value}")
        if equipment.s_parameters is not None:
            try:
                data = self.touchstone_of(equipment)
            except TouchstoneError as exc:
                result.issues.append(
                    Issue(Severity.ERROR, "equipo.datos_s", f"«{equipment.id}»: {exc}", equipment_id=equipment.id)
                )
            else:
                ranges.append(data.frequency_range)
                reference = f" — {equipment.s_parameters.reference}" if equipment.s_parameters.reference else ""
                result.sources.append(
                    f"Datos S {Path(equipment.s_parameters.file).name}{reference}: {data.frequency_range}"
                )
        if ranges:
            result.range = intersect_all(ranges)
            if result.range is None:
                result.issues.append(
                    Issue(
                        Severity.ERROR,
                        "equipo.rango_vacio",
                        f"«{equipment.id}»: sus rangos de frecuencia (modelo, equipo, datos S) no se solapan",
                        equipment_id=equipment.id,
                    )
                )
        return result

    # ── saving, deleting and importing ─────────────────────────────────────
    def _target_path(self, kind: str, item_id: str, current: Path | None) -> Path:
        return current or (self.root / FOLDERS[kind] / f"{item_id}.yaml")

    def save_equipment(self, equipment: Equipment) -> Path:
        path = self._target_path("equipo", equipment.id, equipment.source_path)
        dump_yaml(equipment.to_yaml_dict(), path, header="Equipo del inventario — editado con CAL-ELEKTRA")
        equipment.set_source_path(path)
        self.equipment[equipment.id] = equipment
        return path

    def save_model(self, model: EquipmentModel) -> Path:
        path = self._target_path("modelo", model.id, model.source_path)
        dump_yaml(model.to_yaml_dict(), path, header="Modelo de equipo — editado con CAL-ELEKTRA")
        model.set_source_path(path)
        self.models[model.id] = model
        return path

    def save_setup(self, setup: ConfiguredSetup) -> Path:
        path = self._target_path("setup", setup.id, setup.source_path)
        dump_yaml(setup.to_yaml_dict(), path, header="Hardware setup configurado — editado con CAL-ELEKTRA")
        setup.set_source_path(path)
        self.setups[setup.id] = setup
        return path

    def save_procedure(self, procedure: ProcedureTemplate) -> Path:
        path = self._target_path("procedimiento", procedure.id, procedure.source_path)
        dump_yaml(procedure.to_yaml_dict(), path, header="Procedimiento de medida — editado con CAL-ELEKTRA")
        procedure.set_source_path(path)
        self.procedures[procedure.id] = procedure
        return path

    def delete_equipment(self, equipment_id: str) -> None:
        users = self.references_to(equipment_id)
        if users:
            raise LibraryError(f"«{equipment_id}» se usa en: {', '.join(users)}. Quítelo de esos setups antes de borrarlo.")
        equipment = self.equipment.pop(equipment_id)
        if equipment.source_path and equipment.source_path.exists():
            equipment.source_path.unlink()

    def delete_setup(self, setup_id: str) -> None:
        setup = self.setups.pop(setup_id)
        if setup.source_path and setup.source_path.exists():
            setup.source_path.unlink()

    def delete_procedure(self, procedure_id: str) -> None:
        procedure = self.procedures.pop(procedure_id)
        if procedure.source_path and procedure.source_path.exists():
            procedure.source_path.unlink()

    def copy_s_parameter_file(self, source: Path) -> str:
        """Copy an sNp file into the library and return its path relative to the equipment folder."""
        target_dir = self.root / S_PARAMETER_FOLDER
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / source.name
        if source.resolve() != target.resolve():
            shutil.copy2(source, target)
        return str(Path("..") / "datos_s" / source.name)

    def import_yaml(self, source: Path) -> tuple[str, Path]:
        """Copy a catalogue/setup/procedure YAML into its folder (detected from ``formato``) and reload."""
        data = yaml.safe_load(Path(source).read_text(encoding="utf-8"))
        fmt = parse_format(data.get("formato") if isinstance(data, dict) else None)
        if fmt is None or fmt[0] not in FOLDERS:
            raise LibraryError(f"{Path(source).name}: 'formato' no reconocido ({data!r:.60})")
        kind = fmt[0]
        validators: dict[str, Callable[[Any], Any]] = {
            "categoria": Category.model_validate,
            "modelo": EquipmentModel.model_validate,
            "equipo": Equipment.model_validate,
            "plantilla_setup": SetupTemplate.model_validate,
            "setup": ConfiguredSetup.model_validate,
            "procedimiento": ProcedureTemplate.model_validate,
        }
        if kind in validators:
            try:
                validators[kind](data)
            except ValidationError as exc:
                raise LibraryError(f"{Path(source).name}: {validation_message(exc)}") from exc
        target = self.root / FOLDERS[kind] / Path(source).name
        target.parent.mkdir(parents=True, exist_ok=True)
        if Path(source).resolve() != target.resolve():
            shutil.copy2(source, target)
        self.reload()
        return kind, target

    @property
    def tests_folder(self) -> Path:
        return self.root / TESTS_FOLDER

    def tests(self) -> list[TestRecord]:
        """Executed tests (``ensayos/<id>/ensayo.yaml``), most recent first; unreadable ones are skipped."""
        records = []
        for path in sorted(self.tests_folder.glob(f"*/{RECORD_FILE}")):
            try:
                record = TestRecord.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
            except (OSError, yaml.YAMLError, ValidationError, TypeError):
                continue
            record.set_folder(path.parent)
            records.append(record)
        return sorted(records, key=lambda r: r.started, reverse=True)

    def delete_test(self, test_id: str) -> None:
        folder = self.tests_folder / test_id
        if not (folder / RECORD_FILE).is_file():
            raise LibraryError(f"No existe el ensayo «{test_id}»")
        shutil.rmtree(folder)
