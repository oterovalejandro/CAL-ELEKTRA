"""File-based library: the catalogue and the hardware setups stored as YAML.

Folder layout under the library root::

    catalogo/categorias/*.yaml        categories
    catalogo/modelos/*.yaml           equipment models
    catalogo/equipos/*.yaml           physical equipment (inventory)
    catalogo/especificaciones/*.yaml  specification / uncertainty files (stored, not processed yet)
    catalogo/datos_s/*.sNp            S-parameter files referenced by equipment
    hardware_setups/plantillas/*.yaml generic hardware setup templates
    hardware_setups/configurados/*.yaml configured setups
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, TypeVar

import yaml
from pydantic import BaseModel, ValidationError

from calelektra.catalog.model import Category, Equipment, EquipmentModel, parse_format
from calelektra.core.frequency import FrequencyRange, intersect_all
from calelektra.core.issues import Issue, Severity
from calelektra.core.touchstone import Touchstone, TouchstoneError, read_touchstone
from calelektra.core.units import UnitError
from calelektra.setups.model import ConfiguredSetup, SetupTemplate

FOLDERS = {
    "categoria": Path("catalogo/categorias"),
    "modelo": Path("catalogo/modelos"),
    "equipo": Path("catalogo/equipos"),
    "especificaciones": Path("catalogo/especificaciones"),
    "plantilla_setup": Path("hardware_setups/plantillas"),
    "setup": Path("hardware_setups/configurados"),
}
S_PARAMETER_FOLDER = Path("catalogo/datos_s")

T = TypeVar("T", bound=BaseModel)


@dataclass
class EquipmentRange:
    """Frequency range of an equipment and where each limit comes from."""

    range: FrequencyRange | None
    sources: list[str] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)


class LibraryError(ValueError):
    pass


def _validation_message(exc: ValidationError) -> str:
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
                detail = _validation_message(exc) if isinstance(exc, ValidationError) else str(exc)
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
            if model.category not in self.categories:
                self._issue(
                    Severity.ERROR,
                    "modelo.categoria",
                    f"Modelo «{model.id}»: la categoría «{model.category}» no existe",
                    model.source_path,
                )
            try:
                model.nominal_range()
            except (UnitError, ValueError) as exc:
                self._issue(Severity.ERROR, "modelo.rango", f"Modelo «{model.id}»: {exc}", model.source_path)
        for eq in self.equipment.values():
            if eq.model_id not in self.models:
                self._issue(
                    Severity.ERROR,
                    "equipo.modelo",
                    f"Equipo «{eq.id}»: el modelo «{eq.model_id}» no existe",
                    eq.source_path,
                    equipment_id=eq.id,
                )
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
        for setup in self.setups.values():
            if setup.template not in self.templates:
                self._issue(
                    Severity.ERROR,
                    "setup.plantilla",
                    f"Setup «{setup.id}»: la plantilla «{setup.template}» no existe",
                    setup.source_path,
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
        """Intersection of the model's nominal range, the unit's override and its S-parameter data."""
        result = EquipmentRange(range=None)
        ranges: list[FrequencyRange] = []
        model = self.model_of(equipment)
        try:
            nominal = model.nominal_range() if model else None
        except (UnitError, ValueError):
            nominal = None
        if nominal is not None:
            ranges.append(nominal)
            result.sources.append(f"Modelo {model.display_name}: {nominal}")
        try:
            override = equipment.range_override()
        except (UnitError, ValueError) as exc:
            override = None
            result.issues.append(Issue(Severity.ERROR, "equipo.rango", f"«{equipment.id}»: {exc}", equipment_id=equipment.id))
        if override is not None:
            ranges.append(override)
            result.sources.append(f"Equipo {equipment.id}: {override}")
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

    # ── saving and importing ───────────────────────────────────────────────
    def _target_path(self, kind: str, item_id: str, current: Path | None) -> Path:
        return current or (self.root / FOLDERS[kind] / f"{item_id}.yaml")

    def save_equipment(self, equipment: Equipment) -> Path:
        path = self._target_path("equipo", equipment.id, equipment.source_path)
        dump_yaml(equipment.to_yaml_dict(), path, header="Equipo del inventario — editado con CAL-ELEKTRA")
        equipment.set_source_path(path)
        self.equipment[equipment.id] = equipment
        return path

    def save_setup(self, setup: ConfiguredSetup) -> Path:
        path = self._target_path("setup", setup.id, setup.source_path)
        dump_yaml(setup.to_yaml_dict(), path, header="Hardware setup configurado — editado con CAL-ELEKTRA")
        setup.set_source_path(path)
        self.setups[setup.id] = setup
        return path

    def copy_s_parameter_file(self, source: Path) -> str:
        """Copy an sNp file into the library and return its path relative to the equipment folder."""
        target_dir = self.root / S_PARAMETER_FOLDER
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / source.name
        if source.resolve() != target.resolve():
            shutil.copy2(source, target)
        return str(Path("..") / "datos_s" / source.name)

    def import_yaml(self, source: Path) -> tuple[str, Path]:
        """Copy a catalogue/setup YAML into its folder (detected from ``formato``) and reload."""
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
        }
        if kind in validators:
            try:
                validators[kind](data)
            except ValidationError as exc:
                raise LibraryError(f"{Path(source).name}: {_validation_message(exc)}") from exc
        target = self.root / FOLDERS[kind] / Path(source).name
        target.parent.mkdir(parents=True, exist_ok=True)
        if Path(source).resolve() != target.resolve():
            shutil.copy2(source, target)
        self.reload()
        return kind, target
