"""R&S ZNB43 simulated at SCPI level, to try the debugger (and the ZNx adapter) without the analyzer.

It keeps the state of the channels, traces and diagrams, answers the SCPI
commands that the debugger sends (the ones of the RsZnx driver, short or long
form), adjusts values the way the analyzer does (IF bandwidth steps, step size →
number of points) and fills the error queue (``SYST:ERR?``) with SCPI errors
for unknown headers, missing traces or values out of range. A query that fails
gets no answer, as on the instrument (the client sees a timeout).

The simulated DUT is a ~60 cm coaxial cable between ports 1 and 2. The noise
depends on the IF bandwidth and the power, so averaging, smoothing and max hold
have a visible effect. Unformatted data (``SDAT``) include the averaging but not
the smoothing or the hold, which only change the formatted trace (``FDAT``).

This is not a model of the ZNB firmware: what the analyzer accepts or rejects
must be confirmed with the debugger on the real ZNB43.
"""

from __future__ import annotations

import math
import re
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

import numpy as np

from calelektra.core.touchstone import Touchstone, touchstone_text
from calelektra.instruments.base import InstrumentError
from calelektra.instruments.znx_debug.settings import (
    AVERAGE_MODES,
    EXPORT_FORMATS,
    EXPORT_TO_TOUCHSTONE,
    HOLD_MODES,
    IMPEDANCE_MODES,
    SELECTIVITIES,
    SWEEP_TYPES,
    TOUCHSTONE_VERSIONS,
    TRACE_FORMATS,
    parse_bool,
    parse_choice,
)

C0 = 299_792_458.0
IFBW_STEPS = (1.0, 1.5, 2.0, 3.0, 5.0, 7.0)
IFBW_RANGE = (1.0, 10e6)
NOISE_DBM_PER_HZ = -110.0  # equivalent input noise of the simulated receivers
DEFAULT_DIRECTORY = "C:\\Users\\Public\\Documents\\Rohde-Schwarz\\Vna"
ERROR_QUEUE_LENGTH = 100
COMPLEX_FORMATS = ("SMIT", "POL", "ISM")
OPTIONAL_TRAILING = {"STAT", "IMM", "LEV", "AMPL", "DUMM", "NEXT"}
SENSE_ROOTS = {"FREQ", "SWE", "AVER", "BAND", "SEGM"}
UNITS = {
    "": 1.0, "HZ": 1.0, "KHZ": 1e3, "MHZ": 1e6, "GHZ": 1e9, "DBM": 1.0, "DB": 1.0,
    "S": 1.0, "MS": 1e-3, "US": 1e-6, "NS": 1e-9, "PCT": 1.0, "%": 1.0,
}

Model = Callable[[np.ndarray], np.ndarray]


class ScpiCommandError(Exception):
    """Error that the simulated analyzer puts in its error queue."""

    MESSAGES = {
        -102: "Syntax error",
        -104: "Data type error",
        -109: "Missing parameter",
        -113: "Undefined header",
        -114: "Header suffix out of range",
        -200: "Execution error",
        -213: "Init ignored",
        -221: "Settings conflict",
        -222: "Data out of range",
        -224: "Illegal parameter value",
        -256: "File name not found",
        -350: "Queue overflow",
        -410: "Query INTERRUPTED",
    }

    def __init__(self, code: int, detail: str = ""):
        super().__init__(code, detail)
        self.code = code
        self.detail = detail

    def entry(self) -> str:
        message = self.MESSAGES.get(self.code, "Error")
        return f'{self.code},"{message};{self.detail}"' if self.detail else f'{self.code},"{message}"'


def cable_model(length_m: float = 0.6, velocity_factor: float = 0.7, loss_db_1ghz: float = 0.45) -> Model:
    """S-matrix ``(points, 2, 2)`` of a coaxial cable between ports 1 and 2."""
    delay = length_m / (velocity_factor * C0)

    def model(f: np.ndarray) -> np.ndarray:
        loss_db = loss_db_1ghz * np.sqrt(np.maximum(f, 0) / 1e9)
        s21 = 10 ** (-loss_db / 20) * np.exp(-2j * np.pi * f * delay)
        reflection = 0.035 * np.sqrt(np.clip(f / 43.5e9, 1e-4, None)) * np.exp(-4j * np.pi * f * delay)
        s = np.empty((len(f), 2, 2), dtype=complex)
        s[:, 0, 0] = reflection
        s[:, 1, 1] = reflection * np.exp(0.4j)
        s[:, 1, 0] = s[:, 0, 1] = s21
        return s

    return model


def short_form(word: str) -> str:
    """SCPI short form of a header node: ``FREQuency`` → ``FREQ``, ``SWEep`` → ``SWE``."""
    word = word.upper()
    if len(word) <= 4:
        return word
    return word[:3] if word[3] in "AEIOU" else word[:4]


def split_message(message: str) -> list[str]:
    """Commands of a message (``;`` outside quotes)."""
    parts, current, quote = [], [], ""
    for char in message:
        if quote:
            quote = "" if char == quote else quote
        elif char in "'\"":
            quote = char
        elif char == ";":
            parts.append("".join(current))
            current = []
            continue
        current.append(char)
    parts.append("".join(current))
    return [part.strip() for part in parts if part.strip()]


def split_arguments(text: str) -> list[str]:
    """Arguments of a command (``,`` outside quotes), quotes removed."""
    if not text.strip():
        return []
    arguments, current, quote = [], [], ""
    for char in text:
        if quote:
            if char == quote:
                quote = ""
            else:
                current.append(char)
        elif char in "'\"":
            quote = char
        elif char == ",":
            arguments.append("".join(current).strip())
            current = []
        else:
            current.append(char)
    arguments.append("".join(current).strip())
    return arguments


def has_query(message: str) -> bool:
    """True if any command of the message is a query (``?`` outside quotes)."""
    return any("?" in re.sub(r"'[^']*'|\"[^\"]*\"", "", part) for part in split_message(message))


def ieee_block(payload: bytes) -> bytes:
    length = str(len(payload))
    return f"#{len(length)}{length}".encode() + payload


def block_payload(answer: bytes) -> bytes:
    if not answer.startswith(b"#") or len(answer) < 2 or not answer[1:2].isdigit():
        raise InstrumentError("la respuesta no es un bloque binario")
    digits = int(answer[1:2])
    length = int(answer[2 : 2 + digits])
    return answer[2 + digits : 2 + digits + length]


def smooth(values: np.ndarray, aperture_pct: float) -> np.ndarray:
    """Moving average over ``aperture_pct`` % of the points (the analyzer smoothing)."""
    n = len(values)
    width = int(round(aperture_pct / 100 * n))
    if width < 2 or n < 3:
        return values
    width += 1 - width % 2
    kernel = np.ones(width)
    return np.convolve(values, kernel, mode="same") / np.convolve(np.ones(n), kernel, mode="same")


def formatted(values: np.ndarray, frequencies: np.ndarray, fmt: str) -> np.ndarray:
    """Formatted trace (``FDAT``) of the complex values of one S-parameter."""
    magnitude = np.abs(values)
    if fmt == "MLOG":
        return 20 * np.log10(np.maximum(magnitude, 1e-20))
    if fmt == "MLIN":
        return magnitude
    if fmt == "PHAS":
        return np.degrees(np.angle(values))
    if fmt == "UPH":
        return np.degrees(np.unwrap(np.angle(values)))
    if fmt == "SWR":
        clipped = np.minimum(magnitude, 0.999999)
        return (1 + clipped) / (1 - clipped)
    if fmt == "REAL":
        return values.real
    if fmt == "IMAG":
        return values.imag
    if fmt == "GDEL":
        if len(values) < 2 or np.ptp(frequencies) == 0:
            return np.zeros(len(values))
        return -np.gradient(np.unwrap(np.angle(values)), 2 * np.pi * frequencies)
    return values  # Smith and polar diagrams: complex values


@dataclass
class Call:
    suffixes: list[int | None]
    query: bool
    arguments: list[str]
    text: str

    def suffix(self, index: int) -> int:
        value = self.suffixes[index] if index < len(self.suffixes) else None
        return 1 if value is None else value

    def argument(self, index: int = 0) -> str:
        if index >= len(self.arguments) or self.arguments[index] == "":
            raise ScpiCommandError(-109, self.text)
        return self.arguments[index]


@dataclass
class SimTrace:
    name: str
    parameter: str = "S21"
    format: str = "MLOG"
    smoothing: bool = False
    aperture: float = 1.0
    hold: str = "OFF"
    held: np.ndarray | None = None


@dataclass
class SimChannel:
    number: int
    start: float
    stop: float
    points: int = 201
    sweep_type: str = "LIN"
    if_bandwidth: float = 10e3
    selectivity: str = "NORM"
    power: float = -10.0
    rf_output: bool = True
    averaging: bool = False
    average_count: int = 10
    average_mode: str = "AUTO"
    sweep_count: int = 1
    continuous: bool = True
    segments: list[tuple[float, float, int]] = field(default_factory=list)
    traces: list[SimTrace] = field(default_factory=list)
    active: str | None = None
    history: deque = field(default_factory=deque)
    data: np.ndarray | None = None  # S-matrix of the last sweep, averaged
    completed_sweeps: int = 0

    def trace(self, name: str) -> SimTrace | None:
        return next((t for t in self.traces if t.name == name), None)


class SimulatedZnb:
    """ZNB43 simulated at SCPI level; also a transport for the debugger and for ``RsZnxVna``."""

    binary = False  # the ZNx adapter then asks for ASCII data (``:FORM ASC``)

    def __init__(
        self,
        ports: int = 2,
        frequency_range: tuple[float, float] = (100e3, 43.5e9),
        power_range: tuple[float, float] = (-40.0, 0.0),
        max_points: int = 100001,
        calibrated: bool = True,
        model: Model | None = None,
        seed: int | None = 1,
    ):
        self.ports = ports
        self.fmin, self.fmax = frequency_range
        self.power_range = power_range
        self.max_points = max_points
        self.calibrated = calibrated  # MMEM:STOR:TRAC:PORT needs a full calibration of the ports
        self.model = model or cable_model()
        self.rng = np.random.default_rng(seed)
        self.files: dict[str, bytes] = {}
        self.directory = DEFAULT_DIRECTORY
        self.errors: deque[str] = deque()
        self.received: list[str] = []
        self._output: bytes | None = None
        self._handlers = self._handler_table()
        self._reset()

    # ── transport interface ────────────────────────────────────────────────
    def write(self, command: str) -> None:
        self._interrupt_pending()
        self._output = self.process(command)

    def query(self, command: str, timeout_ms: int | None = None) -> str:
        return self._answer(command).decode("latin-1").rstrip("\n")

    def query_block(self, command: str, timeout_ms: int | None = None) -> bytes:
        return block_payload(self._answer(command))

    def query_floats(self, command: str, timeout_ms: int | None = None) -> np.ndarray:
        answer = self._answer(command)
        if answer.startswith(b"#"):
            return self._decode_block(block_payload(answer))
        text = answer.decode("latin-1").strip()
        return np.array([float(item) for item in text.split(",") if item.strip()])

    def close(self) -> None:
        self._output = None

    def _interrupt_pending(self) -> None:
        if self._output is not None:
            self._output = None
            self._push(ScpiCommandError(-410))

    def _answer(self, command: str) -> bytes:
        self._interrupt_pending()
        answer = self.process(command)
        if answer is None:
            raise InstrumentError(f"«{command}»: el equipo no ha respondido (tiempo de espera agotado)")
        return answer

    # ── message processing ─────────────────────────────────────────────────
    def process(self, message: str) -> bytes | None:
        """Bytes the analyzer sends back for one message, or ``None`` if it sends nothing."""
        self.received.append(message)
        answers: list[bytes] = []
        path: list[str] = []
        for command in split_message(message):
            try:
                answer, path = self._execute(command, path)
            except ScpiCommandError as error:
                self._push(error)
                if -199 <= error.code <= -100:
                    break  # command error: the rest of the message is discarded
                continue  # execution error: a failed query gets no answer
            if answer is not None:
                answers.append(answer)
        return b";".join(answers) + b"\n" if answers else None

    def _push(self, error: ScpiCommandError) -> None:
        if len(self.errors) >= ERROR_QUEUE_LENGTH:
            self.errors[-1] = ScpiCommandError(-350).entry()
        else:
            self.errors.append(error.entry())

    def _execute(self, command: str, previous: list[str]) -> tuple[bytes | None, list[str]]:
        header, _, rest = command.replace("\t", " ").partition(" ")
        if header.startswith("*"):
            handler = self._common.get(header.upper())
            if handler is None:
                raise ScpiCommandError(-113, command)
            answer = handler(split_arguments(rest))
            return (answer.encode("latin-1") if isinstance(answer, str) else answer), previous
        query = header.endswith("?")
        nodes = [node for node in header.rstrip("?").split(":") if node]
        if not nodes:
            raise ScpiCommandError(-102, command)
        if not header.startswith(":") and previous:
            nodes = previous + nodes
        names, suffixes = [], []
        for node in nodes:
            match = re.fullmatch(r"([A-Za-z]+)(\d*)", node)
            if not match:
                raise ScpiCommandError(-102, command)
            names.append(short_form(match.group(1)))
            suffixes.append(int(match.group(2)) if match.group(2) else None)
        names, suffixes = self._normalize(names, suffixes)
        handler = self._handlers.get(tuple(names))
        if handler is None:
            raise ScpiCommandError(-113, command)
        answer = handler(Call(suffixes, query, split_arguments(rest), command))
        if isinstance(answer, str):
            answer = answer.encode("latin-1")
        return answer, nodes[:-1]

    @staticmethod
    def _normalize(names: list[str], suffixes: list[int | None]) -> tuple[list[str], list[int | None]]:
        names = ["BAND" if name == "BWID" else name for name in names]
        keep = [i for i, name in enumerate(names) if name != "RES"]
        names, suffixes = [names[i] for i in keep], [suffixes[i] for i in keep]
        while len(names) > 1 and names[-1] in OPTIONAL_TRAILING:
            names.pop()
            suffixes.pop()
        if names[0] in SENSE_ROOTS:
            names, suffixes = ["SENS", *names], [None, *suffixes]
        elif names[0] == "POW":
            names, suffixes = ["SOUR", *names], [None, *suffixes]
        if names == ["FORM", "DATA"]:
            names, suffixes = ["FORM"], suffixes[:1]
        return names, suffixes

    # ── state ──────────────────────────────────────────────────────────────
    def _new_channel(self, number: int) -> SimChannel:
        return SimChannel(number, start=self.fmin, stop=self.fmax)

    def _reset(self) -> None:
        channel = self._new_channel(1)
        channel.traces.append(SimTrace("Trc1", "S21"))
        channel.active = "Trc1"
        self.channels: dict[int, SimChannel] = {1: channel}
        self.windows: dict[int, list[str]] = {1: ["Trc1"]}
        self.data_format = "ASC"
        self.byte_order = "SWAP"
        self.display_update = False
        self.touchstone_version = "F11"

    def _channel(self, call: Call, index: int = 0) -> SimChannel:
        channel = self.channels.get(call.suffix(index))
        if channel is None:
            raise ScpiCommandError(-114, call.text)
        return channel

    def _find_trace(self, name: str, call: Call) -> tuple[SimChannel, SimTrace]:
        for channel in self.channels.values():
            trace = channel.trace(name)
            if trace is not None:
                return channel, trace
        raise ScpiCommandError(-224, f"{call.text} (no existe la traza «{name}»)")

    def _active(self, call: Call) -> tuple[SimChannel, SimTrace]:
        channel = self._channel(call)
        trace = channel.trace(channel.active or "")
        if trace is None:
            raise ScpiCommandError(-221, f"{call.text} (el canal no tiene traza activa)")
        return channel, trace

    def _changed(self, channel: SimChannel) -> None:
        """Stimulus, power or IFBW changed: old data, averaging and hold no longer valid."""
        channel.data = None
        channel.history.clear()
        for trace in channel.traces:
            trace.held = None

    # ── values ─────────────────────────────────────────────────────────────
    def _number(self, call: Call, low: float, high: float, index: int = 0) -> float:
        text = call.argument(index).strip().upper()
        if text == "MIN":
            return low
        if text == "MAX":
            return high
        match = re.fullmatch(r"([+-]?(?:\d+\.?\d*|\.\d+)(?:E[+-]?\d+)?)\s*([A-Z%]*)", text)
        if not match or match.group(2) not in UNITS:
            raise ScpiCommandError(-104, call.text)
        value = float(match.group(1)) * UNITS[match.group(2)]
        if not low <= value <= high:
            raise ScpiCommandError(-222, call.text)
        return value

    def _integer(self, call: Call, low: int, high: int) -> int:
        value = self._number(call, low, high)
        if value != int(value):
            raise ScpiCommandError(-104, call.text)
        return int(value)

    def _bool(self, call: Call) -> bool:
        try:
            return parse_bool(call.argument())
        except ValueError:
            raise ScpiCommandError(-224, call.text) from None

    def _choice(self, call: Call, choices: dict[str, str]) -> str:
        try:
            return parse_choice(call.argument(), choices)
        except ValueError:
            raise ScpiCommandError(-224, call.text) from None

    def _values(self, values: np.ndarray) -> bytes:
        values = np.asarray(values, dtype=float)
        if self.data_format == "ASC":
            return ",".join(f"{v:.12g}" for v in values).encode()
        dtype = ("<" if self.byte_order == "SWAP" else ">") + ("f4" if self.data_format == "REAL,32" else "f8")
        return ieee_block(values.astype(dtype).tobytes())

    def _decode_block(self, payload: bytes) -> np.ndarray:
        dtype = ("<" if self.byte_order == "SWAP" else ">") + ("f4" if self.data_format == "REAL,32" else "f8")
        return np.frombuffer(payload, dtype=dtype).astype(float)

    @staticmethod
    def _text(value: float) -> str:
        return f"{value:.12g}"

    @staticmethod
    def _flag(value: bool) -> str:
        return "1" if value else "0"

    # ── sweep ──────────────────────────────────────────────────────────────
    def _stimulus(self, channel: SimChannel) -> np.ndarray:
        if channel.sweep_type == "LOG":
            return np.geomspace(channel.start, channel.stop, channel.points)
        if channel.sweep_type == "SEGM":
            return np.concatenate([np.linspace(a, b, n) for a, b, n in channel.segments])
        if channel.sweep_type == "LIN":
            return np.linspace(channel.start, channel.stop, channel.points)
        return np.full(channel.points, (channel.start + channel.stop) / 2)

    def _sweep(self, channel: SimChannel) -> None:
        f = self._stimulus(channel)
        s = np.zeros((len(f), self.ports, self.ports), dtype=complex)
        model = self.model(f)
        size = min(self.ports, model.shape[1])
        s[:, :size, :size] = model[:, :size, :size]
        noise = 10 ** ((NOISE_DBM_PER_HZ + 10 * math.log10(channel.if_bandwidth) - channel.power) / 20)
        if not channel.rf_output:
            s[:] = 0
        s += noise / math.sqrt(2) * (self.rng.standard_normal(s.shape) + 1j * self.rng.standard_normal(s.shape))
        if channel.averaging and channel.average_count > 1:
            if channel.history.maxlen != channel.average_count:
                channel.history = deque(channel.history, maxlen=channel.average_count)
            channel.history.append(s)
            s = np.mean(np.asarray(channel.history), axis=0)
        else:
            channel.history.clear()
        channel.data = s
        for trace in channel.traces:
            if trace.hold != "OFF" and trace.format not in COMPLEX_FORMATS:
                values = self._formatted(channel, trace)
                if trace.held is None or trace.held.shape != values.shape:
                    trace.held = values
                else:
                    trace.held = (np.maximum if trace.hold == "MAX" else np.minimum)(trace.held, values)

    def _parameter_data(self, channel: SimChannel, trace: SimTrace) -> np.ndarray:
        if channel.data is None or len(channel.data) != len(self._stimulus(channel)):
            self._sweep(channel)
        i, j = int(trace.parameter[1]) - 1, int(trace.parameter[2]) - 1
        return channel.data[:, i, j]

    def _formatted(self, channel: SimChannel, trace: SimTrace) -> np.ndarray:
        values = formatted(self._parameter_data(channel, trace), self._stimulus(channel), trace.format)
        return smooth(values, trace.aperture) if trace.smoothing else values

    def _trace_data(self, channel: SimChannel, trace: SimTrace, kind: str, call: Call) -> bytes:
        if channel.continuous:
            self._sweep(channel)  # continuous mode: new data every time they are read
        if kind in ("SDAT", "MDAT"):
            values = self._parameter_data(channel, trace)
        elif kind == "FDAT":
            values = self._formatted(channel, trace)
            if trace.hold != "OFF" and trace.held is not None and trace.held.shape == values.shape:
                values = trace.held
        else:
            raise ScpiCommandError(-224, call.text)
        if np.iscomplexobj(values):
            pairs = np.empty(2 * len(values))
            pairs[0::2], pairs[1::2] = values.real, values.imag
            values = pairs
        return self._values(values)

    def _sweep_time(self, channel: SimChannel) -> float:
        return round(len(self._stimulus(channel)) * (1.25 / channel.if_bandwidth + 15e-6) + 2e-3, 6)

    # ── handlers ───────────────────────────────────────────────────────────
    def _handler_table(self) -> dict[tuple[str, ...], Callable[[Call], bytes | str | None]]:
        self._common: dict[str, Callable[[list[str]], bytes | str | None]] = {
            "*IDN?": lambda _a: f"Rohde-Schwarz,ZNB43-{self.ports}Port,1334.3330K43/000000,simulado (CAL-ELEKTRA)",
            "*RST": lambda _a: self._reset(),
            "*CLS": lambda _a: self.errors.clear(),
            "*OPC?": lambda _a: "1",
            "*OPC": lambda _a: None,
            "*WAI": lambda _a: None,
            "*ESR?": lambda _a: "0",
            "*STB?": lambda _a: "4" if self.errors else "0",
        }
        return {
            ("SENS", "FREQ", "STAR"): self._start,
            ("SENS", "FREQ", "STOP"): self._stop,
            ("SENS", "FREQ", "CENT"): self._center,
            ("SENS", "FREQ", "SPAN"): self._span,
            ("SENS", "SWE", "POIN"): self._points,
            ("SENS", "SWE", "STEP"): self._step,
            ("SENS", "SWE", "TYPE"): self._sweep_type,
            ("SENS", "SWE", "COUN"): self._sweep_count,
            ("SENS", "SWE", "TIME"): self._sweep_time_handler,
            ("SENS", "BAND"): self._if_bandwidth,
            ("SENS", "BAND", "SEL"): self._selectivity,
            ("SENS", "AVER"): self._averaging,
            ("SENS", "AVER", "COUN"): self._average_count,
            ("SENS", "AVER", "MODE"): self._average_mode,
            ("SENS", "AVER", "CLE"): self._average_clear,
            ("SENS", "SEGM", "DEL", "ALL"): self._segments_delete,
            ("SENS", "SEGM", "INS"): self._segment_insert,
            ("SENS", "SEGM", "COUN"): self._segment_count,
            ("SENS", "SEGM", "FREQ", "STAR"): lambda call: self._segment_value(call, 0),
            ("SENS", "SEGM", "FREQ", "STOP"): lambda call: self._segment_value(call, 1),
            ("SENS", "SEGM", "SWE", "POIN"): lambda call: self._segment_value(call, 2),
            ("SENS", "SEGM", "BAND", "CONT"): self._accept_flag,
            ("SENS", "SEGM", "POW", "CONT"): self._accept_flag,
            ("SOUR", "POW"): self._power,
            ("OUTP",): self._rf_output,
            ("INIT",): self._initiate,
            ("INIT", "CONT"): self._continuous,
            ("ABOR",): lambda call: None,
            ("CALC", "PAR", "SDEF"): self._define_trace,
            ("CALC", "PAR", "MEAS"): self._trace_measurement,
            ("CALC", "PAR", "CAT"): self._trace_catalog,
            ("CALC", "PAR", "SEL"): self._select_trace,
            ("CALC", "PAR", "DEL"): self._delete_trace,
            ("CALC", "PAR", "DEL", "CALL"): self._delete_channel_traces,
            ("CALC", "PAR", "DEL", "ALL"): self._delete_all_traces,
            ("CALC", "FORM"): self._trace_format,
            ("CALC", "SMO"): self._smoothing,
            ("CALC", "SMO", "APER"): self._smoothing_aperture,
            ("CALC", "PHOL"): self._hold,
            ("CALC", "DATA"): self._data,
            ("CALC", "DATA", "STIM"): self._stimulus_data,
            ("CALC", "DATA", "NSW", "COUN"): self._completed_sweeps,
            ("CALC", "DATA", "TRAC"): self._data_by_name,
            ("DISP", "WIND"): self._window,
            ("DISP", "WIND", "TRAC", "EFE"): self._feed,
            ("DISP", "WIND", "TRAC", "FEED"): self._feed,
            ("DISP", "TRAC", "Y", "AUTO"): self._autoscale,
            ("SYST", "DISP", "UPD"): self._display_update,
            ("SYST", "ERR"): self._error,
            ("SYST", "ERR", "ALL"): self._all_errors,
            ("FORM",): self._data_format,
            ("FORM", "BORD"): self._byte_order,
            ("MMEM", "STOR", "TRAC", "PORT"): self._export_touchstone,
            ("MMEM", "STOR", "TRAC", "OPT", "FORM"): self._touchstone_version,
            ("MMEM", "DATA"): self._file_data,
            ("MMEM", "CDIR"): self._directory,
            ("MMEM", "DEL"): self._delete_file,
        }

    # stimulus
    def _start(self, call: Call) -> str | None:
        channel = self._channel(call)
        if call.query:
            return self._text(channel.start)
        channel.start = self._number(call, self.fmin, self.fmax)
        channel.stop = max(channel.stop, channel.start)
        self._changed(channel)
        return None

    def _stop(self, call: Call) -> str | None:
        channel = self._channel(call)
        if call.query:
            return self._text(channel.stop)
        channel.stop = self._number(call, self.fmin, self.fmax)
        channel.start = min(channel.start, channel.stop)
        self._changed(channel)
        return None

    def _center(self, call: Call) -> str | None:
        channel = self._channel(call)
        if call.query:
            return self._text((channel.start + channel.stop) / 2)
        center = self._number(call, self.fmin, self.fmax)
        half = min((channel.stop - channel.start) / 2, center - self.fmin, self.fmax - center)
        channel.start, channel.stop = center - half, center + half
        self._changed(channel)
        return None

    def _span(self, call: Call) -> str | None:
        channel = self._channel(call)
        if call.query:
            return self._text(channel.stop - channel.start)
        span = self._number(call, 0.0, self.fmax - self.fmin)
        center = (channel.start + channel.stop) / 2
        start = min(max(center - span / 2, self.fmin), self.fmax - span)
        channel.start, channel.stop = start, start + span
        self._changed(channel)
        return None

    def _points(self, call: Call) -> str | None:
        channel = self._channel(call)
        if call.query:
            return str(len(self._stimulus(channel)))
        points = self._integer(call, 1, self.max_points)
        if channel.sweep_type == "SEGM":
            raise ScpiCommandError(-221, f"{call.text} (barrido segmentado: los puntos son los de los segmentos)")
        channel.points = points
        self._changed(channel)
        return None

    def _step(self, call: Call) -> str | None:
        channel = self._channel(call)
        if channel.sweep_type != "LIN":
            raise ScpiCommandError(-221, f"{call.text} (el paso solo existe en el barrido lineal)")
        span = channel.stop - channel.start
        if call.query:
            return self._text(span / (channel.points - 1) if channel.points > 1 else 0.0)
        step = self._number(call, 0.0, self.fmax - self.fmin)
        if step <= 0 or step > span:
            raise ScpiCommandError(-222, call.text)
        points = int(math.floor(span / step + 1e-9)) + 1
        if points > self.max_points:
            raise ScpiCommandError(-222, f"{call.text} (más de {self.max_points} puntos)")
        channel.points = points
        channel.stop = channel.start + (points - 1) * step
        self._changed(channel)
        return None

    def _sweep_type(self, call: Call) -> str | None:
        channel = self._channel(call)
        if call.query:
            return channel.sweep_type
        sweep_type = self._choice(call, SWEEP_TYPES)
        if sweep_type == "SEGM" and not channel.segments:
            raise ScpiCommandError(-221, f"{call.text} (no hay segmentos definidos)")
        channel.sweep_type = sweep_type
        self._changed(channel)
        return None

    def _sweep_count(self, call: Call) -> str | None:
        channel = self._channel(call)
        if call.query:
            return str(channel.sweep_count)
        channel.sweep_count = self._integer(call, 1, 100000)
        return None

    def _sweep_time_handler(self, call: Call) -> str | None:
        channel = self._channel(call)
        if call.query:
            return self._text(self._sweep_time(channel))
        raise ScpiCommandError(-221, f"{call.text} (el simulador solo calcula el tiempo automático)")

    def _if_bandwidth(self, call: Call) -> str | None:
        channel = self._channel(call)
        if call.query:
            return self._text(channel.if_bandwidth)
        requested = self._number(call, *IFBW_RANGE)
        allowed = sorted(m * 10**e for e in range(8) for m in IFBW_STEPS if m * 10**e <= IFBW_RANGE[1])
        channel.if_bandwidth = next(v for v in allowed if v >= requested * (1 - 1e-9))  # rounded up
        self._changed(channel)
        return None

    def _selectivity(self, call: Call) -> str | None:
        channel = self._channel(call)
        if call.query:
            return channel.selectivity
        channel.selectivity = self._choice(call, SELECTIVITIES)
        return None

    def _power(self, call: Call) -> str | None:
        channel = self._channel(call)
        if call.query:
            return self._text(channel.power)
        channel.power = round(self._number(call, *self.power_range), 2)
        self._changed(channel)
        return None

    def _rf_output(self, call: Call) -> str | None:
        channel = self._channel(call)
        if call.query:
            return self._flag(channel.rf_output)
        channel.rf_output = self._bool(call)
        self._changed(channel)
        return None

    # averaging and sweep control
    def _averaging(self, call: Call) -> str | None:
        channel = self._channel(call)
        if call.query:
            return self._flag(channel.averaging)
        channel.averaging = self._bool(call)
        channel.history.clear()
        return None

    def _average_count(self, call: Call) -> str | None:
        channel = self._channel(call)
        if call.query:
            return str(channel.average_count)
        channel.average_count = self._integer(call, 1, 1000)
        channel.history.clear()
        return None

    def _average_mode(self, call: Call) -> str | None:
        channel = self._channel(call)
        if call.query:
            return channel.average_mode
        channel.average_mode = self._choice(call, AVERAGE_MODES)
        return None

    def _average_clear(self, call: Call) -> None:
        self._channel(call).history.clear()

    def _initiate(self, call: Call) -> None:
        channel = self._channel(call)
        if channel.continuous:
            raise ScpiCommandError(-213, f"{call.text} (el canal está en barrido continuo)")
        for _ in range(channel.sweep_count):
            self._sweep(channel)
        channel.completed_sweeps = channel.sweep_count

    def _continuous(self, call: Call) -> str | None:
        channel = self._channel(call)
        if call.query:
            return self._flag(channel.continuous)
        channel.continuous = self._bool(call)
        return None

    def _completed_sweeps(self, call: Call) -> str:
        return str(self._channel(call).completed_sweeps)

    # segments (frequency list)
    def _segments_delete(self, call: Call) -> None:
        channel = self._channel(call)
        channel.segments.clear()
        if channel.sweep_type == "SEGM":
            channel.sweep_type = "LIN"
        self._changed(channel)

    def _segment_insert(self, call: Call) -> None:
        channel = self._channel(call)
        number = call.suffix(1)
        if not 1 <= number <= len(channel.segments) + 1:
            raise ScpiCommandError(-114, call.text)
        start = self._number(call, self.fmin, self.fmax, 0)
        stop = self._number(call, self.fmin, self.fmax, 1)
        points = int(self._number(call, 1, self.max_points, 2))
        if stop < start or (points == 1 and stop != start):
            raise ScpiCommandError(-222, call.text)
        channel.segments.insert(number - 1, (start, stop, points))
        self._changed(channel)

    def _segment_count(self, call: Call) -> str:
        return str(len(self._channel(call).segments))

    def _segment_value(self, call: Call, index: int) -> str:
        channel = self._channel(call)
        number = call.suffix(1)
        if not call.query or not 1 <= number <= len(channel.segments):
            raise ScpiCommandError(-114, call.text)
        value = channel.segments[number - 1][index]
        return str(value) if index == 2 else self._text(value)

    def _accept_flag(self, call: Call) -> str | None:
        self._channel(call)
        if call.query:
            return "0"
        self._bool(call)
        return None

    # traces
    def _parameter(self, text: str, call: Call) -> str:
        match = re.fullmatch(r"S(\d)(\d)|S(\d\d)(\d\d)", text.strip().upper())
        if not match:
            raise ScpiCommandError(-224, call.text)
        i, j = (int(g) for g in (match.groups()[:2] if match.group(1) else match.groups()[2:]))
        if not (1 <= i <= self.ports and 1 <= j <= self.ports):
            raise ScpiCommandError(-224, f"{call.text} (el analizador tiene {self.ports} puertos)")
        return f"S{i}{j}"

    def _define_trace(self, call: Call) -> None:
        name, parameter = call.argument(0), self._parameter(call.argument(1), call)
        if any(channel.trace(name) for channel in self.channels.values()):
            raise ScpiCommandError(-224, f"{call.text} (ya existe una traza «{name}»)")
        number = call.suffix(0)
        channel = self.channels.setdefault(number, self._new_channel(number))
        channel.traces.append(SimTrace(name, parameter))
        channel.active = name

    def _trace_measurement(self, call: Call) -> str | None:
        channel, trace = self._find_trace(call.argument(0), call)
        if call.query:
            return f"'{trace.parameter}'"
        trace.parameter = self._parameter(call.argument(1), call)
        trace.held = None
        return None

    def _trace_catalog(self, call: Call) -> str:
        channel = self._channel(call)
        return "'" + ",".join(f"{t.name},{t.parameter}" for t in channel.traces) + "'"

    def _select_trace(self, call: Call) -> str | None:
        channel = self._channel(call)
        if call.query:
            return f"'{channel.active or ''}'"
        name = call.argument()
        if channel.trace(name) is None:
            raise ScpiCommandError(-224, f"{call.text} (la traza «{name}» no está en el canal {channel.number})")
        channel.active = name
        return None

    def _remove(self, channel: SimChannel, names: list[str]) -> None:
        channel.traces = [t for t in channel.traces if t.name not in names]
        for traces in self.windows.values():
            traces[:] = [name for name in traces if name not in names]
        if channel.active in names:
            channel.active = channel.traces[0].name if channel.traces else None

    def _delete_trace(self, call: Call) -> None:
        channel, trace = self._find_trace(call.argument(), call)
        self._remove(channel, [trace.name])

    def _delete_channel_traces(self, call: Call) -> None:
        channel = self._channel(call)
        self._remove(channel, [t.name for t in channel.traces])

    def _delete_all_traces(self, call: Call) -> None:
        for channel in self.channels.values():
            self._remove(channel, [t.name for t in channel.traces])

    def _trace_format(self, call: Call) -> str | None:
        _channel, trace = self._active(call)
        if call.query:
            return trace.format
        trace.format = self._choice(call, TRACE_FORMATS)
        trace.held = None
        return None

    def _smoothing(self, call: Call) -> str | None:
        _channel, trace = self._active(call)
        if call.query:
            return self._flag(trace.smoothing)
        trace.smoothing = self._bool(call)
        trace.held = None
        return None

    def _smoothing_aperture(self, call: Call) -> str | None:
        _channel, trace = self._active(call)
        if call.query:
            return self._text(trace.aperture)
        trace.aperture = self._number(call, 0.05, 100.0)
        trace.held = None
        return None

    def _hold(self, call: Call) -> str | None:
        _channel, trace = self._active(call)
        if call.query:
            return trace.hold
        trace.hold = self._choice(call, HOLD_MODES)
        trace.held = None  # enabling, disabling or setting it again restarts the hold
        return None

    # data
    def _data(self, call: Call) -> bytes:
        channel, trace = self._active(call)
        if not call.query:
            raise ScpiCommandError(-200, f"{call.text} (el simulador no admite escribir datos)")
        return self._trace_data(channel, trace, call.argument().upper(), call)

    def _data_by_name(self, call: Call) -> bytes:
        channel, trace = self._find_trace(call.argument(0), call)
        return self._trace_data(channel, trace, call.argument(1).upper(), call)

    def _stimulus_data(self, call: Call) -> bytes:
        return self._values(self._stimulus(self._channel(call)))

    # display
    def _window(self, call: Call) -> str | None:
        number = call.suffix(1)
        if call.query:
            return self._flag(number in self.windows)
        if self._bool(call):
            self.windows.setdefault(number, [])
        else:
            self.windows.pop(number, None)
        return None

    def _feed(self, call: Call) -> None:
        number = call.suffix(1)
        if number not in self.windows:
            raise ScpiCommandError(-114, f"{call.text} (no existe el diagrama {number})")
        _channel, trace = self._find_trace(call.argument(), call)
        if trace.name not in self.windows[number]:
            self.windows[number].append(trace.name)

    def _autoscale(self, call: Call) -> None:
        if len(call.arguments) > 1:
            self._find_trace(call.argument(1), call)

    def _display_update(self, call: Call) -> str | None:
        if call.query:
            return self._flag(self.display_update)
        if call.argument().strip().upper() != "ONCE":
            self.display_update = self._bool(call)
        return None

    # status and data format
    def _error(self, call: Call) -> str:
        return self.errors.popleft() if self.errors else '0,"No error"'

    def _all_errors(self, call: Call) -> str:
        if not self.errors:
            return '0,"No error"'
        entries = ",".join(self.errors)
        self.errors.clear()
        return entries

    def _data_format(self, call: Call) -> str | None:
        if call.query:
            return "ASC,0" if self.data_format == "ASC" else self.data_format
        text = ",".join(call.arguments).upper().replace(" ", "")
        if text in ("ASC", "ASCII", "ASC,0", "ASCII,0"):
            self.data_format = "ASC"
        elif text in ("REAL,32", "REAL,64"):
            self.data_format = text
        else:
            raise ScpiCommandError(-224, call.text)
        return None

    def _byte_order(self, call: Call) -> str | None:
        if call.query:
            return self.byte_order
        order = call.argument().upper()
        if order.startswith("NORM"):
            self.byte_order = "NORM"
        elif order.startswith("SWAP"):
            self.byte_order = "SWAP"
        else:
            raise ScpiCommandError(-224, call.text)
        return None

    # files
    def _file_key(self, name: str) -> str:
        name = name.replace("/", "\\")
        if not re.match(r"^[A-Za-z]:\\|^\\", name):
            name = self.directory.rstrip("\\") + "\\" + name
        return name.lower()

    def _touchstone_version(self, call: Call) -> str | None:
        if call.query:
            return self.touchstone_version
        self.touchstone_version = self._choice(call, TOUCHSTONE_VERSIONS)
        return None

    def _export_touchstone(self, call: Call) -> None:
        channel = self.channels.get(int(self._number(call, 1, 1000, 0)))
        if channel is None:
            raise ScpiCommandError(-224, f"{call.text} (el canal no existe)")
        file_name = call.argument(1)
        try:
            data_format = parse_choice(call.argument(2), EXPORT_FORMATS)
        except ValueError:
            raise ScpiCommandError(-224, call.text) from None
        rest = call.arguments[3:]
        if rest and rest[0].strip().upper() in IMPEDANCE_MODES:
            rest = rest[1:]
        try:
            ports = [int(p) for p in rest]
        except ValueError:
            raise ScpiCommandError(-224, call.text) from None
        if not ports or len(set(ports)) != len(ports) or not all(1 <= p <= self.ports for p in ports):
            raise ScpiCommandError(-224, f"{call.text} (puertos no válidos)")
        if not re.search(rf"\.s{len(ports)}p$", file_name, re.IGNORECASE):
            raise ScpiCommandError(-224, f"{call.text} (la extensión debe ser .s{len(ports)}p)")
        if len(ports) > 1 and not self.calibrated:
            raise ScpiCommandError(-200, f"{call.text} (hace falta una calibración completa de los puertos)")
        if channel.data is None:
            self._sweep(channel)
        index = [p - 1 for p in ports]
        data = Touchstone(
            n_ports=len(ports),
            frequencies=self._stimulus(channel),
            s=channel.data[:, index][:, :, index],
            comments=["Rohde & Schwarz ZNB43 (simulado por CAL-ELEKTRA)", f"{datetime.now():%Y-%m-%d %H:%M:%S}"],
        )
        text = touchstone_text(data, EXPORT_TO_TOUCHSTONE[data_format], "HZ")
        if self.touchstone_version == "F20":
            lines = text.splitlines()
            option = next(i for i, line in enumerate(lines) if line.startswith("#"))
            keywords = [f"[Number of Ports] {len(ports)}"]
            if len(ports) == 2:
                keywords.append("[Two-Port Data Order] 21_12")
            keywords += [f"[Number of Frequencies] {len(data.frequencies)}", "[Network Data]"]
            lines = ["[Version] 2.0", *lines[:option + 1], *keywords, *lines[option + 1 :], "[End]"]
            text = "\n".join(lines) + "\n"
        self.files[self._file_key(file_name)] = text.encode()

    def _file_data(self, call: Call) -> bytes:
        if not call.query:
            raise ScpiCommandError(-200, f"{call.text} (el simulador no admite copiar ficheros al equipo)")
        content = self.files.get(self._file_key(call.argument()))
        if content is None:
            raise ScpiCommandError(-256, call.text)
        return ieee_block(content)

    def _directory(self, call: Call) -> str | None:
        if call.query:
            return f"'{self.directory}'"
        self.directory = call.argument()
        return None

    def _delete_file(self, call: Call) -> None:
        if self.files.pop(self._file_key(call.argument()), None) is None:
            raise ScpiCommandError(-256, call.text)
