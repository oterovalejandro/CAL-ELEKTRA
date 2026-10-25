"""Models of the devices that the dummy VNA «sees» connected to its ports.

Two-port models return ``S(f)`` with shape ``(points, 2, 2)``; one-port models
return ``Γ(f)``; the directional coupler model returns its 4 × 4 matrix (each
measured pair is a 2 × 2 sub-matrix, the other ports terminated in 50 Ω). A DUT is simulated from its S-parameter file when it has one,
otherwise from its catalogue fields (length, attenuation, LISN network…), so
the fields of the equipment are also used while a (simulated) test runs.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import numpy as np

from calelektra.core.networks import LISN_NETWORKS, impedance_to_gamma, lisn_impedance
from calelektra.core.touchstone import Touchstone

TwoPortModel = Callable[[np.ndarray], np.ndarray]
OnePortModel = Callable[[np.ndarray], np.ndarray]

C0 = 299_792_458.0


def through_model() -> TwoPortModel:
    def model(f: np.ndarray) -> np.ndarray:
        s = np.zeros((len(f), 2, 2), dtype=complex)
        s[:, 1, 0] = s[:, 0, 1] = 1.0
        return s

    return model


def interp_complex(f: np.ndarray, f_data: np.ndarray, data: np.ndarray) -> np.ndarray:
    magnitude = np.interp(f, f_data, np.abs(data))
    phase = np.interp(f, f_data, np.unwrap(np.angle(data)))
    return magnitude * np.exp(1j * phase)


def touchstone_model(data: Touchstone) -> TwoPortModel:
    if data.n_ports != 2:
        raise ValueError("se necesita un fichero de 2 puertos")

    def model(f: np.ndarray) -> np.ndarray:
        s = np.empty((len(f), 2, 2), dtype=complex)
        for i in range(2):
            for j in range(2):
                s[:, i, j] = interp_complex(f, data.frequencies, data.s[:, i, j])
        return s

    return model


def cable_model(length_m: float, attenuation_db_m_1ghz: float, return_loss_db: float = 28.0, velocity: float = 0.7, seed: int = 0) -> TwoPortModel:
    """Coaxial cable: loss ∝ √f (scaled from the 1 GHz value), delay and small end reflections."""
    delay = length_m / (velocity * C0)
    rng = np.random.default_rng(seed)
    phase1, phase2 = rng.uniform(-np.pi, np.pi, 2)

    def model(f: np.ndarray) -> np.ndarray:
        f_ghz = np.maximum(f, 0) / 1e9
        loss_db = attenuation_db_m_1ghz * length_m * (0.9 * np.sqrt(f_ghz) + 0.1 * f_ghz)
        transmission = 10 ** (-loss_db / 20) * np.exp(-2j * np.pi * f * delay)
        gamma = 10 ** (-(return_loss_db - 0.3 * f_ghz) / 20)
        s = np.empty((len(f), 2, 2), dtype=complex)
        s[:, 1, 0] = s[:, 0, 1] = transmission
        s[:, 0, 0] = gamma * np.exp(1j * (phase1 - 2 * np.pi * f * 0.05e-9))
        s[:, 1, 1] = gamma * np.exp(1j * (phase2 - 2 * np.pi * f * 0.07e-9))
        return s

    return model


def reflection_model(gamma: complex) -> OnePortModel:
    return lambda f: np.full(len(f), gamma, dtype=complex)


def lisn_model(network: str, line_index: int = 0, z0: float = 50.0) -> OnePortModel:
    """Reflection at the EUT terminal of a V-AMN: nominal network with a small per-line spread."""
    if network not in LISN_NETWORKS:
        network = "50Ω‖(50µH+5Ω)"
    spread = 1 + 0.015 * line_index

    def model(f: np.ndarray) -> np.ndarray:
        return impedance_to_gamma(lisn_impedance(network, f, spread), z0)

    return model


def coupler_model(coupling_db: float = 20.0, directivity_db: float = 20.0, seed: int = 0) -> Callable[[np.ndarray], np.ndarray]:
    """Directional coupler, ports In, Out, Coupl. Fwd, Coupl. Rev (S has shape (points, 4, 4)).

    Coupling ``coupling_db`` with a small ripple, main-line loss growing with
    √f, directivity around ``directivity_db`` and input/output reflections of
    about −25 dB. Reciprocal and symmetric (S32 = S41, S42 = S31).
    """
    rng = np.random.default_rng(seed)
    phases = rng.uniform(-np.pi, np.pi, 6)

    def model(f: np.ndarray) -> np.ndarray:
        f_ghz = np.maximum(f, 0) / 1e9
        coupling = coupling_db + 0.4 * np.sin(f_ghz / 2.3 + phases[0])
        c = 10 ** (-coupling / 20)
        loss_db = 0.15 + 0.12 * np.sqrt(f_ghz)
        through = np.sqrt(1 - c**2) * 10 ** (-loss_db / 20)
        directivity = directivity_db + 3 * np.cos(f_ghz / 3.1 + phases[1]) - 0.1 * f_ghz
        leak = 10 ** (-(coupling + directivity) / 20)
        delay = np.exp(-2j * np.pi * f * 0.35e-9)
        s = np.zeros((len(f), 4, 4), dtype=complex)
        for (i, j), value in {
            (0, 1): through * delay,
            (0, 2): c * delay * np.exp(1j * phases[2]),
            (1, 3): c * delay * np.exp(1j * phases[2]),
            (0, 3): leak * np.exp(1j * (phases[3] - 2 * np.pi * f * 0.2e-9)),
            (1, 2): leak * np.exp(1j * (phases[3] - 2 * np.pi * f * 0.2e-9)),
            (2, 3): 10 ** (-45 / 20) * np.exp(1j * phases[4]),
        }.items():
            s[:, i, j] = s[:, j, i] = value
        for port in range(4):
            gamma_db = -26 + 0.25 * f_ghz + 2 * np.sin(f_ghz + port + phases[5])
            s[:, port, port] = 10 ** (gamma_db / 20) * np.exp(1j * (phases[5] + port - 2 * np.pi * f * 0.08e-9))
        return s

    return model


def pair_model(model: Callable[[np.ndarray], np.ndarray], first: int, second: int) -> TwoPortModel:
    """Two-port seen by the VNA between two ports of a multiport (the others terminated in 50 Ω)."""
    index = [first, second]
    return lambda f: model(f)[:, index][:, :, index]


@dataclass
class BenchState:
    """What is physically connected to the analyzer ports (simulation)."""

    reflections: dict[int, OnePortModel]
    two_port: tuple[tuple[int, int], TwoPortModel] | None = None
    description: str = "nada conectado"


def open_ports(port_count: int) -> BenchState:
    return BenchState(reflections={p: reflection_model(1.0) for p in range(1, port_count + 1)}, description="puertos abiertos")


def unknown_through_model() -> TwoPortModel:
    """Reciprocal adapter used as unknown through in the simulated UOSM calibrations."""
    return cable_model(0.025, 0.4, return_loss_db=30.0, velocity=0.8, seed=11)


def simulated_dut(library, equipment, line_index: int = 0) -> tuple[int, Callable[[np.ndarray], np.ndarray]]:
    """Model of a DUT for the simulated VNA built from its S-parameter file or its fields.

    Returns ``(ports, model)``: 2 → ``S(f)`` of a two-port, 1 → ``Γ(f)``, 4 → ``S(f)`` of a
    directional coupler (``pair_model`` gives each measured pair).
    """
    category = library.category_id_of(equipment)
    if category == "acoplador":
        coupling = float(library.field(equipment, "acoplamiento_nominal_db", 20.0) or 20.0)
        directivity = float(library.field(equipment, "directividad_min_db", 15.0) or 15.0) + 9.0
        return 4, coupler_model(coupling, directivity, seed=sum(map(ord, equipment.id)) % 997)
    if equipment.s_parameters is not None:
        data = library.touchstone_of(equipment)
        if data.n_ports == 2:
            return 2, touchstone_model(data)
    seed = sum(map(ord, equipment.id)) % 997
    if category in ("cable", "cable_medida", "path"):
        length = float(library.field(equipment, "longitud_m", 1.0) or 1.0)
        attenuation = float(library.field(equipment, "atenuacion_db_m_1ghz", 0.5) or 0.5)
        return 2, cable_model(length, attenuation, seed=seed)
    if category == "adaptador":
        return 2, cable_model(0.03, 0.4, return_loss_db=32.0, velocity=0.8, seed=seed)
    if category == "lisn":
        return 1, lisn_model(str(library.field(equipment, "red_nominal", "")), line_index)
    if category == "carga":
        kind = library.field(equipment, "tipo", "carga_50")
        gamma = {"cortocircuito": -1.0, "abierto": 1.0}.get(kind, 0.01)
        return 1, reflection_model(gamma)
    return 2, cable_model(1.0, 0.5, seed=seed)
