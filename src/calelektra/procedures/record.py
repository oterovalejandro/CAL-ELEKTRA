"""Test («ensayo»): its definition and, once run, the record of what was measured, with what and how.

A test is launched from a test template (procedure) in a folder chosen by the
user. It is saved as ``<carpeta>/<id>/ensayo.yaml``: first with its
definition (status ``preparado``: template snapshot, hardware setup, cable to
calibrate, specifications) and, after the run, with the results, next to the
result files (.s2p/.s1p, CSV, specification files used and the instrument
log). So a calibration can be traced back to the equipment, its
certificates and S-parameter references, the VNA calibration and the
settings used.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr

from calelektra.procedures.model import ProcedureTemplate

RECORD_FILE = "ensayo.yaml"
STATUS_NAMES = {
    "preparado": "Preparado",
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


class TestDefinition(BaseModel):
    """What the test measures: template (snapshot), hardware setup and cable to calibrate."""

    __test__ = False  # not a pytest test class
    model_config = ConfigDict(populate_by_name=True, extra="allow")

    procedure_id: str = Field(alias="procedimiento")
    setup_id: str = Field(alias="setup")
    dut_id: str | None = Field(default=None, alias="dut")
    procedure: dict[str, Any] = Field(default_factory=dict, alias="configuracion")  # test template as launched
    notes: str | None = Field(default=None, alias="notas")

    def template(self) -> ProcedureTemplate:
        return ProcedureTemplate.model_validate(self.procedure)

    @classmethod
    def from_template(cls, procedure: ProcedureTemplate, setup_id: str, dut_id: str | None, notes: str | None = None) -> "TestDefinition":
        return cls(procedure_id=procedure.id, setup_id=setup_id, dut_id=dut_id, procedure=procedure.to_yaml_dict(), notes=notes)


class TestRecord(BaseModel):
    __test__ = False  # not a pytest test class
    model_config = ConfigDict(populate_by_name=True, extra="allow")

    format: str = Field(default="calelektra/ensayo@0.3", alias="formato")
    id: str
    name: str | None = Field(default=None, alias="nombre")
    status: str = Field(default="preparado", alias="estado")
    message: str | None = Field(default=None, alias="mensaje")
    simulated: bool = Field(default=False, alias="simulado")
    operator: str | None = Field(default=None, alias="operador")
    created: datetime | None = Field(default=None, alias="creado")
    started: datetime = Field(alias="inicio")
    finished: datetime | None = Field(default=None, alias="fin")
    definition: TestDefinition | None = Field(default=None, alias="definicion")
    verdict: str | None = Field(default=None, alias="veredicto")  # PASA | FALLA | NO_EVALUADO | SIN_ESPECIFICACIONES
    spec_results: list[dict[str, Any]] = Field(default_factory=list, alias="especificaciones")
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

    @property
    def title(self) -> str:
        return self.name or self.id

    @property
    def dut_id(self) -> str | None:
        if self.dut:
            return self.dut.get("id")
        return self.definition.dut_id if self.definition else None

    def to_yaml_dict(self) -> dict[str, Any]:
        return self.model_dump(by_alias=True, exclude_none=True, mode="json")
