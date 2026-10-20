"""Measurement procedure templates (the «test templates» of ELEKTRA).

A procedure says *how* to measure: algorithm, frequency definition, trace
settings (IFBW, averaging, smoothing for all traces or per trace),
calibration, verification, the S2P specification files that give pass/fail,
and export. It runs on a configured hardware setup whose template is in
``plantillas_compatibles``; the cable to calibrate is chosen in each test.

Relative file paths (frequency list, specification files) are relative to the
``procedimientos/`` folder of the library.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, PrivateAttr, field_serializer, field_validator, model_validator

from calelektra.core.units import format_frequency, parse_frequency

ALGORITHMS = {
    "vna_2_puertos": "Parámetros S de 2 puertos con VNA (calibración TOSM/UOSM, medida y exportación .s2p)",
    "vna_1_puerto": "Reflexión en 1 puerto con VNA (calibración OSM, medida, impedancia y exportación .s1p)",
}
CALIBRATION_METHODS = {
    "automatico": "Automático según los conectores (TOSM si los planos se pueden unir; si no, UOSM)",
    "TOSM": "TOSM (through conocido, unión directa de los planos)",
    "UOSM": "UOSM (through desconocido)",
    "OSM": "OSM de 1 puerto",
    "existente": "Usar la calibración activa del VNA",
}
SWEEP_MODES = {"lineal": "Lineal", "logaritmico": "Logarítmico", "lista": "Lista de frecuencias (CSV)"}
LINEAR_DEFINITIONS = {"puntos": "Nº de puntos", "paso": "Paso de frecuencia"}
TRACES = {"vna_2_puertos": ("S11", "S21", "S12", "S22"), "vna_1_puerto": ("S11",)}
SPEC_CRITERIA = {
    "minimo": "Mínimo: medida ≥ especificación",
    "maximo": "Máximo: medida ≤ especificación",
    "tolerancia": "Tolerancia: |medida − especificación| ≤ tolerancia",
}
SPEC_QUANTITIES = {"magnitud_db": "Magnitud (dB)", "fase_grados": "Fase (°)"}


class _Section(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="allow")


def _optional_frequency(value: Any) -> float | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    return parse_frequency(value)


def _frequency_text(value: float | None) -> str | None:
    if value is None:
        return None
    return "DC" if value == 0 else format_frequency(value, decimal=".")


class TraceSettings(_Section):
    """Settings of one trace; empty values take those of «all traces»."""

    if_bandwidth: float | None = Field(default=None, alias="ifbw")
    averages: int | None = Field(default=None, alias="promedios", ge=1)
    smoothing_pct: float | None = Field(default=None, alias="suavizado_pct", ge=0, le=100)

    @field_validator("if_bandwidth", mode="before")
    @classmethod
    def _frequency(cls, value: Any) -> float | None:
        return _optional_frequency(value)

    @field_serializer("if_bandwidth")
    def _frequency_out(self, value: float | None) -> str | None:
        return _frequency_text(value)


class SweepSettings(_Section):
    """Frequency definition and trace settings of the measurement."""

    mode: Literal["lineal", "logaritmico", "lista"] = Field(
        default="lineal", validation_alias=AliasChoices("modo", "tipo", "mode"), serialization_alias="modo"
    )
    start: float | None = Field(default=None, alias="inicio")  # None: the required range of the setup
    stop: float | None = Field(default=None, alias="fin")
    linear_definition: Literal["puntos", "paso"] = Field(default="puntos", alias="definicion")
    points: int = Field(default=801, alias="puntos", ge=2)
    step: float | None = Field(default=None, alias="paso")
    list_file: str | None = Field(default=None, alias="fichero_lista")
    power_dbm: float = Field(default=-10.0, alias="potencia_dbm")
    # settings of all the traces …
    if_bandwidth: float = Field(default=1e3, alias="ifbw")
    averages: int = Field(default=1, alias="promedios", ge=1)
    smoothing_pct: float = Field(default=0.0, alias="suavizado_pct", ge=0, le=100)
    # … or of each trace
    per_trace: bool = Field(default=False, alias="por_traza")
    traces: dict[str, TraceSettings] = Field(default_factory=dict, alias="trazas")

    @field_validator("if_bandwidth", mode="before")
    @classmethod
    def _ifbw(cls, value: Any) -> float:
        return parse_frequency(value)

    @field_validator("start", "stop", "step", mode="before")
    @classmethod
    def _frequencies(cls, value: Any) -> float | None:
        return _optional_frequency(value)

    @field_serializer("if_bandwidth")
    def _ifbw_out(self, value: float) -> str:
        return format_frequency(value, decimal=".")

    @field_serializer("start", "stop", "step")
    def _frequencies_out(self, value: float | None) -> str | None:
        return _frequency_text(value)

    @model_validator(mode="after")
    def _consistent(self) -> "SweepSettings":
        if self.mode == "lineal" and self.linear_definition == "paso" and not self.step:
            raise ValueError("el barrido lineal por paso necesita el paso de frecuencia")
        if self.mode == "lista" and not self.list_file:
            raise ValueError("el barrido por lista necesita el fichero CSV con las frecuencias")
        if self.start is not None and self.stop is not None and self.stop <= self.start:
            raise ValueError("la frecuencia final debe ser mayor que la inicial")
        return self

    def summary(self) -> str:
        """Short text of the frequency definition, e.g. «Lineal 50 MHz – 40 GHz, paso 50 MHz»."""
        if self.mode == "lista":
            return f"Lista {Path(self.list_file or '').name}"
        span = "rango del setup" if self.start is None or self.stop is None else f"{format_frequency(self.start)} – {format_frequency(self.stop)}"
        if self.mode == "lineal" and self.linear_definition == "paso" and self.step:
            return f"Lineal {span}, paso {format_frequency(self.step)}"
        return f"{SWEEP_MODES[self.mode]} {span}, {self.points} puntos"

    @property
    def vna_sweep_type(self) -> str:
        return "logaritmico" if self.mode == "logaritmico" else "lineal"

    def trace(self, name: str) -> tuple[float, int, float]:
        """Effective IFBW, averaging and smoothing (% of the points) of a trace."""
        own = self.traces.get(name) if self.per_trace else None
        ifbw = own.if_bandwidth if own and own.if_bandwidth is not None else self.if_bandwidth
        averages = own.averages if own and own.averages is not None else self.averages
        smoothing = own.smoothing_pct if own and own.smoothing_pct is not None else self.smoothing_pct
        return ifbw, averages, smoothing


class SpecFile(_Section):
    """S2P (or S1P) file with specification values; pass/fail compares the measurement with it."""

    name: str = Field(alias="nombre")
    file: str = Field(alias="fichero")
    parameters: list[str] = Field(default_factory=lambda: ["S21"], alias="parametros")
    criterion: Literal["minimo", "maximo", "tolerancia"] = Field(default="minimo", alias="criterio")
    quantity: Literal["magnitud_db", "fase_grados"] = Field(default="magnitud_db", alias="magnitud")
    tolerance: float | None = Field(default=None, alias="tolerancia", gt=0)

    @field_validator("parameters", mode="before")
    @classmethod
    def _as_list(cls, value: Any) -> Any:
        return [value] if isinstance(value, str) else value

    @field_validator("parameters")
    @classmethod
    def _names(cls, value: list[str]) -> list[str]:
        names = [str(item).upper() for item in value]
        for item in names:
            if len(item) != 3 or item[0] != "S" or not item[1:].isdigit():
                raise ValueError(f"parámetro S no válido: {item}")
        return names

    @model_validator(mode="after")
    def _tolerance(self) -> "SpecFile":
        if self.criterion == "tolerancia" and self.tolerance is None:
            raise ValueError(f"«{self.name}»: el criterio de tolerancia necesita el valor de la tolerancia")
        return self

    def describe(self) -> str:
        unit = "dB" if self.quantity == "magnitud_db" else "°"
        values = ", ".join(f"|{p}|" if self.quantity == "magnitud_db" else f"∠{p}" for p in self.parameters)
        rule = {
            "minimo": "≥ especificación",
            "maximo": "≤ especificación",
            "tolerancia": f"= especificación ± {self.tolerance:g} {unit}" if self.tolerance else "= especificación ±",
        }[self.criterion]
        return f"{values} ({unit}) {rule}"


class CalibrationSettings(_Section):
    method: Literal["automatico", "TOSM", "UOSM", "OSM", "existente"] = Field(default="automatico", alias="metodo")
    save_on_vna: bool = Field(default=False, alias="guardar_en_vna")
    vna_cal_name: str = Field(default="CALELEKTRA_{ensayo}", alias="nombre_en_vna")


class VerificationSettings(_Section):
    active: bool = Field(default=True, alias="activa")
    s21_tolerance_db: float = Field(default=0.1, alias="tolerancia_s21_db", gt=0)
    s11_max_db: float = Field(default=-30.0, alias="s11_max_db")


class ExportSettings(_Section):
    data_format: Literal["RI", "MA", "DB"] = Field(default="RI", alias="formato")
    frequency_unit: Literal["HZ", "KHZ", "MHZ", "GHZ"] = Field(default="HZ", alias="unidad_frecuencia")
    filename: str = Field(default="{dut}_{fecha}", alias="nombre_fichero")
    csv: bool = True


class ProcedureTemplate(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="allow")

    format: str = Field(default="calelektra/procedimiento@0.3", alias="formato")
    id: str
    name: str = Field(alias="nombre")
    version: str = "1.0"
    status: str | None = Field(default=None, alias="estado")
    description: str | None = Field(default=None, alias="descripcion")
    algorithm: Literal["vna_2_puertos", "vna_1_puerto"] = Field(alias="algoritmo")
    compatible_templates: list[str] = Field(default_factory=list, alias="plantillas_compatibles")
    sweep: SweepSettings = Field(default_factory=SweepSettings, alias="barrido")
    calibration: CalibrationSettings = Field(default_factory=CalibrationSettings, alias="calibracion")
    verification: VerificationSettings = Field(default_factory=VerificationSettings, alias="verificacion")
    specifications: list[SpecFile] = Field(default_factory=list, alias="especificaciones")
    export: ExportSettings = Field(default_factory=ExportSettings, alias="exportacion")
    options: dict[str, Any] = Field(default_factory=dict, alias="opciones")

    _source_path: Path | None = PrivateAttr(default=None)

    @field_validator("version", mode="before")
    @classmethod
    def _version_as_text(cls, value: Any) -> str:
        return str(value)

    @property
    def source_path(self) -> Path | None:
        return self._source_path

    def set_source_path(self, path: Path | None) -> None:
        self._source_path = path

    @property
    def traces(self) -> tuple[str, ...]:
        return TRACES[self.algorithm]

    def to_yaml_dict(self) -> dict[str, Any]:
        return self.model_dump(by_alias=True, exclude_none=True, mode="json")
