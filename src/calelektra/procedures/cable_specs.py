"""Pass / fail of a cable from the attenuation formula of its model and the VSWR of its connectors.

The other way to give the specifications of a cable (besides the S2P files of
``procedures/specs.py``). Fields of the model of the cable (category «cable»):

* ``k1_atenuacion``, ``k2_atenuacion``: K1 and K2 of the attenuation
  A = K1·√f + K2·f, in dB per reference length;
* ``unidad_frecuencia_k``: unit of f in the formula (Hz, kHz, MHz, GHz);
* ``longitud_normalizacion_k``: reference length of K1 and K2 (1 m, 100 m,
  1 ft, 100 ft, 1 in);
* ``vswr_conector_a``, ``vswr_conector_b``: VSWR of each connector (B = A if
  it is not given);
* ``longitud_m``: length of the cable (of each unit).

With the tolerances of the test template (``tolerancias_cable``):

    A(f)        = (K1·√(f/u) + K2·(f/u)) · L / L_ref                       nominal attenuation (dB)
    IL_max(f)   = A(f) + t_IL                                              |S21|, |S12| ≥ −IL_max(f)
    |Γ_i|       = (VSWR_i − 1) / (VSWR_i + 1)
    |Γ_cable|   = |Γ_A| + |Γ_B|                                            linear sum: what each connector returns
    VSWR_cable  = (1 + |Γ_cable|) / (1 − |Γ_cable|)
    VSWR_max    = VSWR_cable · (1 + t_VSWR/100)
    RL_min      = −20·log10((VSWR_max − 1) / (VSWR_max + 1))              |S11|, |S22| ≤ −RL_min

The second connector returns its reflection in full: the cable is assumed to
let it through (no attenuation on the way back), the worst case. The limits
are evaluated inside the nominal range of the cable.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from calelektra.core.frequency import FrequencyRange
from calelektra.core.units import format_frequency
from calelektra.procedures.specs import ParameterResult, SpecCurve

FREQUENCY_UNITS = {"Hz": 1.0, "kHz": 1e3, "MHz": 1e6, "GHz": 1e9}
LENGTH_UNITS = {"1 m": 1.0, "100 m": 100.0, "1 ft": 0.3048, "100 ft": 30.48, "1 in": 0.0254}  # metres
CABLE_FIELDS = (
    "k1_atenuacion",
    "k2_atenuacion",
    "unidad_frecuencia_k",
    "longitud_normalizacion_k",
    "vswr_conector_a",
    "vswr_conector_b",
    "longitud_m",
)
INSERTION_LOSS = "Pérdidas de inserción máximas (fórmula K1, K2)"
RETURN_LOSS = "Pérdidas de retorno mínimas (VSWR de los conectores)"


def gamma_of_vswr(vswr: float) -> float:
    return (vswr - 1) / (vswr + 1)


def vswr_of_gamma(gamma: float) -> float:
    return (1 + gamma) / (1 - gamma) if gamma < 1 else math.inf


@dataclass
class CableSpecification:
    """Nominal values of a cable (fields of its model and unit) and the tolerances of the test template."""

    k1: float | None = None
    k2: float | None = None
    frequency_unit: str | None = None
    length_unit: str | None = None
    length_m: float | None = None
    vswr_a: float | None = None
    vswr_b: float | None = None
    insertion_loss_tolerance_db: float = 0.0
    vswr_tolerance_pct: float = 0.0
    nominal_range: FrequencyRange | None = None
    problems: list[str] = field(default_factory=list)  # why a part is not evaluated

    # ── attenuation ────────────────────────────────────────────────────────
    @property
    def has_attenuation(self) -> bool:
        return (
            (self.k1 is not None or self.k2 is not None)
            and self.length_m is not None
            and self.frequency_unit in FREQUENCY_UNITS
            and self.length_unit in LENGTH_UNITS
        )

    def nominal_attenuation(self, frequencies: np.ndarray) -> np.ndarray:
        """A(f) in dB for the length of the cable."""
        f = np.maximum(np.asarray(frequencies, dtype=float), 0.0) / FREQUENCY_UNITS[str(self.frequency_unit)]
        per_reference = (self.k1 or 0.0) * np.sqrt(f) + (self.k2 or 0.0) * f
        return per_reference * float(self.length_m or 0.0) / LENGTH_UNITS[str(self.length_unit)]

    def max_insertion_loss(self, frequencies: np.ndarray) -> np.ndarray:
        return self.nominal_attenuation(frequencies) + self.insertion_loss_tolerance_db

    # ── VSWR ───────────────────────────────────────────────────────────────
    @property
    def connector_vswr(self) -> tuple[float, float] | None:
        if self.vswr_a is None and self.vswr_b is None:
            return None
        a = self.vswr_a if self.vswr_a is not None else self.vswr_b
        b = self.vswr_b if self.vswr_b is not None else a
        return float(a), float(b)  # type: ignore[arg-type]

    @property
    def cable_gamma(self) -> float | None:
        vswr = self.connector_vswr
        return None if vswr is None else gamma_of_vswr(vswr[0]) + gamma_of_vswr(vswr[1])

    @property
    def cable_vswr(self) -> float | None:
        gamma = self.cable_gamma
        return None if gamma is None else vswr_of_gamma(gamma)

    @property
    def max_vswr(self) -> float | None:
        vswr = self.cable_vswr
        return None if vswr is None else vswr * (1 + self.vswr_tolerance_pct / 100)

    @property
    def has_return_loss(self) -> bool:
        return self.max_vswr is not None and math.isfinite(self.max_vswr)

    @property
    def min_return_loss_db(self) -> float | None:
        vswr = self.max_vswr
        return None if vswr is None or not math.isfinite(vswr) else -20 * math.log10(gamma_of_vswr(vswr))

    # ── descriptions ───────────────────────────────────────────────────────
    def formula_text(self) -> str:
        return (
            f"A = ({self.k1 or 0:g}·√f + {self.k2 or 0:g}·f) dB/{self.length_unit} con f en {self.frequency_unit}, "
            f"L = {self.length_m:g} m"
        )

    def insertion_loss_criterion(self) -> str:
        return f"IL ≤ A(f) + {self.insertion_loss_tolerance_db:g} dB; {self.formula_text()}"

    def return_loss_criterion(self) -> str:
        vswr = self.connector_vswr
        assert vswr is not None and self.cable_vswr is not None and self.max_vswr is not None
        return (
            f"VSWR ≤ {self.max_vswr:.3f} (RL ≥ {self.min_return_loss_db:.2f} dB): conectores {vswr[0]:g} y {vswr[1]:g} → "
            f"cable {self.cable_vswr:.3f} + {self.vswr_tolerance_pct:g} %"
        )

    def to_record(self) -> dict[str, Any]:
        """As kept in the test record (``especificacion_cable``)."""
        data: dict[str, Any] = {
            "k1": self.k1,
            "k2": self.k2,
            "unidad_frecuencia": self.frequency_unit,
            "longitud_normalizacion": self.length_unit,
            "longitud_m": self.length_m,
            "vswr_conector_a": self.vswr_a,
            "vswr_conector_b": self.vswr_b,
            "tolerancia_il_db": self.insertion_loss_tolerance_db,
            "tolerancia_vswr_pct": self.vswr_tolerance_pct,
        }
        if self.has_return_loss:
            data.update(vswr_cable=round(float(self.cable_vswr), 5), vswr_max=round(float(self.max_vswr), 5),
                        rl_min_db=round(float(self.min_return_loss_db), 4))
        if self.nominal_range is not None:
            data["rango_hz"] = [self.nominal_range.start, self.nominal_range.stop]
        return {key: value for key, value in data.items() if value is not None}

    @classmethod
    def from_record(cls, data: dict[str, Any]) -> CableSpecification | None:
        if not data:
            return None
        span = data.get("rango_hz")
        try:
            nominal_range = FrequencyRange(float(span[0]), float(span[1])) if span else None
        except (TypeError, ValueError, IndexError):
            nominal_range = None
        return cls(
            k1=data.get("k1"),
            k2=data.get("k2"),
            frequency_unit=data.get("unidad_frecuencia"),
            length_unit=data.get("longitud_normalizacion"),
            length_m=data.get("longitud_m"),
            vswr_a=data.get("vswr_conector_a"),
            vswr_b=data.get("vswr_conector_b"),
            insertion_loss_tolerance_db=float(data.get("tolerancia_il_db", 0.0)),
            vswr_tolerance_pct=float(data.get("tolerancia_vswr_pct", 0.0)),
            nominal_range=nominal_range,
        )

    # ── charts and pass / fail ─────────────────────────────────────────────
    def _inside(self, frequencies: np.ndarray) -> np.ndarray:
        if self.nominal_range is None:
            return np.ones(len(frequencies), dtype=bool)
        return (frequencies >= self.nominal_range.start * (1 - 1e-12)) & (frequencies <= self.nominal_range.stop * (1 + 1e-12))

    def curves(self, traces: tuple[str, ...], frequencies: np.ndarray | None = None) -> list[SpecCurve]:
        """Limit lines of the chart (|S| in dB), over ``frequencies`` or the nominal range of the cable."""
        if frequencies is None:
            if self.nominal_range is None:
                return []
            frequencies = np.linspace(self.nominal_range.start, self.nominal_range.stop, 801)
        f = np.asarray(frequencies, dtype=float)
        f = f[self._inside(f)]
        curves = []
        if self.has_attenuation and len(f):
            limit = -self.max_insertion_loss(f)
            curves += [SpecCurve(INSERTION_LOSS, p, "magnitud_db", "minimo", None, f, limit) for p in ("S21", "S12") if p in traces]
        if self.has_return_loss and len(f):
            limit = np.full(len(f), -float(self.min_return_loss_db))
            curves += [SpecCurve(RETURN_LOSS, p, "magnitud_db", "maximo", None, f, limit) for p in ("S11", "S22") if p in traces]
        return curves

    def evaluate(self, frequencies: np.ndarray, s: np.ndarray, traces: tuple[str, ...]) -> list[dict[str, Any]]:
        """Entries of the record (``evaluacion``): insertion loss and return loss, as the S2P specifications."""
        frequencies = np.asarray(frequencies, dtype=float)
        inside = self._inside(frequencies)
        entries = []
        if self.has_attenuation:
            limit = -self.max_insertion_loss(frequencies)
            parameters = [_compare(p, frequencies, s, limit, "minimo", inside) for p in ("S21", "S12") if p in traces]
            entries.append(_entry(INSERTION_LOSS, self.insertion_loss_criterion(), parameters))
        if self.has_return_loss:
            limit = np.full(len(frequencies), -float(self.min_return_loss_db))
            parameters = [_compare(p, frequencies, s, limit, "maximo", inside) for p in ("S11", "S22") if p in traces]
            entries.append(_entry(RETURN_LOSS, self.return_loss_criterion(), parameters))
        return entries


def cable_specification(
    fields: dict[str, Any], insertion_loss_tolerance_db: float | None, vswr_tolerance_pct: float | None, nominal_range: FrequencyRange | None
) -> CableSpecification | None:
    """The specification of a cable from the fields of its model and unit (``None`` if it has no formula nor VSWR)."""
    if all(fields.get(key) is None for key in ("k1_atenuacion", "k2_atenuacion", "vswr_conector_a", "vswr_conector_b")):
        return None
    spec = CableSpecification(
        k1=_number(fields.get("k1_atenuacion")),
        k2=_number(fields.get("k2_atenuacion")),
        frequency_unit=fields.get("unidad_frecuencia_k"),
        length_unit=fields.get("longitud_normalizacion_k"),
        length_m=_number(fields.get("longitud_m")),
        vswr_a=_number(fields.get("vswr_conector_a")),
        vswr_b=_number(fields.get("vswr_conector_b")),
        insertion_loss_tolerance_db=float(insertion_loss_tolerance_db or 0.0),
        vswr_tolerance_pct=float(vswr_tolerance_pct or 0.0),
        nominal_range=nominal_range,
    )
    if spec.k1 is not None or spec.k2 is not None:
        if spec.frequency_unit not in FREQUENCY_UNITS:
            spec.problems.append("falta la unidad de frecuencia de K1 y K2: no se evalúan las pérdidas de inserción")
        if spec.length_unit not in LENGTH_UNITS:
            spec.problems.append("falta la longitud a la que están normalizados K1 y K2: no se evalúan las pérdidas de inserción")
        if spec.length_m is None:
            spec.problems.append("falta la longitud del cable: no se evalúan las pérdidas de inserción")
    vswr = spec.connector_vswr
    if vswr is not None:
        if min(vswr) < 1:
            spec.vswr_a = spec.vswr_b = None
            spec.problems.append("el VSWR de un conector es menor que 1: no se evalúan las pérdidas de retorno")
        elif spec.cable_gamma is not None and spec.cable_gamma >= 1:
            spec.vswr_a = spec.vswr_b = None
            spec.problems.append("la suma de las reflexiones de los conectores llega a 1: no se evalúan las pérdidas de retorno")
    return spec


def _number(value: Any) -> float | None:
    return None if value is None else float(value)


def _compare(parameter: str, frequencies: np.ndarray, s: np.ndarray, limit: np.ndarray, criterion: str, inside: np.ndarray) -> ParameterResult:
    i, j = int(parameter[1]) - 1, int(parameter[2]) - 1
    if i >= s.shape[1] or j >= s.shape[1]:
        return ParameterResult(parameter, None)
    with np.errstate(divide="ignore"):
        values = 20 * np.log10(np.maximum(np.abs(s[:, i, j]), 1e-15))
    valid = inside & np.isfinite(values) & np.isfinite(limit)
    if not valid.any():
        return ParameterResult(parameter, None, not_evaluated=len(frequencies))
    margin = values[valid] - limit[valid] if criterion == "minimo" else limit[valid] - values[valid]
    worst = int(np.argmin(margin))
    failed = int(np.sum(margin < 0))
    f = frequencies[valid]
    return ParameterResult(
        parameter, failed == 0, evaluated=int(valid.sum()), failed=failed, not_evaluated=int((~valid).sum()),
        worst_margin=float(margin[worst]), worst_frequency=float(f[worst]), frequencies=f, limit=limit[valid],
    )


def _state(passed: bool | None) -> str:
    return "PASA" if passed else ("FALLA" if passed is False else "NO_EVALUADO")


def _entry(name: str, criterion: str, parameters: list[ParameterResult]) -> dict[str, Any]:
    states = [p.passed for p in parameters]
    passed = False if any(state is False for state in states) else (True if states and all(states) else None)
    return {
        "nombre": name,
        "criterio": criterion,
        "unidad": "dB",
        "resultado": _state(passed),
        "margen_minimo": min((round(p.worst_margin, 4) for p in parameters if p.worst_margin is not None), default=None),
        "parametros": [
            {
                "parametro": p.parameter,
                "resultado": _state(p.passed),
                "margen_minimo": None if p.worst_margin is None else round(p.worst_margin, 4),
                "frecuencia": None if p.worst_frequency is None else format_frequency(p.worst_frequency, decimal="."),
                "evaluados": p.evaluated,
                "fallos": p.failed,
                "fuera_de_rango": p.not_evaluated,
            }
            for p in parameters
        ],
        "detalle": [p.text("dB") for p in parameters] or ["Ningún parámetro medido"],
    }
