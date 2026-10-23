"""Test templates (the «test templates» of ELEKTRA).

A test template calls a **hardware setup** (which gives the bench and the
frequency points) and defines the **EUT** to calibrate (whose model gives the
S-parameter specifications for pass / fail). It says how to measure:
algorithm, trace settings (IFBW and averaging, and the smoothing used to
display the traces, for all traces or per trace), source power, calibration,
verification, uncertainty and export.

Launching a template in a folder makes a test: the record of that
measurement (``procedures/record.py``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, field_serializer, field_validator, model_validator

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
TRACES = {"vna_2_puertos": ("S11", "S21", "S12", "S22"), "vna_1_puerto": ("S11",)}
PLANES_NEEDED = {"vna_2_puertos": 2, "vna_1_puerto": 1}
# keys of earlier formats: the frequencies are now given by the hardware setup and
# the specifications by the model of the EUT
OBSOLETE_SWEEP_KEYS = ("modo", "tipo", "mode", "inicio", "fin", "definicion", "puntos", "paso", "fichero_lista")
OBSOLETE_TEMPLATE_KEYS = ("plantillas_compatibles", "especificaciones")


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
    """Source power and trace settings: IFBW, averaging and display smoothing, for all traces or per trace.

    The smoothing only changes how the traces are displayed; measurement,
    pass / fail and exported files use the traces as measured.
    """

    power_dbm: float = Field(default=-10.0, alias="potencia_dbm")
    # settings of all the traces …
    if_bandwidth: float = Field(default=1e3, alias="ifbw")
    averages: int = Field(default=1, alias="promedios", ge=1)
    smoothing_pct: float = Field(default=0.0, alias="suavizado_pct", ge=0, le=100)
    # … or of each trace
    per_trace: bool = Field(default=False, alias="por_traza")
    traces: dict[str, TraceSettings] = Field(default_factory=dict, alias="trazas")

    @model_validator(mode="before")
    @classmethod
    def _drop_frequencies(cls, data: Any) -> Any:
        if isinstance(data, dict):
            return {k: v for k, v in data.items() if k not in OBSOLETE_SWEEP_KEYS}
        return data

    @field_validator("if_bandwidth", mode="before")
    @classmethod
    def _ifbw(cls, value: Any) -> float:
        return parse_frequency(value)

    @field_serializer("if_bandwidth")
    def _ifbw_out(self, value: float) -> str:
        return format_frequency(value, decimal=".")

    def trace(self, name: str) -> tuple[float, int, float]:
        """Effective IFBW, averaging and display smoothing (% of the points) of a trace."""
        own = self.traces.get(name) if self.per_trace else None
        ifbw = own.if_bandwidth if own and own.if_bandwidth is not None else self.if_bandwidth
        averages = own.averages if own and own.averages is not None else self.averages
        smoothing = own.smoothing_pct if own and own.smoothing_pct is not None else self.smoothing_pct
        return ifbw, averages, smoothing

    def smoothing(self, traces: tuple[str, ...]) -> dict[str, float]:
        """Display smoothing of each trace (only the traces with smoothing)."""
        values = {name: self.trace(name)[2] for name in traces}
        return {name: pct for name, pct in values.items() if pct > 0}


class CalibrationSettings(_Section):
    method: Literal["automatico", "TOSM", "UOSM", "OSM", "existente"] = Field(default="automatico", alias="metodo")
    save_on_vna: bool = Field(default=False, alias="guardar_en_vna")
    vna_cal_name: str = Field(default="CALELEKTRA_{ensayo}", alias="nombre_en_vna")


class VerificationSettings(_Section):
    active: bool = Field(default=True, alias="activa")
    s21_tolerance_db: float = Field(default=0.1, alias="tolerancia_s21_db", gt=0)
    s11_max_db: float = Field(default=-30.0, alias="s11_max_db")


class UncertaintySettings(_Section):
    """Uncertainty of the traces, from the uncertainty data file of the VNA (``calelektra.uncertainty``)."""

    active: bool = Field(default=True, alias="activa")
    coverage_factor: float = Field(default=2.0, alias="k", gt=0)
    trace_noise: bool = Field(default=True, alias="ruido_traza")
    temperature_stability: bool = Field(default=True, alias="estabilidad_temperatura")


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
    setup_id: str | None = Field(default=None, alias="hardware_setup")  # configured setup: bench and frequencies
    eut_id: str | None = Field(default=None, alias="eut")  # equipment to calibrate; its model gives the specifications
    sweep: SweepSettings = Field(default_factory=SweepSettings, alias="barrido")
    calibration: CalibrationSettings = Field(default_factory=CalibrationSettings, alias="calibracion")
    verification: VerificationSettings = Field(default_factory=VerificationSettings, alias="verificacion")
    uncertainty: UncertaintySettings = Field(default_factory=UncertaintySettings, alias="incertidumbre")
    export: ExportSettings = Field(default_factory=ExportSettings, alias="exportacion")
    options: dict[str, Any] = Field(default_factory=dict, alias="opciones")

    _source_path: Path | None = PrivateAttr(default=None)

    @model_validator(mode="before")
    @classmethod
    def _drop_obsolete(cls, data: Any) -> Any:
        if isinstance(data, dict):
            return {k: v for k, v in data.items() if k not in OBSOLETE_TEMPLATE_KEYS}
        return data

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
