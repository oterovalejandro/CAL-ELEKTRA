"""Results of a two-port (a cable) by direction, as its calibration certificate gives them.

* Fwd, from port 1 to port 2: VSWR of port 1 (S11), insertion loss S21 and the group delay of S21.
* Rev, from port 2 to port 1: VSWR of port 2 (S22), insertion loss S12 and the group delay of S12.

At each frequency, with the expanded uncertainties of the traces
(``uncertainty/traces.py``: magnitude in dB and phase in degrees, all with the
same coverage factor):

    VSWR = (1 + ρ)/(1 − ρ),  ρ = |Sii|      U_ρ = ρ·ln10/20·U_dB(Sii)     U_VSWR = 2·U_ρ/(1 − ρ)²
    IL   = 20·log10|Sji|  (dB, negative)     U_IL = U_dB(Sji)
    τ_g  = −dφ/dω of Sji (``group_delay``)   U_τ = U_φ(Sji) / (360°·f)

and the limits of the specification by formula of the cable, when it has one,
inside its nominal range: VSWR_max(f) and −IL_max(f). The algorithms are in
``docs/10_algoritmos_de_incertidumbre.md``.

The table of the certificate (``report_rows``) has a row per report
frequency, with the values of the nearest measured point. With a smoothing
(``informe.suavizado_pct``, the laboratory: 1 %), its VSWR and group delay are
those of the laboratory: the VSWR and the delay between consecutive points
(``step_delay``) averaged over int(pct/100·N) points (``moving_average``); the
insertion loss and the uncertainties are those of the point.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from calelektra.core.networks import group_delay, moving_average, step_delay
from calelektra.core.units import format_frequency, parse_frequency
from calelektra.procedures.cable_specs import CableSpecification, vswr_of_gamma
from calelektra.procedures.results import db, write_csv

DIRECTIONS = {"Fwd": ("S11", "S21"), "Rev": ("S22", "S12")}  # reflection of the port, transmission
INDEX = {"S11": (0, 0), "S21": (1, 0), "S12": (0, 1), "S22": (1, 1)}
PORT = {"S11": 1, "S22": 2}
DEFAULT_APERTURE = 10  # frequency steps of the group delay
DEFAULT_REPORT_POINTS = 21
LN10_20 = math.log(10) / 20


def results_csv_path(result_path: Path) -> Path:
    """``<result>_fwd_rev.csv`` next to the result Touchstone file."""
    return result_path.with_name(f"{result_path.stem}_fwd_rev.csv")


def _bands(uncertainty: Mapping[str, Any] | None, parameter: str, points: int) -> tuple[np.ndarray, np.ndarray]:
    """(U dB, U °) of a trace from a ``TraceUncertainty`` or a pair of arrays; NaN without uncertainty."""
    trace = (uncertainty or {}).get(parameter)
    if trace is None:
        return np.full(points, np.nan), np.full(points, np.nan)
    if hasattr(trace, "magnitude_db"):
        return np.asarray(trace.magnitude_db, dtype=float), np.asarray(trace.phase_deg, dtype=float)
    return np.asarray(trace[0], dtype=float), np.asarray(trace[1], dtype=float)


@dataclass
class DirectionResults:
    name: str  # Fwd | Rev
    reflection: str  # S11 | S22
    transmission: str  # S21 | S12
    vswr: np.ndarray
    vswr_u: np.ndarray
    vswr_limit: np.ndarray  # NaN without specification
    insertion_loss_db: np.ndarray  # 20·log10|Sji|, negative
    insertion_loss_u: np.ndarray
    insertion_loss_limit: np.ndarray  # −IL_max(f), negative; NaN without specification
    group_delay_ns: np.ndarray
    group_delay_u_ns: np.ndarray
    step_delay_ns: np.ndarray  # between consecutive points: the delay smoothed in the certificate

    @property
    def port(self) -> int:
        return PORT[self.reflection]

    def passes(self) -> np.ndarray:
        """At each point: within both limits (True), out of one (False); where there is no limit it counts as within."""
        vswr_ok = ~(self.vswr > self.vswr_limit)
        loss_ok = ~(self.insertion_loss_db < self.insertion_loss_limit)
        return vswr_ok & loss_ok

    def summary(self, frequencies: np.ndarray) -> dict[str, Any]:
        def worst(values: np.ndarray, largest: bool) -> tuple[float, int] | None:
            finite = np.isfinite(values)
            if not finite.any():
                return None
            index = int(np.nanargmax(values) if largest else np.nanargmin(values))
            return float(values[index]), index

        def largest(values: np.ndarray, digits: int) -> float | None:
            finite = values[np.isfinite(values)]
            return round(float(finite.max()), digits) if len(finite) else None

        data: dict[str, Any] = {"puerto": self.port, "reflexion": self.reflection, "transmision": self.transmission}
        found = worst(self.vswr, True)
        if found:
            data["vswr_max"] = round(found[0], 4)
            data["frecuencia_vswr_max"] = format_frequency(frequencies[found[1]], decimal=".")
        found = worst(self.insertion_loss_db, False)
        if found:
            data["perdida_insercion_peor_db"] = round(found[0], 4)
        delay = self.group_delay_ns[np.isfinite(self.group_delay_ns)]
        if len(delay):
            data["retardo_grupo_ns"] = {"media": round(float(delay.mean()), 4), "min": round(float(delay.min()), 4), "max": round(float(delay.max()), 4)}
        margins = {
            "margen_vswr": self.vswr_limit - self.vswr,
            "margen_perdida_insercion_db": self.insertion_loss_db - self.insertion_loss_limit,
        }
        for key, values in margins.items():
            found = worst(values, False)
            if found:
                data[key] = round(found[0], 4)
        for key, values, digits in (
            ("U_vswr_max", self.vswr_u, 4),
            ("U_perdida_insercion_max_db", self.insertion_loss_u, 4),
            ("U_retardo_grupo_max_ns", self.group_delay_u_ns, 4),
        ):
            value = largest(values, digits)
            if value is not None:
                data[key] = value
        return data


@dataclass
class CableResults:
    frequencies: np.ndarray
    directions: dict[str, DirectionResults]
    aperture: int = DEFAULT_APERTURE
    coverage_factor: float = 2.0

    @classmethod
    def compute(
        cls,
        frequencies: np.ndarray,
        s: np.ndarray,
        uncertainty: Mapping[str, Any] | None = None,
        spec: CableSpecification | None = None,
        aperture: int = DEFAULT_APERTURE,
        coverage_factor: float = 2.0,
    ) -> CableResults:
        """From the S-parameters (points, 2, 2), their uncertainty per trace and the specification of the cable."""
        f = np.asarray(frequencies, dtype=float)
        s = np.asarray(s)
        points = len(f)
        inside = spec._inside(f) if spec is not None else np.zeros(points, dtype=bool)
        vswr_limit = np.full(points, np.nan)
        loss_limit = np.full(points, np.nan)
        if spec is not None and spec.has_return_loss:
            vswr_limit = np.where(inside, spec.max_vswr(f), np.nan)
        if spec is not None and spec.has_attenuation:
            loss_limit = np.where(inside, -spec.max_insertion_loss(f), np.nan)
        directions = {}
        with np.errstate(divide="ignore", invalid="ignore"):
            for name, (reflection, transmission) in DIRECTIONS.items():
                rho = np.abs(s[:, INDEX[reflection][0], INDEX[reflection][1]])
                u_reflection_db, _ = _bands(uncertainty, reflection, points)
                u_rho = rho * LN10_20 * u_reflection_db
                values = s[:, INDEX[transmission][0], INDEX[transmission][1]]
                u_transmission_db, u_transmission_deg = _bands(uncertainty, transmission, points)
                directions[name] = DirectionResults(
                    name=name,
                    reflection=reflection,
                    transmission=transmission,
                    vswr=vswr_of_gamma(rho),
                    vswr_u=2 * u_rho / (1 - rho) ** 2,
                    vswr_limit=vswr_limit,
                    insertion_loss_db=db(values),
                    insertion_loss_u=u_transmission_db,
                    insertion_loss_limit=loss_limit,
                    group_delay_ns=group_delay(f, values, aperture) * 1e9,
                    group_delay_u_ns=np.where(f > 0, u_transmission_deg / (360.0 * f) * 1e9, np.nan),
                    step_delay_ns=step_delay(f, values) * 1e9,
                )
        return cls(f, directions, int(aperture), coverage_factor)

    def summary(self) -> dict[str, Any]:
        """``resultados.sentidos`` of the record."""
        data: dict[str, Any] = {name: direction.summary(self.frequencies) for name, direction in self.directions.items()}
        data["apertura_retardo_pasos"] = self.aperture
        return data

    def write_csv(self, path: Path) -> Path:
        k = f"k={self.coverage_factor:g}"
        header, columns = ["Frecuencia (Hz)"], [self.frequencies]
        for name, d in self.directions.items():
            header += [
                f"VSWR puerto {d.port} ({name})",
                f"U VSWR puerto {d.port} ({name}, {k})",
                f"Límite VSWR ({name})",
                f"Pérdida de inserción {d.transmission} ({name}, dB)",
                f"U pérdida de inserción {d.transmission} ({name}, dB, {k})",
                f"Límite pérdida de inserción ({name}, dB)",
                f"Retardo de grupo {d.transmission} ({name}, ns)",
                f"U retardo de grupo {d.transmission} ({name}, ns, {k})",
            ]
            columns += [
                d.vswr, d.vswr_u, d.vswr_limit, d.insertion_loss_db, d.insertion_loss_u, d.insertion_loss_limit, d.group_delay_ns, d.group_delay_u_ns,
            ]
        write_csv(path, header, columns)
        return path

    def smoothing_points(self, smoothing_pct: float) -> int:
        """Points of the moving average of ``smoothing_pct`` % of the sweep (fewer than 2: no smoothing)."""
        return int(smoothing_pct / 100 * len(self.frequencies)) if smoothing_pct > 0 else 0

    def report_rows(self, nominal: list[float], smoothing_pct: float = 0.0) -> dict[str, list[dict[str, Any]]]:
        """Rows of the tables of the certificate: at each report frequency, the values of the nearest measured point.

        With ``smoothing_pct``, the VSWR and the group delay of the rows are smoothed as in the certificates of
        the laboratory (moving average of the VSWR and of the delay between consecutive points).
        """
        f = self.frequencies
        size = self.smoothing_points(smoothing_pct)
        shown = {
            name: (moving_average(d.vswr, size), moving_average(d.step_delay_ns, size)) if size >= 2 else (d.vswr, d.group_delay_ns)
            for name, d in self.directions.items()
        }
        rows: dict[str, list[dict[str, Any]]] = {name: [] for name in self.directions}
        for target in nominal:
            index = int(np.argmin(np.abs(f - target)))
            for name, d in self.directions.items():
                vswr, delay = shown[name]
                rows[name].append(
                    {
                        "frecuencia": float(target),
                        "frecuencia_medida": float(f[index]),
                        "vswr": float(vswr[index]),
                        "u_vswr": float(d.vswr_u[index]),
                        "limite_vswr": float(d.vswr_limit[index]),
                        "perdida_insercion": float(d.insertion_loss_db[index]),
                        "u_perdida_insercion": float(d.insertion_loss_u[index]),
                        "limite_perdida_insercion": float(d.insertion_loss_limit[index]),
                        "retardo_grupo": float(delay[index]),
                        "u_retardo_grupo": float(d.group_delay_u_ns[index]),
                        "pasa": bool(d.passes()[index]),
                    }
                )
        return rows


def report_frequencies(setting: int | list[Any] | None, start: float, stop: float, logarithmic: bool = False) -> list[float]:
    """Frequencies of the rows of the certificate.

    A number n (21 by default): n frequencies from ``start`` to ``stop``,
    equally spaced in a linear sweep and logarithmically spaced in a
    logarithmic one, rounded to two significant digits except the first and the
    last (21 points from 10 MHz to 26.5 GHz: 10 MHz, 1.3 GHz, 2.7 GHz, 4 GHz…
    26.5 GHz; logarithmic: …, 23 kHz, 340 MHz, 1.4 GHz…). A list
    (``[10 MHz, 1.3 GHz, …]``): those inside [start, stop].
    """
    if isinstance(setting, (list, tuple)):
        values = sorted(parse_frequency(value) for value in setting)
        return [value for value in values if start * (1 - 1e-9) <= value <= stop * (1 + 1e-9)]
    count = int(setting or DEFAULT_REPORT_POINTS)
    if count < 2 or stop <= start:
        return [start]
    spaced = np.geomspace(start, stop, count) if logarithmic and start > 0 else np.linspace(start, stop, count)
    values = [float(value) for value in spaced]
    rounded = [values[0]] + [float(f"{value:.2g}") for value in values[1:-1]] + [values[-1]]
    return list(dict.fromkeys(rounded))
