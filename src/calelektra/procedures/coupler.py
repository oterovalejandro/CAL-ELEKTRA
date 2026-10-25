"""Directional coupler: quantities from the measured port pairs and pass / fail against its nominal values.

Ports ENTRADA, SALIDA, ACOPLADO and AISLADO. Each measured pair (port on P1,
port on P2) gives a 2 × 2 S matrix with the other two ports terminated in
50 Ω. From the pairs, in dB and as positive losses:

* pérdidas de inserción  IL = −|S(SALIDA ← ENTRADA)|
* acoplamiento           C  = −|S(ACOPLADO ← ENTRADA)|
* aislamiento            I  = −|S(ACOPLADO ← SALIDA)| (or −|S(AISLADO ← ENTRADA)| if that pair is measured instead)
* directividad           D  = I − C
* pérdidas de retorno    RL = the worst of the return losses of ENTRADA and SALIDA

The return loss of each port comes from one measurement: ENTRADA and SALIDA
from the ENTRADA–SALIDA pair (each of them is measured in two pairs), the
other ports from the first pair that measures them. The transmission loss
(TL) of each pair is −|S21|: from its port on P1 to its port on P2.

The nominal values come from the fields of the model of the coupler
(``acoplamiento_nominal_db`` ± ``tolerancia_acoplamiento_db``,
``perdidas_insercion_max_db``, ``aislamiento_min_db``,
``directividad_min_db``, ``perdidas_retorno_min_db``) and are evaluated
inside its nominal frequency range. The margin is positive when a point
passes.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from calelektra.core.frequency import FrequencyRange
from calelektra.core.units import format_frequency
from calelektra.procedures.results import db


def pair_name(first: str, second: str) -> str:
    return f"{first}-{second}"


@dataclass
class CouplerQuantity:
    key: str
    name: str
    symbol: str
    values: np.ndarray | None  # dB; None: the pairs measured do not give it


def _transmission(data: dict[str, np.ndarray], source: str, target: str) -> np.ndarray | None:
    """S(target ← source) from the pair that has both ports."""
    if pair_name(source, target) in data:
        return data[pair_name(source, target)][:, 1, 0]
    if pair_name(target, source) in data:
        return data[pair_name(target, source)][:, 0, 1]
    return None


def pair_ports(name: str) -> tuple[str, str]:
    first, _, second = name.partition("-")
    return first, second


def return_loss_sources(names: list[str]) -> dict[str, tuple[str, int]]:
    """Port → (pair, 0 or 1: its position in the pair) whose reflection gives the return loss of the port.

    ENTRADA and SALIDA come from the ENTRADA–SALIDA pair when it is measured; every other port (and
    ENTRADA or SALIDA without that pair) from the first measured pair that has it.
    """
    from calelektra.procedures.model import COUPLER_PORTS

    through = next((name for name in names if set(pair_ports(name)) == {"ENTRADA", "SALIDA"}), None)
    sources: dict[str, tuple[str, int]] = {}
    for port in COUPLER_PORTS:
        candidates = [name for name in names if port in pair_ports(name)]
        if port in ("ENTRADA", "SALIDA") and through is not None:
            candidates.insert(0, through)
        if candidates:
            sources[port] = (candidates[0], pair_ports(candidates[0]).index(port))
    return sources


def port_return_losses(data: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
    """Return loss (dB, positive) of each port measured, from the pair given by :func:`return_loss_sources`."""
    losses = {}
    for port, (name, index) in return_loss_sources(list(data)).items():
        losses[port] = -db(data[name][:, index, index])
    return losses


TRANSMISSION_SYMBOLS = {
    frozenset({"ENTRADA", "SALIDA"}): "IL",
    frozenset({"ENTRADA", "ACOPLADO"}): "C",
    frozenset({"SALIDA", "ACOPLADO"}): "I",
    frozenset({"ENTRADA", "AISLADO"}): "I",
}


def transmission_symbol(name: str) -> str | None:
    """IL, C or I: the quantity of the coupler given by the transmission of the pair (``None``: none)."""
    return TRANSMISSION_SYMBOLS.get(frozenset(pair_ports(name)))


def pair_transmission_losses(data: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
    """Transmission loss (dB, positive) of each pair: −|S21|, from its port on P1 to its port on P2."""
    return {name: -db(s[:, 1, 0]) for name, s in data.items()}


def coupler_quantities(data: dict[str, np.ndarray]) -> list[CouplerQuantity]:
    """Insertion loss, coupling, isolation, directivity and return loss (dB) from the measured pairs."""

    def loss(trace: np.ndarray | None) -> np.ndarray | None:
        return None if trace is None else -db(trace)

    insertion = loss(_transmission(data, "ENTRADA", "SALIDA"))
    coupling = loss(_transmission(data, "ENTRADA", "ACOPLADO"))
    isolation = loss(_transmission(data, "SALIDA", "ACOPLADO"))
    if isolation is None:
        isolation = loss(_transmission(data, "ENTRADA", "AISLADO"))
    directivity = isolation - coupling if isolation is not None and coupling is not None else None
    ports = port_return_losses(data)
    reflections = [ports[port] for port in ("ENTRADA", "SALIDA") if port in ports]
    return_loss = np.min(reflections, axis=0) if reflections else None
    return [
        CouplerQuantity("perdidas_insercion", "Pérdidas de inserción", "IL", insertion),
        CouplerQuantity("acoplamiento", "Acoplamiento", "C", coupling),
        CouplerQuantity("aislamiento", "Aislamiento", "I", isolation),
        CouplerQuantity("directividad", "Directividad", "D", directivity),
        CouplerQuantity("perdidas_retorno", "Pérdidas de retorno", "RL", return_loss),
    ]


@dataclass
class CouplerLimit:
    key: str
    criterion: str  # maximo | minimo | tolerancia
    value: float
    tolerance: float | None = None

    def describe(self) -> str:
        if self.criterion == "tolerancia":
            return f"{self.value:g} ± {self.tolerance:g} dB"
        return f"{'≤' if self.criterion == 'maximo' else '≥'} {self.value:g} dB"

    def margin(self, values: np.ndarray) -> np.ndarray:
        if self.criterion == "maximo":
            return self.value - values
        if self.criterion == "minimo":
            return values - self.value
        return (self.tolerance or 0.0) - np.abs(values - self.value)


def coupler_limits(fields: dict[str, float | None]) -> dict[str, CouplerLimit]:
    """Limits from the nominal values of the model (only those given)."""
    limits: dict[str, CouplerLimit] = {}
    if fields.get("perdidas_insercion_max_db") is not None:
        limits["perdidas_insercion"] = CouplerLimit("perdidas_insercion", "maximo", float(fields["perdidas_insercion_max_db"]))
    if fields.get("acoplamiento_nominal_db") is not None and fields.get("tolerancia_acoplamiento_db") is not None:
        limits["acoplamiento"] = CouplerLimit(
            "acoplamiento", "tolerancia", float(fields["acoplamiento_nominal_db"]), float(fields["tolerancia_acoplamiento_db"])
        )
    if fields.get("aislamiento_min_db") is not None:
        limits["aislamiento"] = CouplerLimit("aislamiento", "minimo", float(fields["aislamiento_min_db"]))
    if fields.get("directividad_min_db") is not None:
        limits["directividad"] = CouplerLimit("directividad", "minimo", float(fields["directividad_min_db"]))
    if fields.get("perdidas_retorno_min_db") is not None:
        limits["perdidas_retorno"] = CouplerLimit("perdidas_retorno", "minimo", float(fields["perdidas_retorno_min_db"]))
    return limits


def _state(passed: bool | None) -> str:
    return "PASA" if passed else ("FALLA" if passed is False else "NO_EVALUADO")


def evaluate_coupler(
    frequencies: np.ndarray, quantities: list[CouplerQuantity], limits: dict[str, CouplerLimit], nominal_range: FrequencyRange | None
) -> list[dict]:
    """Pass / fail of each quantity with a nominal value, as entries of the test record (``evaluacion``)."""
    inside = np.ones(len(frequencies), dtype=bool)
    if nominal_range is not None:
        inside = (frequencies >= nominal_range.start * (1 - 1e-12)) & (frequencies <= nominal_range.stop * (1 + 1e-12))
    entries = []
    for quantity in quantities:
        limit = limits.get(quantity.key)
        if limit is None:
            continue
        entry = {"nombre": quantity.name, "criterio": f"{limit.describe()} (valor nominal del modelo)", "unidad": "dB"}
        if quantity.values is None:
            entry.update(resultado="NO_EVALUADO", parametros=[], detalle=[f"{quantity.name}: los pares medidos no la dan"])
            entries.append(entry)
            continue
        valid = inside & np.isfinite(quantity.values)
        margins = limit.margin(quantity.values[valid])
        evaluated, failed = int(valid.sum()), int((margins < 0).sum())
        passed = None if evaluated == 0 else failed == 0
        worst = int(np.argmin(margins)) if evaluated else None
        worst_margin = float(margins[worst]) if worst is not None else None
        worst_frequency = float(frequencies[valid][worst]) if worst is not None else None
        parameter = {
            "parametro": quantity.symbol,
            "resultado": _state(passed),
            "margen_minimo": None if worst_margin is None else round(worst_margin, 4),
            "frecuencia": None if worst_frequency is None else format_frequency(worst_frequency, decimal="."),
            "evaluados": evaluated,
            "fallos": failed,
            "fuera_de_rango": int((~inside).sum()),
        }
        if passed is None:
            text = f"{quantity.symbol}: ningún punto dentro del rango nominal del acoplador"
        else:
            state = "pasa" if passed else f"FALLA en {failed} de {evaluated} puntos"
            text = f"{quantity.symbol}: {state}; margen mínimo {worst_margin:+.3f} dB a {format_frequency(worst_frequency)}"
            values = quantity.values[valid]
            text += f"; medido {values.min():.3f} – {values.max():.3f} dB"
        entry.update(
            resultado=_state(passed),
            margen_minimo=parameter["margen_minimo"],
            parametros=[parameter],
            detalle=[text],
        )
        entries.append(entry)
    return entries


def coupler_verdict(entries: list[dict]) -> str:
    states = [entry.get("resultado") for entry in entries]
    if "FALLA" in states:
        return "FALLA"
    if "PASA" in states:
        return "PASA"
    return "NO_EVALUADO" if entries else "SIN_ESPECIFICACIONES"


def coupler_summary(quantities: list[CouplerQuantity]) -> dict:
    """Minimum and maximum of each quantity (record ``resultados``)."""
    summary = {}
    for quantity in quantities:
        if quantity.values is not None and np.isfinite(quantity.values).any():
            values = quantity.values[np.isfinite(quantity.values)]
            summary[f"{quantity.key}_db"] = {"min": round(float(values.min()), 4), "max": round(float(values.max()), 4)}
    return summary
