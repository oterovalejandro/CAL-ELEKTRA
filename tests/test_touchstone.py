import numpy as np
import pytest

from calelektra.core.touchstone import Touchstone, TouchstoneError, read_touchstone, write_touchstone


def test_roundtrip(tmp_path):
    f = np.array([1e6, 1e9, 2e9])
    s = np.zeros((3, 2, 2), dtype=complex)
    s[:, 0, 0] = 0.1 + 0.05j
    s[:, 1, 0] = 0.9 - 0.1j
    s[:, 0, 1] = 0.8 - 0.2j
    s[:, 1, 1] = -0.05j
    path = tmp_path / "cable.s2p"
    write_touchstone(path, Touchstone(2, f, s, comments=["prueba"]))
    data = read_touchstone(path)
    assert data.n_ports == 2
    np.testing.assert_allclose(data.frequencies, f)
    np.testing.assert_allclose(data.s, s, atol=1e-9)
    assert data.frequency_range.stop == 2e9
    assert data.comments == ["prueba"]


def test_v1_ma_ghz_with_wrapping_and_noise(tmp_path):
    path = tmp_path / "amp.S2P"
    path.write_text(
        "! cabecera\n"
        "# GHZ S MA R 50\n"
        "1.0 0.5 0 10 90\n"
        "    0.1 0 0.4 180\n"  # wrapped line
        "2.0 0.5 0 10 90 0.1 0 0.4 180 ! comment\n"
        "1.0 2.5 0.3 45 0.5\n"  # noise parameters: frequency decreases
    )
    data = read_touchstone(path)
    assert len(data.frequencies) == 2
    assert data.frequencies[1] == 2e9
    assert data.s_param("S21")[0] == pytest.approx(10j)  # 10 ∠ 90°
    assert data.s_param("S22")[0] == pytest.approx(-0.4)
    assert data.s_param("S12")[0] == pytest.approx(0.1)


def test_db_format_one_port(tmp_path):
    path = tmp_path / "load.s1p"
    path.write_text("# MHZ S DB R 50\n10 -20 0\n20 -40 180\n")
    data = read_touchstone(path)
    assert data.n_ports == 1
    np.testing.assert_allclose(np.abs(data.s_param("S11")), [0.1, 0.01])
    np.testing.assert_allclose(data.s_db("S11"), [-20, -40])


def test_errors(tmp_path):
    bad = tmp_path / "x.s2p"
    bad.write_text("# GHZ S MA R 50\n1 a b\n")
    with pytest.raises(TouchstoneError):
        read_touchstone(bad)
    empty = tmp_path / "e.s2p"
    empty.write_text("! nada\n")
    with pytest.raises(TouchstoneError):
        read_touchstone(empty)
    with pytest.raises(TouchstoneError):
        read_touchstone(tmp_path / "noexiste.s2p")
