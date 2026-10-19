"""Measurement procedure templates (the «test templates» of ELEKTRA).

A procedure says *how* to measure: which algorithm, sweep settings,
calibration method, verification and export. It runs on a configured hardware
setup whose template is in ``plantillas_compatibles``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, field_serializer, field_validator

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


class _Section(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="allow")


class SweepSettings(_Section):
    points: int = Field(default=801, alias="puntos", ge=2)
    sweep_type: Literal["lineal", "logaritmico"] = Field(default="lineal", alias="tipo")
    if_bandwidth: float = Field(default=1e3, alias="ifbw")
    power_dbm: float = Field(default=-10.0, alias="potencia_dbm")
    averages: int = Field(default=1, alias="promedios", ge=1)

    @field_validator("if_bandwidth", mode="before")
    @classmethod
    def _frequency(cls, value: Any) -> float:
        return parse_frequency(value)

    @field_serializer("if_bandwidth")
    def _frequency_text(self, value: float) -> str:
        return format_frequency(value, decimal=".")


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

    def to_yaml_dict(self) -> dict[str, Any]:
        return self.model_dump(by_alias=True, exclude_none=True, mode="json")
