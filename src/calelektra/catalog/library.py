"""File-based library: the catalogue, hardware setups, procedures and test records.

Folder layout under the library root::

    catalogo/categorias/*.yaml          categories (typed fields, ports)
    catalogo/modelos/*.yaml             equipment models
    catalogo/equipos/*.yaml             physical equipment (inventory)
    catalogo/especificaciones/          specification files of the models: S2P/S1P for pass / fail
                                        (``especificaciones_s``) and datasheet/uncertainty YAML (stored)
    catalogo/datos_s/*.sNp              S-parameter files referenced by equipment
    catalogo/incertidumbres/*.yaml      uncertainty data files of the VNA units (``incertidumbre`` of the
                                        equipment): datasheet, certificate, trace noise, temperature…
    hardware_setups/plantillas/*.yaml   generic hardware setup templates
    hardware_setups/configurados/*.yaml configured setups (equipment and frequency points)
    hardware_setups/listas/*.csv        frequency lists of the setups
    procedimientos/*.yaml               test templates (hardware setup + EUT + how to measure)
    ensayos/[carpetas/]<id>/ensayo.yaml tests: records of the templates launched in folders chosen by
                                        the user, with their files (ensayos/carpetas.yaml lists test
                                        folders outside the library)
"""

from __future__ import annotations

import re
import shutil
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, TypeVar

import yaml
from pydantic import BaseModel, ValidationError

from calelektra.catalog.fields import ResolvedField, missing_required, validate_values
from calelektra.catalog.model import Category, Equipment, EquipmentModel, PortDef, SpecFile, parse_format, resolved_fields
from calelektra.core.connectors import Connector
from calelektra.core.frequency import FrequencyRange, intersect_all
from calelektra.core.issues import Issue, Severity
from calelektra.core.touchstone import Touchstone, TouchstoneError, read_touchstone
from calelektra.core.units import UnitError
from calelektra.procedures.model import ProcedureTemplate
from calelektra.procedures.record import RECORD_FILE, TestRecord
from calelektra.setups.model import ConfiguredSetup, SetupTemplate
from calelektra.uncertainty.engine import UncertaintyCalculationError, UncertaintyEngine

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
UNCERTAINTY_FOLDER = Path("catalogo/incertidumbres")
LIST_FOLDER = Path("hardware_setups/listas")
TESTS_FOLDER = Path("ensayos")
TEST_ROOTS_FILE = "carpetas.yaml"  # external test folders (inside ensayos/)

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
        self._uncertainty_cache: dict[Path, tuple[float, UncertaintyEngine]] = {}

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
        self._uncertainty_cache.clear()
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
            for spec in model.s_specifications:
                if not self.spec_path(model, spec).is_file():
                    self._issue(
                        Severity.WARNING,
                        "modelo.especificacion",
                        f"Modelo «{model.id}»: no se encuentra el fichero de especificación «{spec.file}» ({spec.name})",
                        model.source_path,
                    )
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
            if eq.uncertainty_file:
                for severity, message in self.uncertainty_problems(eq):
                    self._issue(severity, "equipo.incertidumbre", f"Equipo «{eq.id}»: {message}", eq.source_path, equipment_id=eq.id)
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
            if setup.frequencies.mode == "lista" and not self.setup_list_path(setup).is_file():
                self._issue(
                    Severity.WARNING,
                    "setup.lista",
                    f"Setup «{setup.id}»: no se encuentra la lista de frecuencias «{setup.frequencies.list_file}»",
                    setup.source_path,
                )
        for procedure in self.procedures.values():
            if procedure.setup_id and procedure.setup_id not in self.setups:
                self._issue(
                    Severity.WARNING,
                    "procedimiento.setup",
                    f"Plantilla de ensayo «{procedure.id}»: el hardware setup «{procedure.setup_id}» no existe",
                    procedure.source_path,
                )
            if procedure.eut_id and procedure.eut_id not in self.equipment:
                self._issue(
                    Severity.WARNING,
                    "procedimiento.eut",
                    f"Plantilla de ensayo «{procedure.id}»: el EUT «{procedure.eut_id}» no existe en el inventario",
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

    def templates_using(self, setup_id: str | None = None, equipment_id: str | None = None) -> list[str]:
        """Test templates that call a hardware setup or calibrate an equipment (EUT)."""
        return sorted(
            p.id
            for p in self.procedures.values()
            if (setup_id is not None and p.setup_id == setup_id) or (equipment_id is not None and p.eut_id == equipment_id)
        )

    # ── frequency lists of the setups, specifications of the models ────────
    def setup_list_path(self, setup: ConfiguredSetup, reference: str | None = None) -> Path:
        """Path of the frequency list of a setup (relative to the setup YAML)."""
        path = Path(reference or setup.frequencies.list_file or "")
        if path.is_absolute():
            return path
        base = setup.source_path.parent if setup.source_path else self.root / FOLDERS["setup"]
        return (base / path).resolve()

    def copy_setup_list(self, source: Path) -> str:
        """Copy a CSV frequency list into ``hardware_setups/listas/``; returns its path relative to the setups."""
        target_dir = self.root / LIST_FOLDER
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / Path(source).name
        if Path(source).resolve() != target.resolve():
            shutil.copy2(source, target)
        return str(Path("..") / "listas" / Path(source).name)

    def spec_path(self, model: EquipmentModel, spec: SpecFile | str) -> Path:
        """Path of a specification file of a model (relative to the model YAML)."""
        path = Path(spec.file if isinstance(spec, SpecFile) else spec)
        if path.is_absolute():
            return path
        base = model.source_path.parent if model.source_path else self.root / FOLDERS["modelo"]
        return (base / path).resolve()

    def copy_spec_file(self, source: Path) -> str:
        """Copy an S2P/S1P specification into ``catalogo/especificaciones/``; returns its path relative to the models."""
        target_dir = self.root / FOLDERS["especificaciones"]
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / Path(source).name
        if Path(source).resolve() != target.resolve():
            shutil.copy2(source, target)
        return str(Path("..") / "especificaciones" / Path(source).name)

    def specifications_of(self, equipment: Equipment) -> list[tuple[SpecFile, Path]]:
        """S-parameter specifications of the model of an equipment, with their files."""
        model = self.model_of(equipment)
        if model is None:
            return []
        return [(spec, self.spec_path(model, spec)) for spec in model.s_specifications]

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

    # ── uncertainty data of the VNA units ──────────────────────────────────
    def uncertainty_path(self, equipment: Equipment) -> Path | None:
        if not equipment.uncertainty_file:
            return None
        relative = Path(equipment.uncertainty_file)
        if relative.is_absolute():
            return relative
        base = equipment.source_path.parent if equipment.source_path else self.root / FOLDERS["equipo"]
        return (base / relative).resolve()

    def uncertainty_engine(self, equipment: Equipment) -> UncertaintyEngine:
        """Uncertainty engine of the data file of an equipment (cached until the file changes)."""
        path = self.uncertainty_path(equipment)
        if path is None:
            raise UncertaintyCalculationError(f"«{equipment.id}» no tiene fichero de incertidumbres")
        try:
            mtime = path.stat().st_mtime
        except OSError as exc:
            raise UncertaintyCalculationError(f"No se encuentra el fichero de incertidumbres {path.name}") from exc
        cached = self._uncertainty_cache.get(path)
        if cached and cached[0] == mtime:
            return cached[1]
        try:
            engine = UncertaintyEngine(path)
        except (OSError, yaml.YAMLError) as exc:
            raise UncertaintyCalculationError(f"{path.name}: {exc}") from exc
        self._uncertainty_cache[path] = (mtime, engine)
        return engine

    def uncertainty_designation(self, equipment: Equipment) -> str:
        """Text the engine checks against the equipment of the file (manufacturer and model)."""
        model = self.model_of(equipment)
        return f"{model.manufacturer} {model.designation}" if model else equipment.model_id

    def uncertainty_problems(self, equipment: Equipment) -> list[tuple[Severity, str]]:
        """Missing or invalid file, or a file of another model or unit."""
        try:
            engine = self.uncertainty_engine(equipment)
        except UncertaintyCalculationError as exc:
            return [(Severity.ERROR, str(exc))]
        problems: list[tuple[Severity, str]] = []
        try:
            engine.validate_equipment(self.uncertainty_designation(equipment))
        except UncertaintyCalculationError as exc:
            problems.append((Severity.WARNING, str(exc)))
        serial = engine.serial_number
        if serial and equipment.serial_number and serial.strip().upper() != equipment.serial_number.strip().upper():
            problems.append(
                (Severity.WARNING, f"el fichero de incertidumbres es del nº de serie {serial} y el equipo es el {equipment.serial_number}")
            )
        return problems

    def copy_uncertainty_file(self, source: Path) -> str:
        """Copy an uncertainty data file into the library; returns its path relative to the equipment folder."""
        target_dir = self.root / UNCERTAINTY_FOLDER
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / source.name
        if source.resolve() != target.resolve():
            shutil.copy2(source, target)
        return str(Path("..") / "incertidumbres" / source.name)

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
        templates = self.templates_using(equipment_id=equipment_id)
        if templates:
            raise LibraryError(f"«{equipment_id}» es el EUT de las plantillas de ensayo: {', '.join(templates)}")
        equipment = self.equipment.pop(equipment_id)
        if equipment.source_path and equipment.source_path.exists():
            equipment.source_path.unlink()

    def delete_setup(self, setup_id: str) -> None:
        templates = self.templates_using(setup_id=setup_id)
        if templates:
            raise LibraryError(f"El setup «{setup_id}» lo usan las plantillas de ensayo: {', '.join(templates)}")
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

    @property
    def procedures_folder(self) -> Path:
        return self.root / FOLDERS["procedimiento"]

    # ── tests: folders and records ─────────────────────────────────────────
    def _external_roots_file(self) -> Path:
        return self.tests_folder / TEST_ROOTS_FILE

    def test_roots(self) -> list[Path]:
        """Folders that contain tests: ``ensayos/`` of the library and the external folders added by the user."""
        roots = [self.tests_folder]
        path = self._external_roots_file()
        if path.is_file():
            try:
                data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            except (OSError, yaml.YAMLError):
                data = {}
            for item in data.get("externas", []) or []:
                folder = Path(item)
                if folder not in roots:
                    roots.append(folder)
        return roots

    def add_test_root(self, folder: Path) -> None:
        folder = Path(folder).resolve()
        external = [p for p in self.test_roots()[1:] if p != folder]
        if folder == self.tests_folder.resolve() or self._inside(folder, self.tests_folder):
            return
        external.append(folder)
        dump_yaml({"formato": "calelektra/carpetas_ensayos@0.3", "externas": [str(p) for p in external]}, self._external_roots_file(),
                  header="Carpetas de ensayos externas a la biblioteca")

    def remove_test_root(self, folder: Path) -> None:
        external = [p for p in self.test_roots()[1:] if Path(p).resolve() != Path(folder).resolve()]
        dump_yaml({"formato": "calelektra/carpetas_ensayos@0.3", "externas": [str(p) for p in external]}, self._external_roots_file(),
                  header="Carpetas de ensayos externas a la biblioteca")

    @staticmethod
    def _inside(path: Path, folder: Path) -> bool:
        try:
            Path(path).resolve().relative_to(Path(folder).resolve())
        except ValueError:
            return False
        return True

    def load_test(self, folder: Path) -> TestRecord:
        path = Path(folder) / RECORD_FILE
        try:
            record = TestRecord.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
        except (OSError, yaml.YAMLError, ValidationError, TypeError) as exc:
            raise LibraryError(f"No se puede leer el ensayo {path}: {exc}") from exc
        record.set_folder(Path(folder))
        return record

    def tests(self, root: Path | None = None) -> list[TestRecord]:
        """Tests under the test folders (or under ``root``), most recent first; unreadable ones are skipped."""
        records = []
        seen: set[Path] = set()
        for base in [Path(root)] if root is not None else self.test_roots():
            if not base.is_dir():
                continue
            for path in sorted(base.rglob(RECORD_FILE)):
                folder = path.parent.resolve()
                if folder in seen:
                    continue
                seen.add(folder)
                try:
                    records.append(self.load_test(path.parent))
                except LibraryError:
                    continue
        return sorted(records, key=lambda r: r.started, reverse=True)

    def test_folders(self, root: Path) -> list[Path]:
        """Sub-folders of ``root`` that organise tests (folders that are not tests themselves)."""
        if not root.is_dir():
            return []
        return sorted(p for p in root.iterdir() if p.is_dir() and not (p / RECORD_FILE).exists() and not p.name.startswith("."))

    def new_test_id(self, folder: Path, name: str) -> str:
        """Folder name of a new test: the name without accents or symbols, unique in ``folder``."""
        ascii_name = unicodedata.normalize("NFKD", name.strip()).encode("ascii", "ignore").decode("ascii")
        base = re.sub(r"[^A-Za-z0-9_.-]+", "_", ascii_name).strip("_.") or "ensayo"
        test_id, counter = base, 2
        while (Path(folder) / test_id).exists():
            test_id, counter = f"{base}_{counter}", counter + 1
        return test_id

    def new_test_record(self, folder: Path, name: str, procedure: ProcedureTemplate, operator: str | None = None) -> TestRecord:
        """Record of a test template launched in ``folder`` (``<folder>/<id>/ensayo.yaml``)."""
        folder = Path(folder)
        folder.mkdir(parents=True, exist_ok=True)
        test_id = self.new_test_id(folder, name)
        record = TestRecord(
            id=test_id,
            name=name.strip() or test_id,
            operator=operator or None,
            started=datetime.now(),
            procedure={"id": procedure.id, "nombre": procedure.name, "version": procedure.version, "algoritmo": procedure.algorithm},
            template=procedure.to_yaml_dict(),
        )
        record.set_folder(folder / test_id)
        self.save_test(record)
        return record

    def save_test(self, record: TestRecord) -> Path:
        assert record.folder is not None
        record.folder.mkdir(parents=True, exist_ok=True)
        path = record.folder / RECORD_FILE
        dump_yaml(record.to_yaml_dict(), path, header=f"Ensayo {record.id} — registrado por CAL-ELEKTRA")
        return path

    def delete_test(self, test: "TestRecord | str | Path") -> None:
        if isinstance(test, TestRecord):
            folder = test.folder
        elif isinstance(test, Path):
            folder = test
        else:
            folder = next((r.folder for r in self.tests() if r.id == test), None)
        if folder is None or not (Path(folder) / RECORD_FILE).is_file():
            raise LibraryError(f"No existe el ensayo «{test}»")
        shutil.rmtree(folder)
