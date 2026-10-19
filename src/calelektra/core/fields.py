"""Pydantic field types shared by the data models."""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import BeforeValidator, PlainSerializer

from calelektra.core.frequency import FrequencyRange


def _as_list(value: Any) -> Any:
    if value is None:
        return []
    if isinstance(value, (str, int, float)):
        return [value]
    return value


FrequencyRangeField = Annotated[
    FrequencyRange,
    BeforeValidator(FrequencyRange.parse),
    PlainSerializer(lambda r: r.to_yaml(), return_type=list),
]

# Accepts a single value or a list in YAML, always a list in Python.
StrList = Annotated[list[str], BeforeValidator(_as_list)]
