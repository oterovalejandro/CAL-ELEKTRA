"""Validation findings shared by the catalogue, the setups and the GUI."""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum


class Severity(IntEnum):
    INFO = 0
    WARNING = 1
    ERROR = 2

    @property
    def label(self) -> str:
        return {Severity.INFO: "Información", Severity.WARNING: "Aviso", Severity.ERROR: "Error"}[self]


@dataclass(frozen=True)
class Issue:
    severity: Severity
    code: str
    message: str
    role: str | None = None
    equipment_id: str | None = None
    segment: str | None = None
    source: str | None = None  # file the finding refers to, if any

    def __str__(self) -> str:
        return f"[{self.severity.label}] {self.message}"


def has_errors(issues: list[Issue]) -> bool:
    return any(issue.severity is Severity.ERROR for issue in issues)
