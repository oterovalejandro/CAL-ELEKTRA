"""S-parameters of a standard defined by the model of Keysight («Agilent model» of METAS VNA Tools).

Keysight application note 1287-11: an offset line of impedance Z0, delay τ and loss L (Ω/s at 1 GHz)
ended by the termination of the standard. At the frequency f (ω = 2πf):

    αl = L·τ / (2·Z0) · √(f / 1 GHz)           βl = ω·τ + αl
    Zc = Z0 + (1 − j) · L / (2ω) · √(f / 1 GHz)  γl = αl + j·βl

* open: Z = 1/(jωC), C = C0 + C1·f + C2·f² + C3·f³;
* short: Z = jωL, L = L0 + L1·f + L2·f² + L3·f³;
* load: Z = the impedance of the load (by default the offset Z0: a matched termination);
* through: the line itself, between two ports of impedance Zr.

The reflection is referred to the reference impedance Zr of the kit:

    Γ1 = (Zc − Zr)/(Zc + Zr)    ΓL = (Z − Zr)/(Z + Zr)    E = e^(−2γl)
    Γ  = [Γ1·(1 − E − Γ1·ΓL) + E·ΓL] / [1 − Γ1·(E·Γ1 + ΓL·(1 − E))]

which is the input impedance of the line, Zin = Zc·(Z + Zc·tanh γl)/(Zc + Z·tanh γl), seen from Zr.
The model is not defined at DC (the loss term grows as 1/√f): f > 0. Coaxial standards only.
"""

from __future__ import annotations

import numpy as np

from calelektra.kits.model import C_UNITS, L_UNITS, KitError, KitStandard, StandardDefinition


def offset_line(f: np.ndarray, definition: StandardDefinition) -> tuple[np.ndarray, np.ndarray]:
    """Characteristic impedance and γ·l of the offset line."""
    f = np.asarray(f, float)
    if np.any(f <= 0):
        raise KitError("El modelo del patrón no está definido en DC: las frecuencias deben ser mayores que 0 Hz")
    delay, loss, z0 = definition.offset_delay_ps * 1e-12, definition.offset_loss * 1e9, definition.offset_z0
    w, root = 2 * np.pi * f, np.sqrt(f / 1e9)
    alpha_l = loss * delay / (2 * z0) * root
    beta_l = w * delay + alpha_l
    zc = z0 + (1 - 1j) * loss / (2 * w) * root
    return zc, alpha_l + 1j * beta_l


def termination(f: np.ndarray, kind: str, definition: StandardDefinition, zr: complex) -> np.ndarray:
    """Reflection of the termination, referred to Zr."""
    w = 2 * np.pi * np.asarray(f, float)
    if kind == "abierto":
        c = sum(coefficient * unit * f**k for k, (coefficient, unit) in enumerate(zip(definition.capacitance, C_UNITS)))
        return (1 - 1j * w * c * zr) / (1 + 1j * w * c * zr)
    if kind == "cortocircuito":
        inductance = sum(coefficient * unit * f**k for k, (coefficient, unit) in enumerate(zip(definition.inductance, L_UNITS)))
        return (1j * w * inductance - zr) / (1j * w * inductance + zr)
    if kind == "carga":
        r, x = definition.load_impedance or (definition.offset_z0, 0.0)
        z = complex(r, x)
        return np.full(w.shape, (z - zr) / (z + zr))
    raise KitError(f"Un {kind} no es un patrón de reflexión")


def reflection(f: np.ndarray, kind: str, definition: StandardDefinition, zr: complex = 50.0) -> np.ndarray:
    zc, gamma_l = offset_line(f, definition)
    g1, gl = (zc - zr) / (zc + zr), termination(f, kind, definition, zr)
    e = np.exp(-2 * gamma_l)
    return (g1 * (1 - e - g1 * gl) + e * gl) / (1 - g1 * (e * g1 + gl * (1 - e)))


def through(f: np.ndarray, definition: StandardDefinition, zr: complex = 50.0) -> np.ndarray:
    """S-matrices of the line (frequency, receiver, source): reciprocal and symmetrical."""
    zc, gamma_l = offset_line(f, definition)
    g1, e = (zc - zr) / (zc + zr), np.exp(-2 * gamma_l)
    s11 = g1 * (1 - e) / (1 - g1**2 * e)
    s21 = (1 - g1**2) * np.exp(-gamma_l) / (1 - g1**2 * e)
    s = np.empty((len(np.atleast_1d(f)), 2, 2), complex)
    s[:, 0, 0] = s[:, 1, 1] = s11
    s[:, 1, 0] = s[:, 0, 1] = s21
    return s


def model_s(standard: KitStandard, f: np.ndarray, zr: complex = 50.0, definition: StandardDefinition | None = None) -> np.ndarray:
    """S-parameters of a standard defined by the model: shape (frequencies, ports, ports)."""
    definition = definition or standard.definition
    if definition.kind != "modelo":
        raise KitError(f"«{standard.id}» no está definido por el modelo")
    if standard.type == "through":
        return through(f, definition, zr)
    if standard.type == "adaptador":
        raise KitError(f"«{standard.id}»: un adaptador (through desconocido) no tiene modelo")
    return reflection(f, standard.type, definition, zr).reshape(-1, 1, 1)
