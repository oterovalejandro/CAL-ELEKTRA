"""Record of an executed test («ensayo»): what was measured, with what and how.

Saved as ``ensayos/<id>/ensayo.yaml`` next to the result files (.s2p/.s1p,
CSV and the instrument log), so a calibration can be traced back to the
equipment, its certificates and S-parameter references, the VNA calibration
and the settings used.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr

RECORD_FILE = "ensayo.yaml"
STATUS_NAMES = {
    "en_curso": "En curso",
    "completado": "Completado",
    "verificacion_fallida": "Completado (verificación fallida)",
    "abortado": "Abortado",
    "error": "Error",
}


class StepRecord(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    key: str = Field(alias="paso")
    name: str = Field(alias="nombre")
    status: str = Field(alias="estado")  # ok | aviso | error | omitido | abortado
    detail: str = Field(default="", alias="detalle")
    finished: datetime | None = Field(default=None, alias="hora")


class TestRecord(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="allow")

    format: str = Field(default="calelektra/ensayo@0.3", alias="formato")
    id: str
    status: str = Field(default="en_curso", alias="estado")
    message: str | None = Field(default=None, alias="mensaje")
    simulated: bool = Field(default=False, alias="simulado")
    operator: str | None = Field(default=None, alias="operador")
    started: datetime = Field(alias="inicio")
    finished: datetime | None = Field(default=None, alias="fin")
    procedure: dict[str, Any] = Field(default_factory=dict, alias="procedimiento")
    setup: dict[str, Any] = Field(default_factory=dict)
    dut: dict[str, Any] = Field(default_factory=dict)
    equipment: list[dict[str, Any]] = Field(default_factory=list, alias="equipos")
    frequency_range: list[str] = Field(default_factory=list, alias="rango")
    sweep: dict[str, Any] = Field(default_factory=dict, alias="barrido")
    instrument: dict[str, Any] = Field(default_factory=dict, alias="instrumento")
    calibration: dict[str, Any] = Field(default_factory=dict, alias="calibracion")
    verification: dict[str, Any] = Field(default_factory=dict, alias="verificacion")
    results: dict[str, Any] = Field(default_factory=dict, alias="resultados")
    files: list[str] = Field(default_factory=list, alias="ficheros")
    steps: list[StepRecord] = Field(default_factory=list, alias="pasos")

    _folder: Path | None = PrivateAttr(default=None)

    @property
    def folder(self) -> Path | None:
        return self._folder

    def set_folder(self, folder: Path | None) -> None:
        self._folder = folder

    @property
    def status_name(self) -> str:
        return STATUS_NAMES.get(self.status, self.status)

    def to_yaml_dict(self) -> dict[str, Any]:
        return self.model_dump(by_alias=True, exclude_none=True, mode="json")
