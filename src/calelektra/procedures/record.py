"""Test («ensayo»): the record of one measurement launched from a test template.

A test template is launched in a folder chosen by the user; the run is
saved as ``<carpeta>/<ensayo>/ensayo.yaml`` next to its files: results
(.s2p/.s1p, CSV), copies of the frequency list and of the specification files
used, and the instrument log. The record keeps the template as launched, the
hardware setup with its frequency definition, every equipment with its
certificate, the VNA calibration, the verification, the uncertainty of the
traces, the pass / fail evaluation and the verdict, so a calibration can be
traced back.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr

from calelektra.procedures.model import ProcedureTemplate

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
    __test__ = False  # not a pytest test class
    model_config = ConfigDict(populate_by_name=True, extra="allow")

    format: str = Field(default="calelektra/ensayo@0.3", alias="formato")
    id: str
    name: str | None = Field(default=None, alias="nombre")
    status: str = Field(default="en_curso", alias="estado")
    message: str | None = Field(default=None, alias="mensaje")
    simulated: bool = Field(default=False, alias="simulado")
    operator: str | None = Field(default=None, alias="operador")
    started: datetime = Field(alias="inicio")
    finished: datetime | None = Field(default=None, alias="fin")
    verdict: str | None = Field(default=None, alias="veredicto")  # PASA | FALLA | NO_EVALUADO | SIN_ESPECIFICACIONES
    procedure: dict[str, Any] = Field(default_factory=dict, alias="procedimiento")  # id, name, version, algorithm
    template: dict[str, Any] = Field(default_factory=dict, alias="plantilla")  # test template as launched
    setup: dict[str, Any] = Field(default_factory=dict)  # hardware setup: id, name, required range, frequencies
    dut: dict[str, Any] = Field(default_factory=dict)  # EUT with its model, serial number and certificate
    equipment: list[dict[str, Any]] = Field(default_factory=list, alias="equipos")
    frequency_range: list[str] = Field(default_factory=list, alias="rango")
    sweep: dict[str, Any] = Field(default_factory=dict, alias="barrido")
    instrument: dict[str, Any] = Field(default_factory=dict, alias="instrumento")
    calibration: dict[str, Any] = Field(default_factory=dict, alias="calibracion")
    verification: dict[str, Any] = Field(default_factory=dict, alias="verificacion")
    specifications: list[dict[str, Any]] = Field(default_factory=list, alias="especificaciones")  # of the EUT model, as used
    spec_results: list[dict[str, Any]] = Field(default_factory=list, alias="evaluacion")  # pass / fail per specification
    conditions: dict[str, Any] = Field(default_factory=dict, alias="condiciones")  # ambient temperature…
    uncertainty: dict[str, Any] = Field(default_factory=dict, alias="incertidumbre")  # file, k and summary per trace
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

    @property
    def title(self) -> str:
        return self.name or self.id

    @property
    def dut_id(self) -> str | None:
        return self.dut.get("id") or self.template.get("eut")

    @property
    def template_id(self) -> str | None:
        return self.procedure.get("id") or self.template.get("id")

    def launched_template(self) -> ProcedureTemplate | None:
        """The test template as it was launched (``None`` if the record does not keep it)."""
        if not self.template:
            return None
        try:
            return ProcedureTemplate.model_validate(self.template)
        except ValueError:
            return None

    def to_yaml_dict(self) -> dict[str, Any]:
        return self.model_dump(by_alias=True, exclude_none=True, mode="json")
