from __future__ import annotations

import numpy as np
import pytest

from calelektra.uncertainty.impedance import Z0, impedance_uncertainty

K = 2.0


def z_of(gamma):
    return Z0 * (1 + gamma) / (1 - gamma)


def monte_carlo(gamma, u_rho, u_phi_deg, n=400_000, seed=1):
    """Expanded uncertainty (k = 2) of |Z| and ∠Z with Gaussian errors of |Γ| and φ (u = U/k).

    Same model as the module: the tangential error is never smaller than the radial one.
    """
    rng = np.random.default_rng(seed)
    u_phi = max(np.radians(u_phi_deg), u_rho / abs(gamma))
    rho = abs(gamma) + rng.normal(0, u_rho / K, n)
    phi = np.angle(gamma) + rng.normal(0, u_phi / K, n)
    z = z_of(rho * np.exp(1j * phi))
    nominal = z_of(gamma)
    return K * np.std(np.abs(z)), K * np.degrees(np.std(np.angle(z / nominal)))


@pytest.mark.parametrize(
    ("gamma", "u_rho", "u_phi"),
    [
        (complex(-0.5, 0.1), 0.01, 1.0),  # Z ≈ 16 Ω
        (0.3 * np.exp(1j * np.radians(40)), 0.01, 2.0),
        (0.95 * np.exp(1j * np.radians(10)), 0.004, 0.25),  # high impedance, still linear
    ],
)
def test_first_order_agrees_with_monte_carlo(gamma, u_rho, u_phi):
    result = impedance_uncertainty(np.array([gamma]), np.array([u_rho]), np.array([u_phi]), magnitude_unit="lineal")
    u_ohm, u_deg = monte_carlo(gamma, u_rho, u_phi)
    assert not result.exact[0]
    assert result.magnitude_plus[0] == result.magnitude_minus[0] == pytest.approx(u_ohm, rel=0.03)
    assert result.phase_plus[0] == pytest.approx(u_deg, rel=0.03)
    assert result.magnitude_db_plus[0] == pytest.approx(8.686 * result.magnitude_plus[0] / abs(z_of(gamma)), rel=1e-3)


def test_magnitude_in_db_or_linear_gives_the_same():
    gamma = np.array([0.0027 * np.exp(1j * 0.3), 0.3185 * np.exp(1j * 1.2)])
    u_lin = np.array([0.008, 0.010])
    u_db = u_lin / (np.abs(gamma) * np.log(10) / 20)  # what the engine gives in dB (26 dB at −51.5 dB)
    a = impedance_uncertainty(gamma, u_db, np.array([10.0, 2.0]), "dB")
    b = impedance_uncertainty(gamma, u_lin, np.array([10.0, 2.0]), "lineal")
    assert np.allclose(a.magnitude_plus, b.magnitude_plus) and np.allclose(a.phase_plus, b.phase_plus)
    assert u_db[0] > 25  # 26 dB at −51.5 dB: the dB value is useless there, the linear one is not


def test_low_impedance_uses_the_exact_bounds_close_to_the_first_order():
    # LISN at 9 kHz: Z ≈ 5 Ω, r = 0.024 is 13 % of |1 + Γ| = 0.18 → exact bounds, a little wider and not symmetric
    gamma = complex(-0.818, 0.02)
    result = impedance_uncertainty(np.array([gamma]), np.array([0.022]), np.array([1.7]), "lineal")
    u_ohm, u_deg = monte_carlo(gamma, 0.022, 1.7)
    assert result.exact[0] and result.magnitude_plus[0] != pytest.approx(result.magnitude_minus[0], rel=1e-3)
    assert u_ohm < result.magnitude_minus[0] < result.magnitude_plus[0] < 1.15 * u_ohm
    assert u_deg < result.phase_plus[0] < 1.15 * u_deg


def test_near_match_the_error_of_gamma_points_anywhere():
    # |Γ| = 0.0027 < U_ρ = 0.008 and the file gives only 10° of phase: the result must not depend on φ
    results = [impedance_uncertainty(np.array([0.0027 * np.exp(1j * phi)]), np.array([0.008]), np.array([10.0]), "lineal") for phi in (0.1, 0.8, 2.0)]
    for result in results:
        assert result.magnitude_plus[0] == pytest.approx(2 * 0.008 * abs(result.impedance[0]), rel=0.01)  # |B| U_ρ |Z|
        assert result.phase_plus[0] == pytest.approx(np.degrees(2 * 0.008), rel=0.01)


def test_exact_bounds_near_the_open():
    gamma = 0.99 * np.exp(1j * np.radians(2))
    u_rho, u_phi = 0.01, 0.6
    result = impedance_uncertainty(np.array([gamma]), np.array([u_rho]), np.array([u_phi]), "lineal")
    assert result.exact[0]
    r = max(u_rho, 0.99 * np.radians(u_phi))
    rng = np.random.default_rng(0)
    disk = gamma + r * np.sqrt(rng.uniform(0, 1, 400_000)) * np.exp(1j * rng.uniform(0, 2 * np.pi, 400_000))
    z = z_of(disk)
    nominal = z_of(gamma)
    assert abs(nominal) + result.magnitude_plus[0] == pytest.approx(np.abs(z).max(), rel=1e-3)
    assert abs(nominal) - result.magnitude_minus[0] == pytest.approx(np.abs(z).min(), rel=1e-3)
    angles = np.degrees(np.angle(z / nominal))
    assert result.phase_plus[0] == pytest.approx(angles.max(), rel=1e-2) and result.phase_minus[0] == pytest.approx(-angles.min(), rel=1e-2)
    assert result.magnitude_plus[0] > 1.5 * result.magnitude_minus[0]  # not symmetric
    assert result.magnitude_db_plus[0] == pytest.approx(20 * np.log10(np.abs(z).max() / abs(nominal)), rel=1e-3)
    assert result.summary()["puntos_cota_exacta"] == 1


def test_points_without_bound_or_without_uncertainty():
    gamma = np.array([0.995, 0.5, 0.2])
    result = impedance_uncertainty(gamma, np.array([0.01, np.nan, 0.01]), np.array([1.0, 1.0, 1.0]), "lineal")
    assert np.isnan(result.magnitude_plus[0]) and np.isnan(result.phase_plus[0])  # the disk holds the open: Z not bounded
    assert np.isnan(result.magnitude_plus[1])  # no uncertainty of Γ
    assert np.isfinite(result.magnitude_plus[2])
    summary = result.summary()
    assert summary["puntos_sin_incertidumbre"] == 2 and summary["z0_ohm"] == 50.0 and summary["puntos"] == 3
