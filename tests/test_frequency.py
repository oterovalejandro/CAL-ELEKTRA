import pytest

from calelektra.core.frequency import FrequencyRange, RangeContribution, analyze_coverage, intersect_all
from calelektra.core.units import UnitError, format_frequency, parse_frequency


@pytest.mark.parametrize(
    "text, hz",
    [
        ("100 kHz", 100e3),
        ("100kHz", 100e3),
        ("43.5 GHz", 43.5e9),
        ("43,5 GHz", 43.5e9),
        ("18G", 18e9),
        ("10M", 10e6),
        ("5 Hz", 5.0),
        ("DC", 0.0),
        (1e6, 1e6),
    ],
)
def test_parse_frequency(text, hz):
    assert parse_frequency(text) == pytest.approx(hz)


@pytest.mark.parametrize("text", ["10 m", "abc", "-5 Hz", "5 parsecs", True])
def test_parse_frequency_rejects(text):
    with pytest.raises(UnitError):
        parse_frequency(text)


def test_format_frequency():
    assert format_frequency(43.5e9) == "43,5 GHz"
    assert format_frequency(100e3) == "100 kHz"
    assert format_frequency(5) == "5 Hz"
    assert format_frequency(26.5e9, decimal=".") == "26.5 GHz"


def test_range_parse_and_str():
    r = FrequencyRange.parse(["100 kHz", "43.5 GHz"])
    assert (r.start, r.stop) == (100e3, 43.5e9)
    assert str(FrequencyRange.parse("DC – 18 GHz")) == "DC – 18 GHz"
    assert FrequencyRange.parse({"inicio": "1 GHz", "fin": "18 GHz"}).stop == 18e9
    with pytest.raises(ValueError):
        FrequencyRange(10, 5)


def test_intersection():
    vna = FrequencyRange(100e3, 43.5e9)
    cable = FrequencyRange(5, 18e9)
    assert vna.intersect(cable) == FrequencyRange(100e3, 18e9)
    assert FrequencyRange(1, 2).intersect(FrequencyRange(3, 4)) is None
    assert intersect_all([vna, cable, FrequencyRange(0, 40e9)]) == FrequencyRange(100e3, 18e9)
    assert intersect_all([]) is None


def contribution(eq_id, start, stop, dut=False):
    return RangeContribution(role=eq_id.lower(), role_label=eq_id, equipment_id=eq_id, range=FrequencyRange(start, stop), is_dut=dut)


def test_coverage_user_example_vna_and_18g_cable():
    """VNA 100 kHz–43 GHz + cable 5 Hz–18 GHz → effective 100 kHz–18 GHz."""
    required = FrequencyRange(100e3, 26.5e9)
    result = analyze_coverage(required, [contribution("VNA", 100e3, 43.5e9), contribution("CABLE", 5, 18e9)])
    assert result.effective == FrequencyRange(100e3, 18e9)
    assert not result.covered
    assert [c.equipment_id for c in result.upper_limiting] == ["CABLE"]
    assert [c.equipment_id for c in result.lower_limiting] == ["VNA"]
    assert result.uncovered == [FrequencyRange(18e9, 26.5e9)]


def test_coverage_ok_and_dut_does_not_limit():
    required = FrequencyRange(100e3, 18e9)
    result = analyze_coverage(
        required,
        [contribution("VNA", 100e3, 43.5e9), contribution("CABLE", 5, 18e9), contribution("DUT", 0, 10e9, dut=True)],
    )
    assert result.covered
    assert result.effective == FrequencyRange(100e3, 18e9)
    assert [c.equipment_id for c in result.dut_outside] == ["DUT"]


def test_coverage_disjoint_and_empty():
    required = FrequencyRange(1e6, 1e9)
    disjoint = analyze_coverage(required, [contribution("A", 1, 10), contribution("B", 1e3, 1e4)])
    assert disjoint.effective is None and not disjoint.covered
    assert analyze_coverage(required, []).uncovered == [required]
