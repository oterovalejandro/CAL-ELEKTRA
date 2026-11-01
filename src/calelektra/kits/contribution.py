"""The contribution of the calibration kit to the uncertainty of a calibrated measurement.

As METAS VNA Tools computes it (linear propagation, LinProp, through the calibration): the definition of
each standard is an uncertain quantity, its value and its dependencies on the inputs of its budget (its
electrical specifications, those of its real values, or the data of its certificate: ``kits/specs.py``,
``kits/calibration.py``); the calibration solves the error terms with those definitions and the error
correction carries them into the corrected measurement. The result is the corrected measurement with its
dependencies on the inputs of the kit: its uncertainty, standard by standard, and correlated between
frequencies as the definitions are.

The calibrations are those of the setups (``setups/connections.py``):

* **OSM**: open, short and load of the gender of the plane;
* **TOSM**: OSM at both ports and the through: flush (its definition, S21 = 1 and S11 = 0, is exact) or a
  defined one (e.g. the through of the kit);
* **UOSM**: OSM at both ports and an unknown through: reciprocity gives the transmission tracking, the
  through needs no definition;
* **transmission normalization**: S21 = S21m · T21/T21m; only the through has a definition, and with a flush
  through the kit does not take part. The reflections are not corrected: their error (the raw match of the
  VNA, the «Unknown Error Terms» of VNA Tools) is not of the kit.

The error made by the definitions does not depend on the raw errors of the VNA: the corrected measurement
is the map that takes the actual standards to their definitions, applied to the actual DUT. So it is
computed with a perfect VNA (raw values = actual values) and the actual standards and DUT at their nominal
values, to first order. The sensitivities are complex derivatives (the calibration is holomorphic in the
definitions), by central differences with h = 10⁻⁶. For OSM they are the Lagrange basis of the three
standards, δΓ = Σᵢ δDᵢ · Πⱼ≠ᵢ (Γ − Aⱼ)/(Aᵢ − Aⱼ); with ideal standards (1, −1, 0):

    δΓ = δD_carga·(1 − Γ²) + δD_abierto·Γ(1 + Γ)/2 + δD_corto·Γ(Γ − 1)/2

The effective (residual) error terms that the kit leaves are the error terms solved with that perfect VNA,
whose nominal values are 0 (directivity, match) or 1 (tracking): their uncertainty (k = 2) is what a
datasheet calls «effective system data» (``EffectiveTerm``).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from calelektra.core.calibration import (
    OnePortTerms,
    TwoPortTerms,
    solve_one_port,
    solve_tosm,
    solve_uosm,
)
from calelektra.kits.model import KitError
from calelektra.kits.standards import ResolvedStandard
from calelektra.sdata.model import PortDescription, SParamData
from calelektra.sdata.unclib import UncVector

METHODS = {
    "OSM": "OSM (1 puerto)",
    "TOSM": "TOSM (through conocido)",
    "UOSM": "UOSM (through desconocido)",
    "normalizacion": "Normalización de transmisión",
}
REFLECTIONS = {"abierto": "OPEN", "cortocircuito": "SHORT", "carga": "MATCH"}
COVERAGE = 2.0
STEP = 1e-6  # of the central differences
NOISE = 1e-8  # their rounding is about 10⁻¹⁶/STEP
FLAT = "plano"  # a flush through: exact definition


@dataclass
class Variable:
    """A definition of the calibration: a parameter of a standard, with its uncertainty."""

    group: str  # what it is, for the breakdown: «Puerto 1: OPEN -M-»
    data: SParamData  # the standard, on the frequencies of the calibration
    receiver: int = 0
    source: int = 0

    @property
    def values(self) -> np.ndarray:
        return self.data.s[:, self.receiver, self.source]


@dataclass
class CalibrationStandards:
    """The definitions of the standards of a calibration on a grid of frequencies, each with its uncertainty."""

    method: str
    frequencies: np.ndarray
    port1: dict[str, Variable] = field(default_factory=dict)  # abierto, cortocircuito, carga
    port2: dict[str, Variable] = field(default_factory=dict)
    through: dict[str, Variable] = field(default_factory=dict)  # S11, S21, S12, S22 of a defined through; empty: flush

    @property
    def variables(self) -> list[Variable]:
        return [*self.port1.values(), *self.port2.values(), *self.through.values()]

    @property
    def ports(self) -> int:
        return 1 if self.method == "OSM" else 2


@dataclass
class EffectiveTerm:
    """An effective error term that the kit leaves: its uncertainty (k = 2) at each frequency."""

    name: str  # «Directividad», «Adaptación de fuente», «Seguimiento de reflexión»…
    port: str  # «puerto 1», «1 → 2»…
    tracking: bool  # nominal 1 (a tracking) or 0 (a directivity or a match)
    radius: np.ndarray  # k·√λmax of the covariance of its real and imaginary parts
    magnitude_db: np.ndarray  # trackings: U of 20·log10|T| (dB)
    phase_deg: np.ndarray  # trackings: U of ∠T (°)

    @property
    def db(self) -> np.ndarray:
        """Directivity and match as a datasheet gives them: −20·log10 of the radius (dB, the larger the better)."""
        return -20 * np.log10(np.maximum(self.radius, 1e-12))


@dataclass
class KitContribution:
    """The corrected measurement with its dependencies on the inputs of the kit, and what follows from them."""

    method: str
    data: SParamData  # the corrected DUT at its nominal values; its uncertainty: only that of the kit
    groups: dict[str, np.ndarray]  # standard → indices of its inputs in ``data.vector``
    terms: list[EffectiveTerm]

    def breakdown(self, receiver: int, source: int, quantity: str = "modulo") -> dict[str, np.ndarray]:
        """U (k = 2) of a quantity of S[receiver, source] (``re``, ``im``, ``modulo``, ``db`` or ``fase``) due to each
        standard, and ``total`` (the inputs of different standards are independent)."""
        data = self.data
        points = np.arange(data.n_points)
        elements = np.column_stack([data.element(points, receiver, source, 0), data.element(points, receiver, source, 1)])
        weights = _weights(data.s[:, receiver, source], quantity)
        variances = _group_variances(data.vector, elements, weights, self.groups)
        result = {group: COVERAGE * np.sqrt(v) for group, v in variances.items()}
        result["total"] = COVERAGE * np.sqrt(sum(variances.values())) if variances else np.zeros(len(points))
        return result


def _weights(values: np.ndarray, quantity: str) -> np.ndarray:
    """Weights of the real and imaginary parts that give a quantity of a complex value (first order)."""
    magnitude = np.abs(values)
    safe = np.where(magnitude > 0, magnitude, 1.0)
    if quantity == "re":
        return np.tile([1.0, 0.0], (len(values), 1))
    if quantity == "im":
        return np.tile([0.0, 1.0], (len(values), 1))
    if quantity == "modulo":
        weights = np.column_stack([values.real / safe, values.imag / safe])
    elif quantity == "db":
        weights = np.column_stack([values.real, values.imag]) / safe[:, None] ** 2 * (20 / math.log(10))
    elif quantity == "fase":
        weights = np.column_stack([-values.imag, values.real]) / safe[:, None] ** 2 * (180 / math.pi)
    else:
        raise KitError(f"Magnitud desconocida: {quantity}")
    weights[magnitude == 0] = [math.sqrt(0.5), math.sqrt(0.5)] if quantity == "modulo" else [0.0, 0.0]
    return weights


def _group_variances(vector: UncVector, elements: np.ndarray, weights: np.ndarray, groups: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
    """Variance of Σₜ wₜ·elementₜ due to the inputs of each group (each input in one group)."""
    combined = vector.combination(elements, weights)
    size = len(elements)
    owners = np.repeat(np.arange(size), np.diff(combined.offsets))
    contributions = (combined.jacobians * combined.input_uncertainties[combined.indices]) ** 2
    result = {}
    for group, inputs in groups.items():
        mask = np.isin(combined.indices, inputs)
        result[group] = np.bincount(owners[mask], weights=contributions[mask], minlength=size)
    return result


def _solve(standards: CalibrationStandards, definitions: list[np.ndarray], dut: np.ndarray) -> tuple[np.ndarray, dict[tuple[str, str], np.ndarray]]:
    """The corrected DUT and the error terms of a calibration with these definitions, with a perfect VNA (raw values =
    actual values = the nominal definitions)."""
    variables = standards.variables
    actual = {id(v): v.values for v in variables}
    defined = {id(v): d for v, d in zip(variables, definitions)}

    def one_port(port: dict[str, Variable]) -> OnePortTerms:
        raw = {REFLECTIONS[kind]: actual[id(v)] for kind, v in port.items()}
        return solve_one_port(raw, {REFLECTIONS[kind]: defined[id(v)] for kind, v in port.items()})

    method = standards.method
    points = len(standards.frequencies)
    if method == "normalizacion":
        corrected = dut.copy()
        terms = {}
        for name, (r, c) in (("S21", (1, 0)), ("S12", (0, 1))):
            variable = standards.through.get(name)
            ratio = defined[id(variable)] / actual[id(variable)] if variable else np.ones(points, complex)
            corrected[:, r, c] = dut[:, r, c] * ratio
            terms[("Seguimiento de transmisión", "1 → 2" if name == "S21" else "2 → 1")] = ratio
        return corrected, terms
    p1 = one_port(standards.port1)
    terms = {("Directividad", "puerto 1"): p1.directivity, ("Adaptación de fuente", "puerto 1"): p1.source_match,
             ("Seguimiento de reflexión", "puerto 1"): p1.tracking}
    if method == "OSM":
        return p1.correct(dut[:, 0, 0]).reshape(-1, 1, 1), terms
    p2 = one_port(standards.port2)
    terms.update({("Directividad", "puerto 2"): p2.directivity, ("Adaptación de fuente", "puerto 2"): p2.source_match,
                  ("Seguimiento de reflexión", "puerto 2"): p2.tracking})
    flush = np.zeros((points, 2, 2), complex)
    flush[:, 1, 0] = flush[:, 0, 1] = 1.0
    if method == "TOSM" and standards.through:
        order = ("S11", "S21", "S12", "S22")
        raw = _matrix([actual[id(standards.through[p])] for p in order])
        definition = _matrix([defined[id(standards.through[p])] for p in order])
        two: TwoPortTerms = solve_tosm(p1, p2, raw, definition)
    elif method == "TOSM":
        two = solve_tosm(p1, p2, flush)
    elif method == "UOSM":
        two = solve_uosm(p1, p2, flush, standards.frequencies)  # the actual through does not matter
    else:
        raise KitError(f"Calibración desconocida: {method}")
    terms.update({("Adaptación de carga", "puerto 2"): two.forward_load_match, ("Adaptación de carga", "puerto 1"): two.reverse_load_match,
                  ("Seguimiento de transmisión", "1 → 2"): two.forward_tracking, ("Seguimiento de transmisión", "2 → 1"): two.reverse_tracking})
    return two.correct(dut), terms


def _matrix(parameters: list[np.ndarray]) -> np.ndarray:
    """S-matrices from S11, S21, S12, S22."""
    s11, s21, s12, s22 = parameters
    result = np.empty((len(s11), 2, 2), complex)
    result[:, 0, 0], result[:, 1, 0], result[:, 0, 1], result[:, 1, 1] = s11, s21, s12, s22
    return result


def _sensitivities(standards: CalibrationStandards, dut: np.ndarray):
    """The corrected DUT and the error terms at the nominal definitions, and their complex derivatives with respect
    to each definition (one array per variable)."""
    nominal = [v.values.astype(complex) for v in standards.variables]
    base, base_terms = _solve(standards, nominal, dut)
    derivatives, term_derivatives = [], []
    for i in range(len(nominal)):
        plus, minus = list(nominal), list(nominal)
        plus[i] = nominal[i] + STEP
        minus[i] = nominal[i] - STEP
        up, up_terms = _solve(standards, plus, dut)
        down, down_terms = _solve(standards, minus, dut)
        derivatives.append(_clean((up - down) / (2 * STEP)))
        term_derivatives.append({key: _clean((up_terms[key] - down_terms[key]) / (2 * STEP)) for key in base_terms})
    return base, base_terms, derivatives, term_derivatives


def _clean(derivative: np.ndarray) -> np.ndarray:
    """Rounding of the differences (about 10⁻¹⁰): below NOISE a derivative is 0 (e.g. the through measured again)."""
    derivative[np.abs(derivative) < NOISE] = 0
    return derivative


def _input_sensitivities(variable: Variable, index_of: dict[bytes, int]) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(frequency, global input, complex sensitivity) of every dependency of the definition of a variable."""
    data = variable.data
    vector = data.vector
    points = np.arange(data.n_points)
    global_of = np.array([index_of[item.id] for item in vector.inputs], np.int64) if vector.inputs else np.zeros(0, np.int64)
    ks, inputs, values = [], [], []
    for part, factor in ((0, 1.0), (1, 1j)):
        elements = data.element(points, variable.receiver, variable.source, part)
        starts = vector.offsets[elements]
        counts = vector.offsets[elements + 1] - starts
        total = int(counts.sum())
        positions = np.repeat(starts - (np.cumsum(counts) - counts), counts) + np.arange(total)
        ks.append(np.repeat(points, counts))
        inputs.append(global_of[vector.indices[positions]] if total else np.zeros(0, np.int64))
        values.append(vector.jacobians[positions] * factor)
    return np.concatenate(ks), np.concatenate(inputs), np.concatenate(values)


def _propagated(values: np.ndarray, derivatives: list[np.ndarray], sources: list[tuple], inputs: list, zr: complex) -> SParamData:
    """Data with ``values`` (frequencies, n, n) that depend on the inputs through the definitions: element (k, r, c)
    changes by Σᵥ derivativeᵥ[k, r, c]·g, with g the complex sensitivity of definition v to each input."""
    points, n = values.shape[0], values.shape[1]
    data = SParamData.from_values(np.arange(points, dtype=float), values, zr, tuple(PortDescription(p + 1) for p in range(n)))
    rows, columns, coefficients = [], [], []
    for derivative, (ks, input_indices, g) in zip(derivatives, sources):
        if not len(ks):
            continue
        for r in range(n):
            for c in range(n):
                product = derivative[ks, r, c] * g
                for part, component in ((0, product.real), (1, product.imag)):
                    rows.append(data.element(ks, r, c, part))
                    columns.append(input_indices)
                    coefficients.append(component)
    size = len(data.vector)
    if rows:
        rows_all, columns_all, coefficients_all = np.concatenate(rows), np.concatenate(columns), np.concatenate(coefficients)
        keys, inverse = np.unique(rows_all * max(len(inputs), 1) + columns_all, return_inverse=True)
        merged = np.bincount(inverse.ravel(), weights=coefficients_all, minlength=len(keys))
        element_of, input_of = np.divmod(keys, max(len(inputs), 1))
        keep = merged != 0
        element_of, input_of, merged = element_of[keep], input_of[keep], merged[keep]
    else:
        element_of = input_of = np.zeros(0, np.int64)
        merged = np.zeros(0)
    offsets = np.concatenate([[0], np.cumsum(np.bincount(element_of, minlength=size))])
    return SParamData(data.frequencies, data.ports, UncVector(data.vector.values, list(inputs), offsets, input_of, merged))


def kit_contribution(standards: CalibrationStandards, dut: np.ndarray, zr: complex = 50.0) -> KitContribution:
    """The contribution of the kit to the corrected measurement of a DUT whose actual S-parameters are ``dut``
    (frequencies of the calibration, ports, ports): 1 port for OSM, 2 for the others."""
    dut = np.asarray(dut, complex)
    f = standards.frequencies
    if dut.shape != (len(f), standards.ports, standards.ports):
        raise KitError(f"El DUT debe tener {standards.ports} puerto{'s' if standards.ports > 1 else ''} y {len(f)} frecuencias")
    variables = standards.variables
    inputs, index_of, groups = [], {}, {}
    for variable in variables:
        for item in variable.data.vector.inputs:
            if item.id not in index_of:
                index_of[item.id] = len(inputs)
                inputs.append(item)
                groups.setdefault(variable.group, []).append(index_of[item.id])
        groups.setdefault(variable.group, [])
    sources = [_input_sensitivities(v, index_of) for v in variables]
    base, base_terms, derivatives, term_derivatives = _sensitivities(standards, dut)
    data = _propagated(base, derivatives, sources, inputs, zr)
    data = SParamData(f, data.ports, data.vector)
    effective = []
    for key, nominal in base_terms.items():
        term = _propagated(nominal.reshape(-1, 1, 1), [d[key].reshape(-1, 1, 1) for d in term_derivatives], sources, inputs, zr)
        effective.append(_effective(key, term))
    return KitContribution(standards.method, data, {g: np.array(v, np.int64) for g, v in groups.items()}, effective)


def _effective(key: tuple[str, str], term: SParamData) -> EffectiveTerm:
    name, port = key
    points = term.n_points
    radius, magnitude, phase = np.zeros(points), np.zeros(points), np.zeros(points)
    tracking = name.startswith("Seguimiento")
    for k in range(points):
        cov = term.point_covariance(k)
        radius[k] = COVERAGE * math.sqrt(max(np.linalg.eigvalsh(cov).max(), 0.0))
        if tracking:  # nominal 1: the real part is the magnitude, the imaginary part the phase
            magnitude[k] = COVERAGE * math.sqrt(max(cov[0, 0], 0.0)) * 20 / math.log(10)
            phase[k] = math.degrees(COVERAGE * math.sqrt(max(cov[1, 1], 0.0)))
    return EffectiveTerm(name, port, tracking, radius, magnitude, phase)


def worst_phase(standards: CalibrationStandards, magnitude: float, quantity: str = "modulo", steps: int = 24) -> dict[str, np.ndarray]:
    """The contribution of the kit to a reflection of a given magnitude, at the phase where it is largest (``steps``
    phases from 0° to 360°, at each frequency): ``total`` and the part of each standard at that phase."""
    f = standards.frequencies
    best: dict[str, np.ndarray] = {}
    for phase in np.arange(steps) * 2 * np.pi / steps:
        result = kit_contribution(standards, reflection_dut(f, magnitude * np.exp(1j * phase)))
        current = result.breakdown(0, 0, quantity)
        if not best:
            best = current
            continue
        larger = current["total"] > best["total"]
        for key in best:
            best[key] = np.where(larger, current[key], best[key])
    return best


# ── the standards of a calibration from a kit ──────────────────────────────────────────────────────────


def choose_standard(standards: list[ResolvedStandard], kind: str, genders: list[str]) -> ResolvedStandard | None:
    """The first standard of a type and genders that is in use and defined."""
    for resolved in standards:
        s = resolved.standard
        if s.type == kind and list(s.genders) == list(genders) and resolved.usable and resolved.definition.kind != "ninguna":
            return resolved
    return None


def calibration_standards(
    standards: list[ResolvedStandard],
    method: str,
    genders: tuple[str, ...],
    frequencies: np.ndarray,
    data_of,
    through: str = FLAT,
) -> CalibrationStandards:
    """The definitions of a calibration with the standards of a kit: OSM with the gender ``genders[0]`` (the gender of
    the standards, the opposite of that of the plane); TOSM and UOSM with ``genders[0]`` and ``genders[1]``; ``through``
    is ``plano`` (flush) or the identifier of the through of the kit (TOSM, normalization). ``data_of(resolved)`` gives
    the S-parameters of a standard with its uncertainty (``Library.standard_data``)."""
    if method not in METHODS:
        raise KitError(f"Calibración desconocida: {method}")
    f = np.asarray(frequencies, float)
    result = CalibrationStandards(method, f)

    def variable(resolved: ResolvedStandard, prefix: str, receiver: int = 0, source: int = 0) -> Variable:
        label = resolved.standard.label or resolved.id
        return Variable(f"{prefix}{label}", data_of(resolved).interpolated(f), receiver, source)

    def port(gender: str, number: int) -> dict[str, Variable]:
        chosen = {}
        for kind in REFLECTIONS:
            resolved = choose_standard(standards, kind, [gender])
            if resolved is None:
                raise KitError(f"El kit no tiene un {kind} {gender} en uso y definido para el puerto {number}")
            chosen[kind] = variable(resolved, f"Puerto {number}: ")
        return chosen

    if method != "normalizacion":
        result.port1 = port(genders[0], 1)
    if method in ("TOSM", "UOSM"):
        result.port2 = port(genders[1] if len(genders) > 1 else genders[0], 2)
    if method in ("TOSM", "normalizacion") and through != FLAT:
        resolved = next((s for s in standards if s.id == through), None)
        if resolved is None or resolved.standard.type != "through" or resolved.definition.kind == "ninguna":
            raise KitError(f"«{through}» no es un through definido del kit")
        data = data_of(resolved).interpolated(f)
        label = resolved.standard.label or resolved.id
        result.through = {name: Variable(f"Through: {label}", data, r, c) for name, (r, c) in
                          (("S11", (0, 0)), ("S21", (1, 0)), ("S12", (0, 1)), ("S22", (1, 1)))}
    return result


def reflection_dut(frequencies: np.ndarray, gamma: complex) -> np.ndarray:
    """A 1-port DUT of constant reflection."""
    return np.full((len(frequencies), 1, 1), complex(gamma))


def two_port_dut(frequencies: np.ndarray, transmission_db: float = 0.0, match_db: float | None = None) -> np.ndarray:
    """A symmetrical, reciprocal 2-port DUT: |S21| = |S12| in dB (no phase) and |S11| = |S22| in dB (None: matched)."""
    result = np.zeros((len(frequencies), 2, 2), complex)
    result[:, 1, 0] = result[:, 0, 1] = 10 ** (transmission_db / 20)
    if match_db is not None:
        result[:, 0, 0] = result[:, 1, 1] = 10 ** (match_db / 20)
    return result
