"""Error model and calibration algorithms (OSM, TOSM, UOSM) without noise."""

import numpy as np
import pytest

from calelektra.core.calibration import (
    STANDARD_GAMMA,
    CalibrationError,
    OnePortTerms,
    cascade,
    error_box,
    solve_one_port,
    solve_tosm,
    solve_uosm,
)
from calelektra.instruments.simulation import cable_model

F = np.linspace(1e6, 20e9, 301)


def port_terms(seed: int, f: np.ndarray = F) -> tuple[OnePortTerms, np.ndarray]:
    rng = np.random.default_rng(seed)
    a, b, c = rng.uniform(-np.pi, np.pi, 3)
    directivity = 0.05 * np.exp(1j * (a + f * 1e-10))
    source_match = 0.2 * np.exp(1j * (b - f * 3e-10))
    transmission = 0.8 * np.exp(1j * (c - 2 * np.pi * f * 3e-9))
    return OnePortTerms(directivity, source_match, transmission**2), transmission


def measure(network: np.ndarray, p1, p2) -> np.ndarray:
    a = error_box(p1[0], p1[1], facing_dut_at_port1=False)
    b = error_box(p2[0], p2[1], facing_dut_at_port1=True)
    return cascade(cascade(a, network), b)


def through() -> np.ndarray:
    s = np.zeros((len(F), 2, 2), dtype=complex)
    s[:, 0, 1] = s[:, 1, 0] = 1
    return s


def one_port_raw(terms: OnePortTerms) -> dict[str, np.ndarray]:
    points = len(terms.directivity)
    return {name: terms.measure(np.full(points, gamma)) for name, gamma in STANDARD_GAMMA.items()}


def test_osm_recovers_the_error_terms():
    terms, _ = port_terms(1)
    solved = solve_one_port(one_port_raw(terms))
    for name in ("directivity", "source_match", "tracking"):
        np.testing.assert_allclose(getattr(solved, name), getattr(terms, name), atol=1e-12)
    with pytest.raises(CalibrationError):
        solve_one_port({"OPEN": np.ones(3)})


@pytest.mark.parametrize("method", ["TOSM", "UOSM"])
def test_two_port_methods_recover_the_dut(method):
    p1, p2 = port_terms(1), port_terms(2)
    t1, t2 = solve_one_port(one_port_raw(p1[0])), solve_one_port(one_port_raw(p2[0]))
    if method == "TOSM":
        terms = solve_tosm(t1, t2, measure(through(), p1, p2))
    else:
        adapter = cable_model(0.03, 0.4, seed=7)(F)
        terms = solve_uosm(t1, t2, measure(adapter, p1, p2), F)
    dut = cable_model(1.5, 0.5, seed=3)(F)
    np.testing.assert_allclose(terms.correct(measure(dut, p1, p2)), dut, atol=1e-10)


def test_uosm_picks_the_right_root_with_a_long_unknown_through():
    # 2 m of cable: hundreds of phase turns; the sweep must sample them (phase step < π)
    f = np.linspace(1e6, 20e9, 4001)
    p1, p2 = port_terms(3, f), port_terms(4, f)
    t1, t2 = solve_one_port(one_port_raw(p1[0])), solve_one_port(one_port_raw(p2[0]))
    long_through = cable_model(2.0, 0.5, seed=5)(f)
    terms = solve_uosm(t1, t2, measure(long_through, p1, p2), f)
    np.testing.assert_allclose(terms.correct(measure(long_through, p1, p2)), long_through, atol=1e-10)
