"""Uncertainty of a VNA S-parameter measurement from the uncertainty data file of the VNA.

Port of the laboratory algorithm ``DEF_vna_uncertainty_s_parameters_calculation.py``
(ALTER TECHNOLOGY CAL EC-LB). The calculation is the same:

1. The uncertainty data file (YAML, ``uncertainty_sources``) of one VNA unit lists
   its contributions: datasheet transmission/reflection uncertainty, trace noise,
   temperature stability, certificate values…
2. For a parameter (S11, S21, S12, S22), a frequency and a measured value, each
   source that applies is looked up in its frequency range and, when it has level
   ranges, in the range of the measured magnitude (dB or linear, whatever the
   file uses). There is no extrapolation: a value outside the ranges of a source
   makes the result NaN.
3. Each value is turned into a standard uncertainty: expanded → U/k, RMS → as is,
   temperature coefficients → coefficient × ΔT (outside the reference range of
   the file) / √3. dB and linear magnitudes are converted when needed.
4. The contributions are combined by RSS and expanded with the requested k.

A source with an IF bandwidth condition only applies when the IF bandwidth used
is equal to or greater than the one of the condition.

Changes with respect to the original script: messages in Spanish, the date of
the calibration check can be given (``today``) and the engine can be built from
data already loaded. The command line is in ``calelektra.uncertainty.__main__``.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

import yaml


class UncertaintyCalculationError(Exception):
    """Raised when the uncertainty engine cannot calculate a valid result."""


UNIT_ALIASES = {
    "db": "magnitude_db",
    "db_rms": "magnitude_db",
    "deg": "phase_deg",
    "deg_rms": "phase_deg",
    "linear": "magnitude_linear",
    "lin": "magnitude_linear",
    "ratio": "magnitude_linear",
    "hz": "frequency",
    "db_per_k": "magnitude_db_per_k",
    "db_k": "magnitude_db_per_k",
    "deg_per_k": "phase_deg_per_k",
    "deg_k": "phase_deg_per_k",
    "degree_per_k": "phase_deg_per_k",
    "degree_k": "phase_deg_per_k",
    "deg_per_ghz_per_k": "phase_deg_per_k",
    "degree_per_ghz_per_k": "phase_deg_per_k",
    "deg_ghz_k": "phase_deg_per_k",
    "degree_ghz_k": "phase_deg_per_k",
}

UNIT_FAMILIES = {
    "magnitude_db",
    "phase_deg",
    "magnitude_linear",
    "frequency",
    "magnitude_db_per_k",
    "phase_deg_per_k",
}

# Source family used only to decide applicability. A source with
# source_family/unit_family «magnitude_all» applies to both magnitude_db and
# magnitude_linear; its value is read in the unit declared in source["unit"]
# and converted to the requested output family when needed.
MAGNITUDE_ALL_SOURCE_FAMILY = "magnitude_all"
MAGNITUDE_OUTPUT_FAMILIES = {"magnitude_db", "magnitude_linear"}

VALUE_FIELDS_BY_UNIT_FAMILY = {
    "magnitude_db": ["magnitude_db", "uncertainty_db", "measurement_uncertainty_db", "spec_max_db", "measured_db"],
    "phase_deg": ["phase_deg", "uncertainty_deg", "measurement_uncertainty_deg"],
    "magnitude_linear": ["magnitude_linear", "linear_magnitude", "uncertainty_linear"],
    "frequency": ["uncertainty_hz", "measurement_uncertainty_hz"],
    "magnitude_db_per_k": ["magnitude_db_per_k", "coefficient_db_per_k"],
    "phase_deg_per_k": ["phase_deg_per_k", "phase_deg_per_ghz_per_k", "coefficient_deg_per_k"],
}

LEVEL_LIMIT_FIELDS_BY_UNIT_FAMILY = {
    "magnitude_db": ("level_min_db", "level_max_db"),
    "magnitude_linear": ("level_min_linear", "level_max_linear"),
    "phase_deg": ("level_min_deg", "level_max_deg"),
    "frequency": ("level_min_hz", "level_max_hz"),
}

PARAMETERS = ("S11", "S22", "S21", "S12", "FREQUENCY")


@dataclass
class Contribution:
    source_id: str
    quantity: str
    source_type: str
    uncertainty_type: str
    distribution: str
    unit_family: str
    raw_field: str
    raw_value: float
    raw_unit: str
    standard_uncertainty: float
    sensitivity_coefficient: float
    coverage_factor_source: float
    frequency_range: dict[str, Any]
    level_range: dict[str, Any] | None = None
    selection_rule: str = "range_match"
    comment: str | None = None


@dataclass
class UncertaintyResult:
    equipment: str
    parameter: str
    frequency_hz: float
    input_value: float
    input_unit: str
    input_unit_family: str
    output_unit_family: str
    level_value: float
    level_unit: str
    level_unit_family: str
    temperature_min_c: float | None
    temperature_max_c: float | None
    temperature_delta_k_used: float
    coverage_factor: float
    combined_standard_uncertainty: float
    expanded_uncertainty: float
    contributions: list[Contribution]
    calculation_date: str = field(default_factory=lambda: date.today().isoformat())
    calibration_due_date: str | None = None
    calibration_valid: bool | None = None
    warnings: list[str] = field(default_factory=list)
    out_of_limits: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False, allow_nan=True)


def normalize_parameter(parameter: str) -> str:
    p = str(parameter).strip().upper()
    if p not in PARAMETERS:
        raise UncertaintyCalculationError(f"Parámetro «{parameter}» no admitido: use S11, S22, S21, S12 o frequency.")
    return p


def normalize_unit_token(unit: str) -> str:
    u = str(unit).strip()
    u = u.replace("°", "deg").replace("º", "deg")
    u = u.lower()
    u = re.sub(r"[\s\-\/]+", "_", u)
    u = re.sub(r"_+", "_", u).strip("_")
    u = u.replace("degrees", "deg")
    u = u.replace("degree", "deg")
    return u


def normalize_unit_family(unit: str) -> str:
    normalized = normalize_unit_token(unit)
    if normalized in UNIT_FAMILIES:
        return normalized
    if normalized in UNIT_ALIASES:
        return UNIT_ALIASES[normalized]
    raise UncertaintyCalculationError(
        f"Unidad «{unit}» no admitida. Unidades válidas: dB, degree/deg/º, linear/ratio, Hz, dB_per_K, deg_per_K y "
        "degree_per_GHz_per_K."
    )


def normalize_source_family_token(value: Any) -> str:
    """Normalize the source applicability family, including magnitude_all."""
    normalized = normalize_unit_token(str(value))
    if normalized == MAGNITUDE_ALL_SOURCE_FAMILY:
        return MAGNITUDE_ALL_SOURCE_FAMILY
    return normalize_unit_family(normalized)


def get_source_family(source: dict[str, Any]) -> str:
    """Applicability family of the source."""
    family = source.get("source_family", source.get("unit_family"))
    if family is not None:
        return normalize_source_family_token(family)
    return normalize_unit_family(source.get("unit", ""))


def get_source_value_family(source: dict[str, Any]) -> str:
    """Real unit family of the numeric uncertainty value stored in the file."""
    unit = source.get("unit")
    if unit is not None:
        return normalize_unit_family(unit)
    family = source.get("unit_family")
    if family is not None and normalize_unit_token(str(family)) != MAGNITUDE_ALL_SOURCE_FAMILY:
        return normalize_unit_family(family)
    raise UncertaintyCalculationError(
        "No se puede determinar la unidad del valor de la fuente: defina «unit» si source_family/unit_family es magnitude_all."
    )


def convert_magnitude_standard_uncertainty(
    standard_uncertainty: float,
    from_family: str,
    to_family: str,
    nominal_value: float,
    nominal_unit_family: str,
) -> float:
    """Convert a standard uncertainty between dB and linear magnitude."""
    if from_family == to_family:
        return standard_uncertainty
    if from_family not in MAGNITUDE_OUTPUT_FAMILIES or to_family not in MAGNITUDE_OUTPUT_FAMILIES:
        raise UncertaintyCalculationError(f"No se puede convertir una incertidumbre de «{from_family}» a «{to_family}».")
    nominal_linear = input_value_to_level_value(value=nominal_value, input_family=nominal_unit_family, limit_family="magnitude_linear")
    if nominal_linear <= 0:
        raise UncertaintyCalculationError(
            "La magnitud lineal nominal debe ser mayor que cero para convertir la incertidumbre entre dB y lineal."
        )
    if from_family == "magnitude_linear" and to_family == "magnitude_db":
        # dB = 20 log10(x), derivative = 20/(ln(10)*x)
        return abs(standard_uncertainty * 20.0 / (math.log(10.0) * nominal_linear))
    if from_family == "magnitude_db" and to_family == "magnitude_linear":
        # x = 10^(dB/20), derivative = ln(10)*x/20
        return abs(standard_uncertainty * math.log(10.0) * nominal_linear / 20.0)
    raise UncertaintyCalculationError(f"Conversión de incertidumbre de magnitud no admitida: «{from_family}» → «{to_family}».")


def db_to_linear(db_value: float) -> float:
    return 10 ** (float(db_value) / 20.0)


def linear_to_db(linear_value: float) -> float:
    if linear_value <= 0:
        raise UncertaintyCalculationError("La magnitud lineal debe ser mayor que cero para pasarla a dB.")
    return 20.0 * math.log10(abs(float(linear_value)))


def input_value_to_level_value(value: float, input_family: str, limit_family: str) -> float:
    """Convert the input value to the family of the level limits of the file (dB, linear…)."""
    if limit_family == "magnitude_db":
        if input_family == "magnitude_db":
            return float(value)
        if input_family == "magnitude_linear":
            return linear_to_db(float(value))
    if limit_family == "magnitude_linear":
        if input_family == "magnitude_linear":
            return abs(float(value))
        if input_family == "magnitude_db":
            return db_to_linear(float(value))
    if limit_family == "phase_deg" and input_family == "phase_deg":
        return float(value)
    if limit_family == "frequency" and input_family == "frequency":
        return float(value)
    raise UncertaintyCalculationError(f"No se puede pasar un valor de «{input_family}» a límites de nivel en «{limit_family}».")


def load_yaml(yaml_path: str | Path) -> dict[str, Any]:
    yaml_path = Path(yaml_path)
    if not yaml_path.exists():
        raise FileNotFoundError(f"No se encuentra el fichero de incertidumbres: {yaml_path}")
    with yaml_path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def get_frequency_bounds(block: dict[str, Any]) -> tuple[float, float] | None:
    if "frequency_hz" in block:
        f = float(block["frequency_hz"])
        return f, f
    if "f_start_hz" in block and "f_stop_hz" in block:
        return float(block["f_start_hz"]), float(block["f_stop_hz"])
    return None


def contains_frequency(freq_hz: float, block: dict[str, Any]) -> bool:
    bounds = get_frequency_bounds(block)
    if bounds is None:
        return False
    f_start, f_stop = bounds
    if f_start == f_stop:
        return math.isclose(freq_hz, f_start, rel_tol=0.0, abs_tol=1e-9)
    return f_start <= freq_hz <= f_stop


def select_frequency_blocks(freq_hz: float, blocks: list[dict[str, Any]], selection_rule: str) -> list[dict[str, Any]]:
    if not blocks:
        return []
    selection_rule = selection_rule or "range_match"
    if selection_rule == "exact_match_required":
        return [
            b for b in blocks if "frequency_hz" in b and math.isclose(freq_hz, float(b["frequency_hz"]), rel_tol=0.0, abs_tol=1e-9)
        ][:1]
    if selection_rule == "range_match":
        return [b for b in blocks if contains_frequency(freq_hz, b)][:1]
    if selection_rule == "nearest_lower":
        candidates = []
        for b in blocks:
            bounds = get_frequency_bounds(b)
            if bounds is not None and bounds[0] <= freq_hz:
                candidates.append((bounds[0], b))
        return [max(candidates, key=lambda x: x[0])[1]] if candidates else []
    if selection_rule == "interpolation_linear":
        direct = [b for b in blocks if contains_frequency(freq_hz, b)]
        if direct:
            return direct[:1]
        centers = []
        for b in blocks:
            bounds = get_frequency_bounds(b)
            if bounds is None:
                continue
            center = bounds[0] if bounds[0] == bounds[1] else (bounds[0] + bounds[1]) / 2.0
            centers.append((center, b))
        lower = [x for x in centers if x[0] <= freq_hz]
        upper = [x for x in centers if x[0] >= freq_hz]
        if not lower or not upper:
            return []
        return [max(lower, key=lambda x: x[0])[1], min(upper, key=lambda x: x[0])[1]]
    raise UncertaintyCalculationError(f"selection_rule no admitida: {selection_rule}")


def level_limit_fields(limit_family: str) -> tuple[str, str]:
    """Min/max keys of the level_ranges of a family."""
    if limit_family not in LEVEL_LIMIT_FIELDS_BY_UNIT_FAMILY:
        raise UncertaintyCalculationError(f"No hay límites de nivel definidos para unit_family='{limit_family}'.")
    return LEVEL_LIMIT_FIELDS_BY_UNIT_FAMILY[limit_family]


def level_matches(level_value: float, level_range: dict[str, Any], limit_family: str) -> bool:
    min_key, max_key = level_limit_fields(limit_family)
    if min_key not in level_range or max_key not in level_range:
        return False
    return float(level_range[min_key]) <= level_value <= float(level_range[max_key])


def select_level_range(level_value: float, level_ranges: list[dict[str, Any]], limit_family: str) -> dict[str, Any] | None:
    for lr in level_ranges:
        if level_matches(level_value, lr, limit_family):
            return lr
    return None


def describe_allowed_level_ranges(level_ranges: list[dict[str, Any]], limit_family: str) -> list[dict[str, Any]]:
    min_key, max_key = level_limit_fields(limit_family)
    return [{min_key: r.get(min_key), max_key: r.get(max_key)} for r in level_ranges if min_key in r or max_key in r]


def detect_level_limit_family(level_ranges: list[dict[str, Any]], preferred_family: str) -> str:
    """Unit family of the level limits present in the file.

    It does not have to be the family of the uncertainty: a phase uncertainty in
    degrees can have its level ranges given by the magnitude of the S-parameter,
    in dB or linear. The family is the one whose min/max keys are in the file.
    """
    families_to_try: list[str] = []
    if preferred_family in LEVEL_LIMIT_FIELDS_BY_UNIT_FAMILY:
        families_to_try.append(preferred_family)
    # For phase uncertainty the level variable is normally the magnitude
    if preferred_family in {"phase_deg", "phase_deg_per_k"}:
        families_to_try.extend(["magnitude_db", "magnitude_linear", "phase_deg"])
    families_to_try.extend(["magnitude_db", "magnitude_linear", "phase_deg", "frequency"])
    families_to_try = list(dict.fromkeys(families_to_try))
    for family in families_to_try:
        if family not in LEVEL_LIMIT_FIELDS_BY_UNIT_FAMILY:
            continue
        min_key, max_key = level_limit_fields(family)
        if any(min_key in r and max_key in r for r in level_ranges):
            return family
    return preferred_family


def find_value_field(data_block: dict[str, Any], unit_family: str) -> tuple[str, float]:
    for field_name in VALUE_FIELDS_BY_UNIT_FAMILY.get(unit_family, []):
        if field_name in data_block:
            return field_name, float(data_block[field_name])
    raise UncertaintyCalculationError(
        f"No hay ningún campo de valor para unit_family='{unit_family}'. Campos disponibles: {list(data_block.keys())}"
    )


def parse_iso_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None


def source_condition(source: dict[str, Any], key: str, default: Any = None) -> Any:
    conditions = source.get("conditions")
    if isinstance(conditions, dict) and key in conditions:
        return conditions[key]
    return source.get(key, default)


class UncertaintyEngine:
    """Calculation engine of one uncertainty data file (one VNA unit)."""

    def __init__(self, yaml_path: str | Path | None = None, data: dict[str, Any] | None = None):
        if data is None:
            if yaml_path is None:
                raise UncertaintyCalculationError("Indique el fichero de incertidumbres")
            data = load_yaml(yaml_path)
        if not isinstance(data, dict) or not isinstance(data.get("uncertainty_sources"), list):
            raise UncertaintyCalculationError("El fichero de incertidumbres no tiene la lista «uncertainty_sources»")
        self.yaml_path = Path(yaml_path) if yaml_path is not None else None
        self.data = data

    # ── file data ──────────────────────────────────────────────────────────
    @property
    def equipment_model(self) -> str:
        return str(self.data.get("equipment", {}).get("model", ""))

    @property
    def equipment_manufacturer(self) -> str:
        return str(self.data.get("equipment", {}).get("manufacturer", ""))

    @property
    def serial_number(self) -> str:
        return str(self.data.get("equipment", {}).get("serial_number", "") or "")

    @property
    def document(self) -> str:
        control = self.data.get("document_control", {}) or {}
        document = str(control.get("document_id", "") or (self.yaml_path.name if self.yaml_path else ""))
        revision = control.get("revision")
        return f"{document} rev. {revision}" if revision else document

    @property
    def frequency_range_hz(self) -> tuple[float, float] | None:
        fr = self.data.get("equipment", {}).get("frequency_range_hz") or {}
        if "start" in fr and "stop" in fr:
            return float(fr["start"]), float(fr["stop"])
        return None

    def validate_equipment(self, equipment: str) -> None:
        model = self.equipment_model.upper()
        manufacturer = self.equipment_manufacturer.upper()
        text = str(equipment).upper()
        if model and model in text:
            return
        if manufacturer and manufacturer in text:
            return
        if text and text in model:
            return
        raise UncertaintyCalculationError(
            f"El equipo «{equipment}» no coincide con el del fichero de incertidumbres «{self.equipment_model}»."
        )

    def equipment_frequency_limit_violation(self, frequency_hz: float) -> dict[str, Any] | None:
        fr = self.data.get("equipment", {}).get("frequency_range_hz", {})
        f_start = float(fr.get("start", -math.inf))
        f_stop = float(fr.get("stop", math.inf))
        if f_start <= frequency_hz <= f_stop:
            return None
        return {
            "type": "equipment_frequency_out_of_limits",
            "frequency_hz": frequency_hz,
            "allowed_min_hz": f_start,
            "allowed_max_hz": f_stop,
            "message": "La frecuencia está fuera del rango de frecuencia del equipo.",
        }

    def default_coverage_factor(self) -> float:
        # Not read from measurement_configuration_basis: that block is only used for the temperature.
        return 2.0

    def reference_temperature_range(self) -> tuple[float, float]:
        t = self.data.get("measurement_configuration_basis", {}).get("datasheet_accuracy_conditions", {}).get("temperature_range_c", {})
        return float(t.get("minimum", 18.0)), float(t.get("maximum", 28.0))

    def temperature_delta_k(self, temp_min_c: float | None, temp_max_c: float | None) -> float:
        if temp_min_c is None or temp_max_c is None:
            return 0.0
        ref_min, ref_max = self.reference_temperature_range()
        delta_low = max(0.0, ref_min - float(temp_min_c))
        delta_high = max(0.0, float(temp_max_c) - ref_max)
        return max(delta_low, delta_high)

    def calibration_status(self, today: date | None = None) -> tuple[str | None, bool | None, list[str]]:
        status = self.data.get("calibration_status")
        if not isinstance(status, dict):
            status = self.data.get("equipment", {}).get("calibration_status", {})
        due_text = status.get("calibration_due_date") if isinstance(status, dict) else None
        due_date = parse_iso_date(due_text)
        if due_date is None:
            return (
                str(due_text) if due_text is not None else None,
                None,
                ["El fichero no tiene calibration_due_date (o su formato no es válido): no se puede comprobar la calibración."],
            )
        today = today or date.today()
        if today > due_date:
            return due_date.isoformat(), False, [
                f"AVISO: el equipo está fuera de calibración (calibration_due_date={due_date.isoformat()}, anterior a la fecha "
                f"del cálculo {today.isoformat()}). No se puede asegurar la validez de los resultados."
            ]
        return due_date.isoformat(), True, []

    # ── sources ────────────────────────────────────────────────────────────
    @staticmethod
    def source_applies_to_parameter(source: dict[str, Any], parameter: str) -> bool:
        applicable = [str(x).upper() for x in source.get("applicable_to", [])]
        return parameter.upper() in applicable

    @staticmethod
    def is_temperature_source(source: dict[str, Any]) -> bool:
        sid = str(source.get("source_id", "")).lower()
        quantity = str(source.get("quantity", "")).lower()
        family = str(source.get("unit_family", "")).lower()
        return "temp" in sid or "temperature_stability" in quantity or family.endswith("_per_k")

    @staticmethod
    def is_trace_noise_source(source: dict[str, Any]) -> bool:
        text = f"{source.get('source_id', '')} {source.get('quantity', '')}".lower()
        return "trace_noise" in text.replace(" ", "_").replace("-", "_")

    @staticmethod
    def if_bandwidth_allowed(source: dict[str, Any], actual_if_bandwidth_hz: float | None) -> tuple[bool, dict[str, Any] | None]:
        """IF bandwidth condition of the source (``conditions.if_bandwidth_hz``).

        A smaller IF bandwidth than the one of the condition excludes the source; it does not
        make the result NaN, since it is a condition of the source and not a measured value
        outside the tables.
        """
        required_ifbw = source_condition(source, "if_bandwidth_hz")
        if required_ifbw is None or actual_if_bandwidth_hz is None:
            return True, None
        if float(actual_if_bandwidth_hz) >= float(required_ifbw):
            return True, None
        return False, {
            "type": "if_bandwidth_out_of_limits",
            "source_id": source.get("source_id", ""),
            "actual_if_bandwidth_hz": float(actual_if_bandwidth_hz),
            "required_min_if_bandwidth_hz": float(required_ifbw),
            "message": "La IFBW usada es menor que la de la condición de la fuente.",
        }

    def source_matches_unit_family(
        self, source: dict[str, Any], parameter: str, requested_family: str, include_temperature_stability: bool
    ) -> bool:
        source_family = get_source_family(source)
        source_value_family = get_source_value_family(source)
        if self.is_temperature_source(source):
            if not include_temperature_stability:
                return False
            # Temperature stability in dB/K applies to any magnitude output (converted later if linear)
            if source_value_family == "magnitude_db_per_k" and requested_family in MAGNITUDE_OUTPUT_FAMILIES:
                return True
            return requested_family == "phase_deg" and source_value_family == "phase_deg_per_k"
        if self.is_trace_noise_source(source):
            # Trace noise has its own criteria (IF bandwidth); its value is converted later if needed
            if requested_family in MAGNITUDE_OUTPUT_FAMILIES and source_value_family in MAGNITUDE_OUTPUT_FAMILIES:
                return True
            return source_family == requested_family or source_value_family == requested_family
        # Applies to all magnitude outputs; the requested family only decides the conversion
        if source_family == MAGNITUDE_ALL_SOURCE_FAMILY:
            return requested_family in MAGNITUDE_OUTPUT_FAMILIES and source_value_family in MAGNITUDE_OUTPUT_FAMILIES
        return source_family == requested_family or source_value_family == requested_family

    def standard_uncertainty(self, raw_value: float, source: dict[str, Any], frequency_hz: float, temp_delta_k: float) -> float:
        utype = str(source.get("uncertainty_type", "")).lower()
        distribution = str(source.get("distribution", "")).lower()
        k = float(source.get("coverage_factor", 1.0))
        sensitivity = float(source.get("sensitivity_coefficient", 1.0))
        if utype == "expanded":
            u = raw_value / k
        elif utype == "rms_standard_deviation":
            u = raw_value
        elif utype == "coefficient":
            if temp_delta_k <= 0:
                return 0.0
            family = get_source_value_family(source)
            source_unit_token = normalize_unit_token(str(source.get("unit", "")))
            if family == "phase_deg_per_k":
                # deg_per_K: coeff · ΔT; deg_per_GHz_per_K: coeff · f(GHz) · ΔT
                if source_unit_token in {"deg_per_ghz_per_k", "degree_per_ghz_per_k", "deg_ghz_k", "degree_ghz_k"}:
                    semi_range = raw_value * (frequency_hz / 1e9) * temp_delta_k
                else:
                    semi_range = raw_value * temp_delta_k
            elif family == "magnitude_db_per_k":
                semi_range = raw_value * temp_delta_k
            else:
                raise UncertaintyCalculationError(f"Unidad de coeficiente no admitida: {family}")
            u = semi_range / math.sqrt(3.0) if distribution == "rectangular" else semi_range / k
        else:
            raise UncertaintyCalculationError(f"uncertainty_type no admitido: {utype}")
        return abs(u * sensitivity)

    def extract_source_contribution(
        self,
        source: dict[str, Any],
        parameter: str,
        frequency_hz: float,
        input_value: float,
        input_family: str,
        output_family: str,
        level_value_input: float,
        level_input_family: str,
        temp_delta_k: float,
        if_bandwidth_hz: float | None,
    ) -> tuple[Contribution | None, list[dict[str, Any]]]:
        out_of_limits: list[dict[str, Any]] = []
        bw_ok, _bw_problem = self.if_bandwidth_allowed(source, if_bandwidth_hz)
        if not bw_ok:
            # Condition of the source, not a measured value: the source is left out without invalidating the total
            return None, out_of_limits

        selection_rule = source.get("selection_rule", "range_match")
        frequency_blocks = source.get("frequency_ranges", [])
        selected_blocks = select_frequency_blocks(frequency_hz, frequency_blocks, selection_rule)
        if not selected_blocks:
            out_of_limits.append({
                "type": "source_frequency_out_of_limits",
                "source_id": source.get("source_id", ""),
                "frequency_hz": frequency_hz,
                "selection_rule": selection_rule,
                "allowed_frequency_ranges": [get_frequency_bounds(b) for b in frequency_blocks],
                "message": "La frecuencia está fuera de todos los rangos de frecuencia de esta fuente de incertidumbre.",
            })
            return None, out_of_limits

        source_family = get_source_family(source)
        source_value_family = get_source_value_family(source)

        # lookup_family: unit family used to read the value from the file;
        # output_family: family of the result (they differ e.g. for magnitude_all in dB with output linear)
        lookup_family = output_family
        convert_uncertainty_to_output_family = False
        conversion_from_family = output_family

        if source_value_family == "magnitude_db_per_k":
            lookup_family = source_value_family
            conversion_from_family = "magnitude_db"  # standard_uncertainty() returns the temperature contribution in dB
            convert_uncertainty_to_output_family = output_family == "magnitude_linear"
        elif source_value_family == "phase_deg_per_k":
            lookup_family = source_value_family
            conversion_from_family = "phase_deg"
        elif source_family == MAGNITUDE_ALL_SOURCE_FAMILY or self.is_trace_noise_source(source):
            lookup_family = source_value_family
            conversion_from_family = lookup_family
            convert_uncertainty_to_output_family = lookup_family != output_family
        elif parameter in {"S11", "S22"} and output_family == "magnitude_db" and source_value_family == "magnitude_linear":
            lookup_family = "magnitude_linear"
            conversion_from_family = "magnitude_linear"
            convert_uncertainty_to_output_family = True

        first_block = selected_blocks[0]
        level_info = None
        level_ranges = first_block.get("level_ranges")
        if level_ranges is None:
            level_ranges = first_block.get("reflection_coefficient_ranges")

        if level_ranges:
            level_limit_family = lookup_family
            if level_limit_family in {"magnitude_db_per_k", "phase_deg_per_k"}:
                level_limit_family = output_family
            # The limits may be in another family than the uncertainty (phase in degrees with limits in dB)
            level_limit_family = detect_level_limit_family(level_ranges, level_limit_family)
            level_value = input_value_to_level_value(value=level_value_input, input_family=level_input_family, limit_family=level_limit_family)
            level_info = select_level_range(level_value=level_value, level_ranges=level_ranges, limit_family=level_limit_family)
            if level_info is None:
                out_of_limits.append({
                    "type": "input_level_out_of_limits",
                    "source_id": source.get("source_id", ""),
                    "level_input_value": level_value_input,
                    "level_input_unit_family": level_input_family,
                    "level_limit_family": level_limit_family,
                    "converted_level_value": level_value,
                    "allowed_level_ranges": describe_allowed_level_ranges(level_ranges, level_limit_family),
                    "message": "El nivel está fuera de todos los rangos de nivel de la fuente; no se aplica el peor caso.",
                })
                return None, out_of_limits
            field_name, raw_value = find_value_field(level_info, lookup_family)
        else:
            field_name, raw_value = find_value_field(first_block, lookup_family)
        freq_info = first_block

        u = self.standard_uncertainty(raw_value, source, frequency_hz, temp_delta_k)

        if convert_uncertainty_to_output_family:
            try:
                u = convert_magnitude_standard_uncertainty(
                    standard_uncertainty=u,
                    from_family=conversion_from_family,
                    to_family=output_family,
                    nominal_value=input_value,
                    nominal_unit_family=input_family,
                )
            except UncertaintyCalculationError as exc:
                out_of_limits.append({
                    "type": "input_magnitude_invalid_for_uncertainty_unit_conversion",
                    "source_id": source.get("source_id", ""),
                    "input_value": input_value,
                    "input_unit_family": input_family,
                    "from_family": lookup_family,
                    "to_family": output_family,
                    "message": str(exc),
                })
                return None, out_of_limits

        base_comment = ""
        conditions = source.get("conditions")
        if isinstance(conditions, dict):
            base_comment = str(conditions.get("comments", ""))
        if source_family == MAGNITUDE_ALL_SOURCE_FAMILY:
            base_comment = f"{base_comment} source_family=magnitude_all; valor leído en {lookup_family}; contribución en {output_family}.".strip()
        elif self.is_temperature_source(source) and source_value_family == "magnitude_db_per_k":
            base_comment = f"{base_comment} estabilidad con la temperatura; coeficiente leído en {lookup_family}; contribución en {output_family}.".strip()
        elif self.is_trace_noise_source(source):
            base_comment = f"{base_comment} ruido de traza; valor leído en {lookup_family}; contribución en {output_family}.".strip()

        return Contribution(
            source_id=str(source.get("source_id", "")),
            quantity=str(source.get("quantity", "")),
            source_type=str(source.get("source_type", "")),
            uncertainty_type=str(source.get("uncertainty_type", "")),
            distribution=str(source.get("distribution", "")),
            unit_family=output_family,
            raw_field=field_name,
            raw_value=raw_value,
            raw_unit=str(source.get("unit", "")),
            standard_uncertainty=u,
            sensitivity_coefficient=float(source.get("sensitivity_coefficient", 1.0)),
            coverage_factor_source=float(source.get("coverage_factor", 1.0)),
            frequency_range=freq_info,
            level_range=level_info,
            selection_rule=str(selection_rule),
            comment=base_comment,
        ), out_of_limits

    def calculate(
        self,
        equipment: str,
        parameter: str,
        frequency_hz: float,
        value: float,
        unit: str,
        temp_min_c: float | None = None,
        temp_max_c: float | None = None,
        coverage_factor: float | None = None,
        include_trace_noise: bool = True,
        include_temperature_stability: bool = True,
        if_bandwidth_hz: float | None = None,
        output_unit: str | None = None,
        level_value: float | None = None,
        level_unit: str | None = None,
        today: date | None = None,
    ) -> UncertaintyResult:
        self.validate_equipment(equipment)
        parameter = normalize_parameter(parameter)
        frequency_hz = float(frequency_hz)
        value = float(value)
        input_family = normalize_unit_family(unit)
        output_family = normalize_unit_family(output_unit) if output_unit is not None else input_family

        # Level of the level_ranges: for phase it is the magnitude of the S-parameter (dB or linear)
        level_value_used = value if level_value is None else float(level_value)
        level_unit_used = unit if level_unit is None else level_unit
        level_family = normalize_unit_family(level_unit_used)

        coverage_factor = float(self.default_coverage_factor() if coverage_factor is None else coverage_factor)
        temp_delta = self.temperature_delta_k(temp_min_c, temp_max_c)
        due_date, calibration_valid, warnings = self.calibration_status(today)
        contributions: list[Contribution] = []
        out_of_limits: list[dict[str, Any]] = []

        equipment_freq_problem = self.equipment_frequency_limit_violation(frequency_hz)
        if equipment_freq_problem is not None:
            out_of_limits.append(equipment_freq_problem)

        for source in self.data.get("uncertainty_sources", []):
            if not source.get("include_in_total", False):
                continue
            if not self.source_applies_to_parameter(source, parameter):
                continue
            if self.is_trace_noise_source(source) and not include_trace_noise:
                continue
            if not self.source_matches_unit_family(source, parameter, output_family, include_temperature_stability):
                continue
            c, source_limits = self.extract_source_contribution(
                source=source,
                parameter=parameter,
                frequency_hz=frequency_hz,
                input_value=value,
                input_family=input_family,
                output_family=output_family,
                level_value_input=level_value_used,
                level_input_family=level_family,
                temp_delta_k=temp_delta,
                if_bandwidth_hz=if_bandwidth_hz,
            )
            out_of_limits.extend(source_limits)
            if c is not None:
                if self.is_temperature_source(source) and c.standard_uncertainty == 0.0:
                    continue
                contributions.append(c)

        if out_of_limits:
            uc = float("nan")
            expanded = float("nan")
            warnings.append(
                "Al menos un valor de entrada o de configuración está fuera de los límites del fichero: la incertidumbre "
                "combinada y la expandida son NaN."
            )
        else:
            if not contributions:
                raise UncertaintyCalculationError("No hay ninguna fuente de incertidumbre aplicable a los datos indicados.")
            uc = math.sqrt(sum(c.standard_uncertainty**2 for c in contributions))
            expanded = uc * coverage_factor

        return UncertaintyResult(
            equipment=equipment,
            parameter=parameter,
            frequency_hz=frequency_hz,
            input_value=value,
            input_unit=unit,
            input_unit_family=input_family,
            output_unit_family=output_family,
            level_value=level_value_used,
            level_unit=level_unit_used,
            level_unit_family=level_family,
            temperature_min_c=temp_min_c,
            temperature_max_c=temp_max_c,
            temperature_delta_k_used=temp_delta,
            coverage_factor=coverage_factor,
            combined_standard_uncertainty=uc,
            expanded_uncertainty=expanded,
            contributions=contributions,
            calculation_date=(today or date.today()).isoformat(),
            calibration_due_date=due_date,
            calibration_valid=calibration_valid,
            warnings=warnings,
            out_of_limits=out_of_limits,
        )


def calculate_expanded_uncertainty(
    yaml_path: str | Path,
    equipment: str,
    parameter: str,
    frequency_hz: float,
    value: float,
    unit: str,
    temp_min_c: float | None = None,
    temp_max_c: float | None = None,
    coverage_factor: float | None = None,
    include_trace_noise: bool = True,
    include_temperature_stability: bool = True,
    if_bandwidth_hz: float | None = None,
    output_unit: str | None = None,
    level_value: float | None = None,
    level_unit: str | None = None,
) -> dict[str, Any]:
    engine = UncertaintyEngine(yaml_path=yaml_path)
    result = engine.calculate(
        equipment=equipment,
        parameter=parameter,
        frequency_hz=frequency_hz,
        value=value,
        unit=unit,
        temp_min_c=temp_min_c,
        temp_max_c=temp_max_c,
        coverage_factor=coverage_factor,
        include_trace_noise=include_trace_noise,
        include_temperature_stability=include_temperature_stability,
        if_bandwidth_hz=if_bandwidth_hz,
        output_unit=output_unit,
        level_value=level_value,
        level_unit=level_unit,
    )
    return result.to_dict()
