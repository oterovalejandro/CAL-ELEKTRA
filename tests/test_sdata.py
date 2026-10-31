"""S-parameter data with uncertainties: the files of METAS VNA Tools (.sdatb, .sdatcv) and Touchstone.

The two .sdatb are files of VNA Tools given by the laboratory: a load of 9 kHz – 45 GHz characterized
from a data set of standards (2015) and an adapter of 0.1 – 40 GHz measured with a calibration that used
those data (2021).
"""

from __future__ import annotations

import gzip
import math
import re
from pathlib import Path

import numpy as np
import pytest

from calelektra.core.touchstone import read_touchstone
from calelektra.sdata import (
    ConversionTerm,
    Distribution,
    FrequencyConversion,
    Input,
    PortDescription,
    SDataError,
    SParamData,
    UncNumber,
    UncVector,
    load_sparam_data,
    read_sdatb,
    read_sdatcv,
    save_sparam_data,
    sdatb_version,
    write_sdatb,
)
from calelektra.sdata.__main__ import main
from calelektra.sdata.sdatb import sdatb_content
from calelektra.sdata.sdatcv import sdatcv_text
from calelektra.sdata.unclib import (
    BinaryReader,
    BinaryWriter,
    cross_covariance,
    read_unc_number,
    write_unc_number,
)

DATA = Path(__file__).resolve().parent / "datos" / "metas"
LOAD = DATA / "Example_Loadf_18831_20150824.sdatb"
ADAPTER = DATA / "Example_Ada_ABf-f_101388_20211217.sdatb"


@pytest.fixture(scope="module")
def load() -> SParamData:
    return read_sdatb(LOAD)


@pytest.fixture(scope="module")
def adapter() -> SParamData:
    return read_sdatb(ADAPTER)


def test_the_load_of_vna_tools(load):
    assert sdatb_version(LOAD) == 2
    assert (load.n_points, load.n_ports, load.ports) == (533, 1, (PortDescription(1),))
    assert (load.frequencies[0], load.frequencies[-1]) == (9e3, 45e9)
    assert load.zr.tolist() == [50] and load.conversions is None
    assert load.s[0, 0, 0] == pytest.approx(-0.000543 - 0.000001j, abs=5e-7)
    u_re, u_im = load.standard_uncertainty()
    assert u_re.shape == (533, 1, 1)
    assert (u_re.max(), u_im.max()) == (pytest.approx(0.00418, abs=5e-6), pytest.approx(0.00452, abs=5e-6))
    # 24 contributions per frequency, standard normal, shared by its real and imaginary parts
    vector = load.vector
    assert len(vector.inputs) == 24 * 533 and all(item.distribution.name == "StandardNormal" for item in vector.inputs)
    for point in (0, 266, 532):
        indices, _ = vector.dependencies(load.element(point, 0, 0, 1))
        assert len(indices) == 24
        assert {re.search(r"Frequency Index: (\d+)", vector.inputs[i].description).group(1) for i in indices} == {str(point)}
    covariance = load.point_covariance(266)
    assert np.sqrt(np.diag(covariance)) == pytest.approx([u_re[266, 0, 0], u_im[266, 0, 0]], rel=1e-12)


def test_the_files_of_vna_tools_are_written_back_byte_for_byte(load, adapter, tmp_path):
    assert sdatb_content(load) == LOAD.read_bytes()
    assert sdatb_content(adapter) == ADAPTER.read_bytes()
    copy = write_sdatb(tmp_path / "copia.sdatb", load)
    assert copy.read_bytes() == LOAD.read_bytes()


def test_the_adapter_and_its_correlation_with_the_load(load, adapter):
    assert (adapter.n_points, adapter.n_ports) == (400, 2)
    assert (adapter.frequencies[0], adapter.frequencies[-1]) == (1e8, 4e10)
    assert np.abs(adapter.s[:, 1, 0] - adapter.s[:, 0, 1]).max() < 2e-3  # S21 ≈ S12: reciprocal
    u_re, u_im = adapter.standard_uncertainty()
    assert u_re[:, 1, 0].max() == pytest.approx(0.00748, abs=5e-6) and u_im[:, 0, 0].max() == pytest.approx(0.00323, abs=5e-6)
    kinds = {item.distribution.name for item in adapter.vector.inputs}
    assert kinds == {"StandardNormal", "Normal"}
    # the inputs of the load data are in the adapter: the files are correlated
    shared = {item.id for item in load.vector.inputs} & {item.id for item in adapter.vector.inputs}
    assert len(shared) == 9598
    k_load, k_adapter = int(np.argmin(abs(load.frequencies - 10.1e9))), int(np.argmin(abs(adapter.frequencies - 10.1e9)))
    elements_load = [load.element(k_load, 0, 0, part) for part in (0, 1)]
    elements_adapter = [adapter.element(k_adapter, 0, 0, part) for part in (0, 1)]
    covariance = cross_covariance(load.vector, elements_load, adapter.vector, elements_adapter)
    assert np.abs(covariance).min() > 1e-10
    same = cross_covariance(load.vector, elements_load, load.vector, elements_load)
    assert same == pytest.approx(load.vector.covariance(elements_load), rel=1e-12)


def test_sdatcv_keeps_the_values_and_the_covariance_of_each_frequency(load, tmp_path):
    path = save_sparam_data(tmp_path / "carga.sdatcv", load)
    lines = path.read_bytes().decode("ascii").split("\r\n")
    assert lines[:5] == ["SDATCV", "Ports", "1", "Zr[1] re\tZr[1] im", "50.0\t0.0"]
    assert lines[5].split("\t") == ["Freq", "S[1,1] re", "S[1,1] im", "CV[1,1]", "CV[2,1]", "CV[1,2]", "CV[2,2]"]
    back = read_sdatcv(path)
    assert np.array_equal(back.frequencies, load.frequencies) and np.array_equal(back.s, load.s)
    for point in (0, 100, 532):
        assert back.point_covariance(point) == pytest.approx(load.point_covariance(point), rel=1e-9, abs=1e-20)
    # the correlation between frequencies is lost: each one has its own inputs
    first, second = (set(back.vector.dependencies(back.element(k, 0, 0, 0))[0].tolist()) for k in (0, 1))
    assert first and second and not first & second
    assert back.vector.inputs[0].description == "Data Set\tcarga.sdatcv\tFrequency Index: 0, Contribution: 0"


def test_sdatcv_order_of_the_covariance_matrix_of_two_ports(tmp_path):
    # variances 1…8 (×1e-6) in the order of the .sdatcv: S11 re, S11 im, S21 re, S21 im, S12 re, S12 im, S22 re, S22 im
    covariance = np.diag(np.arange(1, 9) * 1e-6)
    covariance[0, 7] = covariance[7, 0] = 0.5e-6  # S11 re with S22 im: CV[1,8]
    s = np.array([[[0.1 + 0.2j, 0.3 + 0.4j], [0.5 + 0.6j, 0.7 + 0.8j]]])  # [receiver, source]
    data = SParamData.from_covariances([1e9], s, [covariance])
    u_re, u_im = data.standard_uncertainty()
    assert u_re[0, 1, 0] ** 2 == pytest.approx(3e-6) and u_im[0, 0, 1] ** 2 == pytest.approx(6e-6)  # S21 re, S12 im
    assert data.vector.covariance([data.element(0, 0, 0, 0), data.element(0, 1, 1, 1)])[0, 1] == pytest.approx(0.5e-6)
    lines = sdatcv_text(data).split("\r\n")
    header, row = lines[5].split("\t"), lines[6].split("\t")
    assert header[1:9] == ["S[1,1] re", "S[1,1] im", "S[2,1] re", "S[2,1] im", "S[1,2] re", "S[1,2] im", "S[2,2] re", "S[2,2] im"]
    assert row[1:9] == ["0.1", "0.2", "0.5", "0.6", "0.3", "0.4", "0.7", "0.8"]
    assert float(row[header.index("CV[3,3]")]) == pytest.approx(3e-6) and float(row[header.index("CV[1,8]")]) == pytest.approx(0.5e-6)
    back = read_sdatcv(sdatcv_text(data))
    assert np.array_equal(back.s, s) and back.point_covariance(0) == pytest.approx(covariance, rel=1e-9, abs=1e-20)


SPEC_1_PORT = """SDATCV
Ports
1
Zr[1] re\tZr[1] im
50.0\t0.0
% comentario: §4.2.1 de «METAS VNA Tools - Data Formats V3.0.2»
Freq\tS[1,1] re\tS[1,1] im\tCV[1,1]\tCV[2,1]\tCV[1,2]\tCV[2,2]
1.00e+9\t-9.16e-1\t3.91e-1\t1.39e-6\t3.56e-7\t3.56e-7\t2.05e-6
2.00e+9\t-6.90e-1\t7.17e-1\t1.98e-6\t2.47e-7\t2.47e-7\t1.96e-6 % tras los datos
3.00e+9\t-3.55e-1\t9.29e-1\t2.58e-6\t3.88e-7\t3.88e-7\t1.74e-6
"""

SPEC_2_PORTS_REDUCED = (
    "sdatcv\nports\n1\t2\nZr[1] re\tZr[1] im\tZr[2] re\tZr[2] im\n50.0\t0.0\t50.0\t0.0\n"
    "Freq\tS[1,1] re\tS[1,1] im\tS[2,1] re\tS[2,1] im\tS[1,2] re\tS[1,2] im\tS[2,2] re\tS[2,2] im\t"
    "CV[1,1]\tCV[2,1]\tCV[2,2]\tCV[3,3]\tCV[4,3]\tCV[3,4]\tCV[4,4]\tCV[5,5]\tCV[6,5]\tCV[5,6]\tCV[6,6]\tCV[7,7]\tCV[8,7]\tCV[7,8]\tCV[8,8]\n"
    "1.00e+9\t-3.72e-3\t5.39e-3\t2.35e-1\t-2.13e-1\t2.35e-1\t-2.14e-1\t-3.90e-3\t6.39e-3\t"
    "8.00e-8\t-1.32e-9\t7.86e-8\t4.48e-8\t2.69e-8\t2.69e-8\t4.98e-8\t4.50e-8\t2.70e-8\t2.70e-8\t5.00e-8\t8.46e-8\t4.22e-11\t4.22e-11\t8.55e-8\n"
)


def test_sdatcv_examples_of_the_specification():
    one = read_sdatcv(SPEC_1_PORT)
    assert one.frequencies.tolist() == [1e9, 2e9, 3e9] and one.s[1, 0, 0] == -0.69 + 0.717j
    assert one.point_covariance(0) == pytest.approx(np.array([[1.39e-6, 3.56e-7], [3.56e-7, 2.05e-6]]), rel=1e-9)
    two = read_sdatcv(SPEC_2_PORTS_REDUCED)
    assert two.s[0, 1, 0] == 0.235 - 0.213j and two.s[0, 0, 1] == 0.235 - 0.214j  # S21 and S12
    covariance = two.point_covariance(0)
    assert covariance[0, 1] == covariance[1, 0] == pytest.approx(-1.32e-9)  # CV[1,2] by symmetry
    assert covariance[0, 2] == 0 and covariance[3, 2] == pytest.approx(2.69e-8)
    # uncorrelated S-parameters do not share inputs
    groups = [set(two.vector.dependencies(two.element(0, r, c, 0))[0].tolist()) for r in (0, 1) for c in (0, 1)]
    assert all(len(group) == 2 for group in groups) and len(set().union(*groups)) == 8


def test_versions_1_and_3_to_5(tmp_path):
    shared = Input(b"\x01" * 16, "kit", Distribution(3, (-0.01, 0.01)))
    numbers = [UncNumber(50.0), UncNumber(0.0), UncNumber(50.0), UncNumber(0.0)]
    for k in range(2 * 2 * 2 * 2):  # 2 frequencies × 2 × 2 parameters × (re, im)
        numbers.append(UncNumber(0.01 * k, [(shared, 0.1 * (k % 3)), (Input(bytes([k + 2]) * 16, f"ruido {k}", Distribution(1, (0.0, 1e-4))), 1.0)]))
    vector = UncVector.from_numbers(numbers)
    differential = (PortDescription(1, 1), PortDescription(1, 2, 2))
    cases = {
        2: SParamData([1e9, 2e9], (PortDescription(1), PortDescription(2)), vector),
        3: SParamData([1e9, 2e9], differential, vector),
        4: SParamData([1e9, 2e9], differential, vector, (FrequencyConversion(*(3 * [ConversionTerm(1, 2, 1e6)])),) * 2),
        5: SParamData([1e9, 2e9], differential, vector, (FrequencyConversion(ConversionTerm(2, 1, 0), ConversionTerm(), ConversionTerm(1, 1, -5e5)),) * 2),
    }
    for version, data in cases.items():
        assert data.required_version == version
        path = write_sdatb(tmp_path / f"v{version}.sdatb", data)
        assert sdatb_version(path) == version
        back = read_sdatb(path)
        assert back.ports == data.ports and back.conversions == data.conversions
        assert np.array_equal(back.vector.values, vector.values)
        assert back.vector.standard_uncertainties() == pytest.approx(vector.standard_uncertainties(), rel=1e-15)
    assert [p.label for p in differential] == ["1d", "1c:II"] and PortDescription.parse("1c:II") == differential[1]
    # version 1: GZIP and a complex number with its inputs per element
    path = write_sdatb(tmp_path / "v1.sdatb", cases[2], version=1)
    assert path.read_bytes()[:2] == b"\x1f\x8b" and sdatb_version(path) == 1
    back = read_sdatb(path)
    assert back.vector.covariance(range(4, 20)) == pytest.approx(vector.covariance(range(4, 20)), rel=1e-12, abs=1e-24)
    # any version may come compressed
    (tmp_path / "v2.gz.sdatb").write_bytes(gzip.compress((tmp_path / "v2.sdatb").read_bytes()))
    assert np.array_equal(read_sdatb(tmp_path / "v2.gz.sdatb").s, cases[2].s)
    with pytest.raises(SDataError, match="hace falta la 3"):
        sdatb_content(cases[3], version=2)


def test_touchstone(load, tmp_path):
    path = save_sparam_data(tmp_path / "carga.s1p", load, comments=["de VNA Tools"])
    touchstone = read_touchstone(path)
    assert touchstone.reference_impedance == 50 and np.allclose(touchstone.s, load.s, rtol=0, atol=1e-9)
    values = load_sparam_data(path)  # without uncertainty
    assert not values.vector.inputs and values.standard_uncertainty()[0].max() == 0
    back = read_sdatb(write_sdatb(tmp_path / "carga_valores.sdatb", values))
    assert np.allclose(back.s, load.s, rtol=0, atol=1e-9)
    with pytest.raises(SDataError, match="es de 2 puertos y los datos de 1"):
        save_sparam_data(tmp_path / "carga.s2p", load)
    complex_zr = SParamData.from_values([1e9], np.zeros((1, 1, 1)), zr=50 + 1j)
    with pytest.raises(SDataError, match="Touchstone no admite"):
        complex_zr.to_touchstone()


def test_uncertainty_numbers_of_unclib():
    # the example of METAS UncLib: c = √(a² + b²) with a = 3 ± 0.3 and b = 4 ± 0.4
    a = Input(bytes.fromhex("5C09F458A14A40D3B5141394AEBCCA25"), "side a", Distribution(1, (3.0, 0.3)))
    b = Input(bytes.fromhex("EABF674B28324084A48364301D7C6AF5"), "side b", Distribution(1, (4.0, 0.4)))
    c = UncNumber(5.0, [(a, 0.6), (b, 0.8)])
    assert c.stdunc() == pytest.approx(0.3671511950137164, rel=1e-15)
    w = BinaryWriter()
    write_unc_number(w, c)
    assert w.bytes()[:12].hex() == "020000000000001440020210"  # version 2, 5.0, 2 dependencies; input: version 2, 16-byte id
    assert read_unc_number(BinaryReader(w.bytes())) == c
    w = BinaryWriter()
    for value in (0, 127, 128, 16383, 16384, 2**31 - 1):
        w.int7(value)
    assert w.bytes()[:4] == b"\x00\x7f\x80\x01"
    r = BinaryReader(w.bytes())
    assert [r.int7() for _ in range(6)] == [0, 127, 128, 16383, 16384, 2**31 - 1]
    repeated = UncVector([1.0, 2.0], [a, b], [0, 3, 3], [0, 0, 1], [0.1, 0.2, 0.5])  # input a twice in the first element
    assert repeated.standard_uncertainties() == pytest.approx([math.hypot(0.3 * 0.3, 0.5 * 0.4), 0.0])
    assert repeated.covariance([0])[0, 0] == pytest.approx(repeated.standard_uncertainties()[0] ** 2)
    uniform, triangular = Distribution(3, (-1.0, 1.0)), Distribution(6, (-1.0, 1.0))
    assert (uniform.stdunc(), triangular.stdunc()) == (pytest.approx(2 / math.sqrt(12)), pytest.approx(2 / math.sqrt(24)))
    assert Distribution(10, (0.0, 1.0, 10.0)).stdunc() == pytest.approx(math.sqrt(10 / 8))


def test_errors(tmp_path):
    with pytest.raises(SDataError, match="falta la cabecera"):
        read_sdatb(b"\x06%SDATX" + bytes(20))
    with pytest.raises(SDataError, match="Versión 9"):
        read_sdatb(b"\x06%SDATA" + (9).to_bytes(4, "little") + bytes(8))
    with pytest.raises(SDataError, match="truncado"):
        read_sdatb(LOAD.read_bytes()[:100_000])
    with pytest.raises(SDataError, match="Faltan columnas"):
        read_sdatcv("SDATCV\nPorts\n1\nZr[1] re\tZr[1] im\n50\t0\nFreq\tS[1,1] re\n1e9\t0.1\n")
    with pytest.raises(SDataError, match="formato no admitido"):
        load_sparam_data(tmp_path / "datos.csv")


def test_command_line(capsys, tmp_path):
    target = tmp_path / "carga.sdatcv"
    assert main([str(LOAD), "--convertir", str(target)]) == 0
    output = capsys.readouterr().out
    assert "Formato: .sdatb versión 2" in output and "Frecuencias: 533, de 9 kHz a 45 GHz" in output
    assert "12792 entradas" in output and target.read_text(encoding="ascii").startswith("SDATCV")
    assert main([str(tmp_path / "no_existe.sdatb")]) == 1
    assert "Error" in capsys.readouterr().err
