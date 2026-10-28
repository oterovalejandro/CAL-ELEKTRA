"""Pass / fail of a cable from the attenuation formula of its model and the VSWR of its connectors.

The other way to give the specifications of a cable (besides the S2P files of
``procedures/specs.py``). Fields of the model of the cable (category «cable»):

* ``k1_atenuacion``, ``k2_atenuacion``: K1 and K2 of the attenuation
  A = K1·√f + K2·f, in dB per reference length;
* ``unidad_frecuencia_k``: unit of f in the formula (Hz, kHz, MHz, GHz);
* ``longitud_normalizacion_k``: reference length of K1 and K2 (1 m, 100 m,
  1 ft, 100 ft, 1 in);
* ``vswr_conector_a``, ``vswr_conector_b``: VSWR of each connector, one value
  or one per frequency band (``core/bands.py``; B = A if it is not given);
* ``longitud_m``: length of the cable (of each unit).

With the tolerances of the test template (``tolerancias_cable``):

    A(f)          = (K1·√(f/u) + K2·(f/u)) · L / L_ref                     nominal attenuation (dB)
    IL_max(f)     = A(f) + t_IL                                            |S21|, |S12| ≥ −IL_max(f)
    |Γ_i|         = (VSWR_i − 1) / (VSWR_i + 1)                            each connector (i = A, B)
    VSWR_cable(f) = (1 + |Γ_A| + |Γ_B|) / (1 − |Γ_A| − |Γ_B|)              the assembled cable: linear sum
    VSWR_max(f)   = VSWR_cable(f) · (1 + t_VSWR/100)                       tolerance on the assembled cable
    RL_min(f)     = −20·log10 |Γ_max|,  |Γ_max| = (VSWR_max − 1)/(VSWR_max + 1)    |S11|, |S22| ≤ −RL_min

The laboratory applies the tolerance to the VSWR of the assembled cable
(``vswr_sobre: cable``, the default). ``vswr_sobre: conectores`` applies it to
each connector before the sum, VSWR_i' = VSWR_i · (1 + t_VSWR/100): the limits
of the example certificate of the laboratory (1.38596 up to 18 GHz, 1.48934 up
to 26.5 GHz with 1.12 / 1.16 and 5 %) come out that way. The records keep the
limit they were evaluated with (``vswr_max``) and how (``tolerancia_vswr_sobre``).
The second connector returns its reflection in full: the cable is assumed to
let it through (no attenuation on the way back), the worst case. The limits
are evaluated inside the nominal range of the cable.

The nominal delay of the cable (``CableDelay``) is only informative: the delay
per metre of the datasheet (``retardo_ns_m``) or, without it, the one of its
velocity of propagation (``velocidad_propagacion_pct``: 1/(v·c)), times its
length, next to the delay measured from the phase of S21.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from calelektra.core.bands import BandValues, merge_limits
from calelektra.core.frequency import FrequencyRange
from calelektra.core.units import format_frequency
from calelektra.procedures.specs import ParameterResult, SpecCurve

C0 = 299_792_458.0  # m/s
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
VSWR_TOLERANCE_ON = {"cable": "sobre el VSWR del cable ensamblado", "conectores": "sobre el VSWR de cada conector"}
INSERTION_LOSS = "Pérdidas de inserción máximas (fórmula K1, K2)"
RETURN_LOSS = "Pérdidas de retorno mínimas (VSWR de los conectores)"


def gamma_of_vswr(vswr):
    """|Γ| of a VSWR (number or array)."""
    return (vswr - 1) / (vswr + 1)


def vswr_of_gamma(gamma):
    """VSWR of a |Γ| (number or array; infinite from |Γ| = 1, NaN where |Γ| is NaN)."""
    if np.ndim(gamma):
        g = np.asarray(gamma, dtype=float)
        with np.errstate(divide="ignore", invalid="ignore"):
            return np.where(np.isnan(g), np.nan, np.where(g < 1, (1 + g) / (1 - g), np.inf))
    return (1 + gamma) / (1 - gamma) if gamma < 1 else math.inf


def _decimal(value: float, digits: int) -> str:
    return f"{value:.{digits}f}".rstrip("0").rstrip(".")


def bands_text(bands: BandValues, digits: int = 3) -> str:
    """``1.386 hasta 18 GHz; 1.489 hasta 26.5 GHz`` (point as decimal separator, as in the record)."""
    return "; ".join(
        _decimal(value, digits) + (f" hasta {format_frequency(upper, decimal='.')}" if upper is not None else "") for upper, value in bands.bands
    )


@dataclass
class CableSpecification:
    """Nominal values of a cable (fields of its model and unit) and the tolerances of the test template."""

    k1: float | None = None
    k2: float | None = None
    frequency_unit: str | None = None
    length_unit: str | None = None
    length_m: float | None = None
    vswr_a: BandValues | None = None
    vswr_b: BandValues | None = None
    insertion_loss_tolerance_db: float = 0.0
    vswr_tolerance_pct: float = 0.0
    vswr_tolerance_on: str = "cable"  # cable (the assembled cable) | conectores (each connector, before the sum)
    nominal_range: FrequencyRange | None = None
    problems: list[str] = field(default_factory=list)  # why a part is not evaluated
    stored_max_vswr: BandValues | None = None  # the limit kept in a record: drawn as it was evaluated

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
    def connector_vswr(self) -> tuple[BandValues, BandValues] | None:
        if self.vswr_a is None and self.vswr_b is None:
            return None
        a = self.vswr_a if self.vswr_a is not None else self.vswr_b
        b = self.vswr_b if self.vswr_b is not None else a
        return a, b  # type: ignore[return-value]

    def cable_gamma(self, frequencies: np.ndarray) -> np.ndarray:
        """|Γ_A| + |Γ_B| of the connectors as declared (without tolerance)."""
        pair = self.connector_vswr
        f = np.asarray(frequencies, dtype=float)
        if pair is None:
            return np.full(f.shape, np.nan)
        return gamma_of_vswr(pair[0].at(f)) + gamma_of_vswr(pair[1].at(f))

    def cable_vswr(self, frequencies: np.ndarray) -> np.ndarray:
        return vswr_of_gamma(self.cable_gamma(frequencies))

    def max_gamma(self, frequencies: np.ndarray) -> np.ndarray:
        """|Γ_max| of VSWR_max: the tolerance on the assembled cable or on each connector (NaN where there is no VSWR)."""
        f = np.asarray(frequencies, dtype=float)
        if self.stored_max_vswr is not None:
            return gamma_of_vswr(self.stored_max_vswr.at(f))
        pair = self.connector_vswr
        if pair is None:
            return np.full(f.shape, np.nan)
        factor = 1 + self.vswr_tolerance_pct / 100
        if self.vswr_tolerance_on == "conectores":
            return gamma_of_vswr(pair[0].at(f) * factor) + gamma_of_vswr(pair[1].at(f) * factor)
        with np.errstate(invalid="ignore"):  # connectors summing |Γ| ≥ 1: no limit (NaN)
            return gamma_of_vswr(self.cable_vswr(f) * factor)

    def max_vswr(self, frequencies: np.ndarray) -> np.ndarray:
        return vswr_of_gamma(self.max_gamma(frequencies))

    def min_return_loss_db(self, frequencies: np.ndarray) -> np.ndarray:
        with np.errstate(divide="ignore", invalid="ignore"):
            return -20 * np.log10(self.max_gamma(frequencies))

    def _band_values(self, function) -> BandValues | None:
        """``function(f)`` on each band of the VSWR of the connectors (the value changes only at their edges)."""
        pair = self.connector_vswr
        if pair is None:
            return None
        uppers = merge_limits(*pair)
        points: list[float | None] = list(uppers)
        if pair[0].bands[-1][0] is None and pair[1].bands[-1][0] is None:
            points.append(None)
        bands = []
        for upper in points:
            probe = upper if upper is not None else (uppers[-1] * 2 if uppers else 1.0)
            bands.append((upper, float(function(np.array([probe]))[0])))
        return BandValues(tuple(bands))

    def cable_vswr_bands(self) -> BandValues | None:
        """VSWR of the cable as declared (connectors without tolerance), by bands."""
        return self._band_values(self.cable_vswr)

    def max_vswr_bands(self) -> BandValues | None:
        """VSWR_max by bands: the edges of the bands of the connectors."""
        return self.stored_max_vswr if self.stored_max_vswr is not None else self._band_values(self.max_vswr)

    @property
    def has_return_loss(self) -> bool:
        bands = self.max_vswr_bands()
        return bands is not None and all(math.isfinite(value) for value in bands.values)

    # ── descriptions ───────────────────────────────────────────────────────
    def formula_text(self) -> str:
        return (
            f"A = ({self.k1 or 0:g}·√f + {self.k2 or 0:g}·f) dB/{self.length_unit} con f en {self.frequency_unit}, "
            f"L = {self.length_m:g} m"
        )

    def insertion_loss_criterion(self) -> str:
        return f"IL ≤ A(f) + {self.insertion_loss_tolerance_db:g} dB; {self.formula_text()}"

    def return_loss_criterion(self) -> str:
        bands = self.max_vswr_bands()
        assert bands is not None
        pair = self.connector_vswr
        if bands.is_single:
            rl = -20 * math.log10(gamma_of_vswr(bands.values[0]))
            text = f"VSWR ≤ {bands.values[0]:.3f} (RL ≥ {rl:.2f} dB)"
        else:
            text = f"VSWR ≤ {bands_text(bands)}"
        if pair is None:
            return text
        connectors = bands_text(pair[0]) if pair[0] == pair[1] else f"{bands_text(pair[0])} y {bands_text(pair[1])}"
        if self.vswr_tolerance_on == "conectores":
            return f"{text}: conectores {connectors} + {self.vswr_tolerance_pct:g} % cada uno, reflexiones sumadas"
        cable = self.cable_vswr_bands()
        assert cable is not None
        return f"{text}: cable {bands_text(cable)} (conectores {connectors}, reflexiones sumadas) + {self.vswr_tolerance_pct:g} %"

    def to_record(self) -> dict[str, Any]:
        """As kept in the test record (``especificacion_cable``)."""
        data: dict[str, Any] = {
            "k1": self.k1,
            "k2": self.k2,
            "unidad_frecuencia": self.frequency_unit,
            "longitud_normalizacion": self.length_unit,
            "longitud_m": self.length_m,
            "vswr_conector_a": self.vswr_a.to_yaml() if self.vswr_a is not None else None,
            "vswr_conector_b": self.vswr_b.to_yaml() if self.vswr_b is not None else None,
            "tolerancia_il_db": self.insertion_loss_tolerance_db,
            "tolerancia_vswr_pct": self.vswr_tolerance_pct,
        }
        if self.has_return_loss:
            bands = self.max_vswr_bands()
            assert bands is not None
            data["tolerancia_vswr_sobre"] = self.vswr_tolerance_on
            if self.stored_max_vswr is None:
                data["vswr_cable"] = self.cable_vswr_bands().map(lambda v: round(v, 5)).to_yaml()
            data["vswr_max"] = bands.map(lambda v: round(v, 5)).to_yaml()
            data["rl_min_db"] = bands.map(lambda v: round(-20 * math.log10(gamma_of_vswr(v)), 4)).to_yaml()
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
            vswr_a=_bands(data.get("vswr_conector_a")),
            vswr_b=_bands(data.get("vswr_conector_b")),
            insertion_loss_tolerance_db=float(data.get("tolerancia_il_db", 0.0)),
            vswr_tolerance_pct=float(data.get("tolerancia_vswr_pct", 0.0)),
            vswr_tolerance_on=str(data.get("tolerancia_vswr_sobre", "cable")),
            nominal_range=nominal_range,
            stored_max_vswr=_bands(data.get("vswr_max")),
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
            limit = -self.min_return_loss_db(f)
            keep = np.isfinite(limit)
            curves += [SpecCurve(RETURN_LOSS, p, "magnitud_db", "maximo", None, f[keep], limit[keep]) for p in ("S11", "S22") if p in traces]
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
            limit = -self.min_return_loss_db(frequencies)
            parameters = [_compare(p, frequencies, s, limit, "maximo", inside) for p in ("S11", "S22") if p in traces]
            entries.append(_entry(RETURN_LOSS, self.return_loss_criterion(), parameters))
        return entries


DELAY_FIELDS = ("retardo_ns_m", "velocidad_propagacion_pct", "longitud_m")


def delay_of_velocity(velocity_pct: float) -> float:
    """Delay per metre (ns/m) of a velocity of propagation (% of c)."""
    return 1e9 / (velocity_pct / 100 * C0)


@dataclass
class CableDelay:
    """Nominal delay of a cable (informative): delay per metre × length."""

    per_metre_ns: float
    source: str  # where the delay per metre comes from
    length_m: float
    problems: list[str] = field(default_factory=list)

    @property
    def nominal_ns(self) -> float:
        return self.per_metre_ns * self.length_m

    def text(self) -> str:
        return f"{self.per_metre_ns:g} ns/m ({self.source}) × {self.length_m:g} m = {self.nominal_ns:.3f} ns"

    def summary(self, measured_ns: float | None) -> dict[str, Any]:
        """``resultados.retardo_nominal`` of the record: nominal, difference and the length the measured delay gives."""
        data: dict[str, Any] = {
            "ns_por_metro": round(self.per_metre_ns, 4),
            "origen": self.source,
            "longitud_m": self.length_m,
            "nominal_ns": round(self.nominal_ns, 4),
        }
        if measured_ns is not None and math.isfinite(measured_ns):
            data["diferencia_ns"] = round(measured_ns - self.nominal_ns, 4) + 0.0  # no «-0.0»
            data["diferencia_pct"] = round(100 * (measured_ns - self.nominal_ns) / self.nominal_ns, 3) + 0.0
            data["longitud_estimada_m"] = round(measured_ns / self.per_metre_ns, 4)
        return data


def cable_delay(fields: dict[str, Any]) -> CableDelay | None:
    """The nominal delay of a cable from its fields (``None`` without delay, velocity or length)."""
    delay, velocity, length = (_number(fields.get(key)) for key in DELAY_FIELDS)
    if length is None or (delay is None and not velocity):
        return None
    problems = []
    if delay is not None:
        per_metre, source = delay, "retardo nominal"
        if velocity:
            from_velocity = delay_of_velocity(velocity)
            if abs(from_velocity - delay) > 0.02 * delay:
                problems.append(
                    f"el retardo nominal ({delay:g} ns/m) no concuerda con la velocidad de propagación "
                    f"({velocity:g} % → {from_velocity:.3f} ns/m)"
                )
    else:
        per_metre, source = delay_of_velocity(float(velocity)), f"velocidad de propagación {velocity:g} %"
    return CableDelay(per_metre, source, length, problems)


def cable_specification(
    fields: dict[str, Any],
    insertion_loss_tolerance_db: float | None,
    vswr_tolerance_pct: float | None,
    nominal_range: FrequencyRange | None,
    vswr_tolerance_on: str = "cable",
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
        vswr_a=_bands(fields.get("vswr_conector_a")),
        vswr_b=_bands(fields.get("vswr_conector_b")),
        insertion_loss_tolerance_db=float(insertion_loss_tolerance_db or 0.0),
        vswr_tolerance_pct=float(vswr_tolerance_pct or 0.0),
        vswr_tolerance_on=vswr_tolerance_on,
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
        if min(vswr[0].values + vswr[1].values) < 1:
            spec.vswr_a = spec.vswr_b = None
            spec.problems.append("el VSWR de un conector es menor que 1: no se evalúan las pérdidas de retorno")
        elif not spec.has_return_loss:
            spec.vswr_a = spec.vswr_b = None
            spec.problems.append("la suma de las reflexiones de los conectores llega a 1: no se evalúan las pérdidas de retorno")
    return spec


def _number(value: Any) -> float | None:
    return None if value is None else float(value)


def _bands(value: Any) -> BandValues | None:
    return None if value is None else BandValues.parse(value)


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
