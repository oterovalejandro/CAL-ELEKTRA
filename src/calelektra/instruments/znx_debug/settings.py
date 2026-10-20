"""Settings of a ZNx channel and of its traces that the debugger writes and reads back.

Each setting is one SCPI header of the R&S RsZnx 4.25.0 driver (attribute
table ``CoreEx.Attributes.cs`` and range tables ``CoreEx.RangeTablesInit.cs``):
the debugger sends ``<header> <value>`` and then ``<header>?``. Per-trace
settings act on the *active* trace of the channel, so the trace is selected
first (``CALC<ch>:PAR:SEL``). Labels and descriptions are shown in the console.
"""

from __future__ import annotations

import math
import unicodedata
from dataclasses import dataclass, field
from typing import Any

from calelektra.core.units import UnitError, format_frequency, parse_frequency

SWEEP_TYPES = {
    "LIN": "Lineal",
    "LOG": "Logarítmico",
    "SEGM": "Segmentado (lista de frecuencias)",
    "POW": "Barrido de potencia",
    "CW": "Tiempo, a frecuencia fija",
    "POIN": "Puntos a frecuencia fija (CW)",
}
TRACE_FORMATS = {
    "MLOG": "Magnitud en dB",
    "MLIN": "Magnitud lineal",
    "PHAS": "Fase (°, entre ±180)",
    "UPH": "Fase desenrollada (°)",
    "SWR": "ROE (SWR)",
    "REAL": "Parte real",
    "IMAG": "Parte imaginaria",
    "GDEL": "Retardo de grupo (s)",
    "SMIT": "Carta de Smith",
    "POL": "Polar",
    "ISM": "Carta de Smith inversa (admitancia)",
}
AVERAGE_MODES = {
    "AUTO": "Automático",
    "FLAT": "Aplanado (flatten)",
    "RED": "Reducción de ruido (reduce)",
    "MOV": "Media móvil (moving)",
}
SELECTIVITIES = {"NORM": "Normal", "MED": "Media", "HIGH": "Alta"}
HOLD_MODES = {"OFF": "Desactivada", "MAX": "Retención de máximos (max hold)", "MIN": "Retención de mínimos (min hold)"}
TOUCHSTONE_VERSIONS = {"F11": "Touchstone 1.1", "F20": "Touchstone 2.0", "L11": "Touchstone 1.1 (formato antiguo)"}
# Data format of MMEM:STOR:TRAC:PORT (RsZnx_rngOutputFormat) and the Touchstone option line it produces
EXPORT_FORMATS = {"COMP": "RI (real, imaginaria)", "LINP": "MA (magnitud lineal, fase)", "LOGP": "DB (magnitud en dB, fase)"}
EXPORT_TO_TOUCHSTONE = {"COMP": "RI", "LINP": "MA", "LOGP": "DB"}
IMPEDANCE_MODES = {"CIMP": "Impedancia común", "PIMP": "Impedancia de cada puerto"}
DATA_FORMATS = {"ASC": "ASCII", "REAL,32": "Binario de 32 bits", "REAL,64": "Binario de 64 bits"}
DATA_KINDS = {
    "SDAT": "Sin formato: complejos corregidos, antes de formato, suavizado y retención",
    "FDAT": "Con formato: los valores de la traza en pantalla",
    "MDAT": "Sin formato, con la matemática de trazas",
}

# Accepted spellings besides the SCPI short and long forms
ALIASES = {
    "LINEAL": "LIN",
    "LINEAR": "LIN",
    "LOGARITMICO": "LOG",
    "LOGARITHMIC": "LOG",
    "SEGMENTADO": "SEGM",
    "LISTA": "SEGM",
    "FASE": "PHAS",
    "ASCII": "ASC",
    "REAL32": "REAL,32",
    "REAL64": "REAL,64",
    "BINARIO": "REAL,32",
    "BIN32": "REAL,32",
    "BIN64": "REAL,64",
    "MAXHOLD": "MAX",
    "MINHOLD": "MIN",
    "RI": "COMP",
    "MA": "LINP",
    "DB": "LOGP",
}
TRUE_WORDS = {"ON", "1", "TRUE", "SI", "S", "YES", "Y"}
FALSE_WORDS = {"OFF", "0", "FALSE", "NO", "N"}


def _plain(text: str) -> str:
    """Upper case without accents or spaces (for matching user input and SCPI answers)."""
    text = unicodedata.normalize("NFKD", str(text)).encode("ascii", "ignore").decode()
    return text.strip().strip("'\"").upper().replace(" ", "")


def parse_choice(value: Any, choices: dict[str, str]) -> str:
    """SCPI value of ``value`` among ``choices``: exact, alias or long form (``LINear`` → ``LIN``)."""
    text = _plain(value)
    if text in choices:
        return text
    if ALIASES.get(text) in choices:
        return ALIASES[text]
    if text == "DB" and "MLOG" in choices:
        return "MLOG"
    candidates = [key for key in choices if text.startswith(key.replace(",", ""))]
    if candidates:
        return max(candidates, key=len)
    raise ValueError(f"«{value}» no es válido; opciones: {', '.join(choices)}")


def parse_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = _plain(value)
    if text in TRUE_WORDS:
        return True
    if text in FALSE_WORDS:
        return False
    raise ValueError(f"«{value}» no es un valor de activación; use on u off")


def parse_number(value: Any) -> float:
    if isinstance(value, bool):
        raise ValueError(f"«{value}» no es un número")
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", ".")
    for unit in ("dBm", "dB", "%", "s"):
        if text.lower().endswith(unit.lower()):
            text = text[: -len(unit)].strip()
            break
    try:
        return float(text)
    except ValueError:
        raise ValueError(f"«{value}» no es un número") from None


def parse_scpi_number(text: str) -> float:
    """Numeric answer of the analyzer (``1.5E+009``, ``+1``…)."""
    try:
        return float(str(text).strip().strip("'\"").split(",")[0])
    except ValueError:
        raise ValueError(f"respuesta no numérica: «{text}»") from None


def scpi_number(value: float) -> str:
    return f"{value:.12g}"


@dataclass(frozen=True)
class Setting:
    key: str
    header: str  # «{ch}» is replaced by the channel number
    label: str  # shown in the console
    kind: str  # frequency | integer | number | boolean | choice
    unit: str = ""
    choices: dict[str, str] = field(default_factory=dict)
    per_trace: bool = False
    writable: bool = True
    tolerance: float = 0.0  # absolute tolerance of the read-back value
    example: str = ""

    def command(self, channel: int) -> str:
        return self.header.format(ch=channel)

    def value(self, value: Any) -> Any:
        """Python value of a user input (``"10 MHz"`` → 1e7, ``"on"`` → True…)."""
        if self.kind == "frequency":
            try:
                hz = parse_frequency(value)
            except UnitError as exc:
                raise ValueError(str(exc)) from None
            return hz
        if self.kind == "integer":
            number = parse_number(value)
            if number != int(number):
                raise ValueError(f"«{value}» no es un número entero")
            return int(number)
        if self.kind == "number":
            return parse_number(value)
        if self.kind == "boolean":
            return parse_bool(value)
        return parse_choice(value, self.choices)

    def encode(self, value: Any) -> str:
        """SCPI text of a user input."""
        value = self.value(value)
        if self.kind == "boolean":
            return "ON" if value else "OFF"
        if self.kind in ("frequency", "number"):
            return scpi_number(value)
        return str(value)

    def decode(self, answer: str) -> Any:
        """Python value of the analyzer answer."""
        if self.kind in ("frequency", "number"):
            return parse_scpi_number(answer)
        if self.kind == "integer":
            return int(round(parse_scpi_number(answer)))
        if self.kind == "boolean":
            return parse_bool(answer)
        try:
            return parse_choice(answer, self.choices)
        except ValueError:
            return _plain(answer)

    def matches(self, written: Any, read: Any) -> bool:
        written = self.value(written)
        if self.kind in ("frequency", "number"):
            return math.isclose(written, read, rel_tol=1e-9, abs_tol=self.tolerance)
        return written == read

    def show(self, value: Any, describe: bool = True) -> str:
        if value is None:
            return "—"
        if self.kind == "frequency":
            return format_frequency(value, significant=9)
        if self.kind == "boolean":
            return "ON" if value else "OFF"
        if self.kind == "choice":
            description = self.choices.get(value) if describe else None
            return f"{value} ({description})" if description else str(value)
        if self.kind == "number":
            text = f"{value:.6g}".replace(".", ",")
            return f"{text} {self.unit}".strip()
        return f"{value} {self.unit}".strip()


def _setting(key: str, header: str, label: str, kind: str, **options: Any) -> Setting:
    return Setting(key, header, label, kind, **options)


SETTINGS: dict[str, Setting] = {
    s.key: s
    for s in (
        # Stimulus (channel)
        _setting("sweep_type", "SENS{ch}:SWE:TYPE", "Tipo de barrido (escala de frecuencia)", "choice", choices=SWEEP_TYPES, example="LIN"),
        _setting("start", "SENS{ch}:FREQ:STAR", "Frecuencia de inicio", "frequency", tolerance=0.5, example="10MHz"),
        _setting("stop", "SENS{ch}:FREQ:STOP", "Frecuencia final", "frequency", tolerance=0.5, example="18GHz"),
        _setting("center", "SENS{ch}:FREQ:CENT", "Frecuencia central", "frequency", tolerance=0.5, example="9GHz"),
        _setting("span", "SENS{ch}:FREQ:SPAN", "Span", "frequency", tolerance=0.5, example="1GHz"),
        _setting("points", "SENS{ch}:SWE:POIN", "Número de puntos", "integer", example="801"),
        _setting("step", "SENS{ch}:SWE:STEP", "Paso de frecuencia (barrido lineal)", "frequency", tolerance=0.5, example="10MHz"),
        _setting("power", "SOUR{ch}:POW", "Potencia de la fuente", "number", unit="dBm", tolerance=0.005, example="-10"),
        _setting("rf_output", "OUTP{ch}", "Salida RF del canal", "boolean", example="on"),
        _setting("if_bandwidth", "SENS{ch}:BAND", "Ancho de banda de FI (IFBW)", "frequency", tolerance=0.5, example="1kHz"),
        _setting("if_selectivity", "SENS{ch}:BAND:SEL", "Selectividad del filtro de FI", "choice", choices=SELECTIVITIES, example="NORM"),
        _setting("averaging", "SENS{ch}:AVER", "Promediado", "boolean", example="on"),
        _setting("average_count", "SENS{ch}:AVER:COUN", "Número de promedios", "integer", example="10"),
        _setting("average_mode", "SENS{ch}:AVER:MODE", "Modo de promediado", "choice", choices=AVERAGE_MODES, example="AUTO"),
        _setting("sweep_count", "SENS{ch}:SWE:COUN", "Barridos por cada INIT (modo único)", "integer", example="10"),
        _setting("continuous", "INIT{ch}:CONT", "Barrido continuo", "boolean", example="off"),
        _setting("sweep_time", "SENS{ch}:SWE:TIME", "Tiempo de barrido", "number", unit="s", writable=False),
        # Instrument
        _setting("display_update", "SYST:DISP:UPD", "Actualizar la pantalla en control remoto", "boolean", example="on"),
        _setting(
            "touchstone_version", "MMEM:STOR:TRAC:OPT:FORM", "Versión Touchstone de la exportación", "choice",
            choices=TOUCHSTONE_VERSIONS, example="F11",
        ),
        # Active trace
        _setting("trace_format", "CALC{ch}:FORM", "Formato de la traza", "choice", choices=TRACE_FORMATS, per_trace=True, example="MLOG"),
        _setting("smoothing", "CALC{ch}:SMO", "Suavizado", "boolean", per_trace=True, example="on"),
        _setting(
            "smoothing_aperture", "CALC{ch}:SMO:APER", "Apertura del suavizado", "number", unit="%", per_trace=True,
            tolerance=1e-6, example="2",
        ),
        _setting("hold", "CALC{ch}:PHOL", "Retención (max hold / min hold)", "choice", choices=HOLD_MODES, per_trace=True, example="MAX"),
    )
}
CHANNEL_SETTINGS = [s for s in SETTINGS.values() if not s.per_trace]
TRACE_SETTINGS = [s for s in SETTINGS.values() if s.per_trace]
