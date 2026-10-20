"""Text console of the VNA debugger: each command is one function of :class:`ZnxDebugger`.

    python -m calelektra.instruments.znx_debug --simulado
    python -m calelektra.instruments.znx_debug --visa TCPIP::192.168.0.50::hislip0::INSTR
    python -m calelektra.instruments.znx_debug --ip 192.168.0.50          # SCPI socket, port 5025, no VISA

Commands are typed as ``function arguments`` (``set_start 10MHz``,
``set_trace_format CE_S21 MLOG``, ``self_check export=on``); every SCPI
exchange is echoed. ``--script`` runs a file of commands and ``--comprobar``
runs the self-check and writes its report.
"""

from __future__ import annotations

import argparse
import cmd
import inspect
import shlex
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, TextIO

import numpy as np

from calelektra.core.touchstone import Touchstone
from calelektra.core.units import format_frequency
from calelektra.instruments.base import InstrumentError
from calelektra.instruments.znx_debug.checks import FAILED, CheckResult, report_text
from calelektra.instruments.znx_debug.debugger import ScpiError, ScpiExchange, ZnxDebugger
from calelektra.instruments.znx_debug.settings import SETTINGS, parse_bool

GROUPS: list[tuple[str, list[str]]] = [
    ("Sesión", [
        "identify", "reset", "clear_status", "errors", "wait", "scpi", "status", "set_display_update",
        "get_display_update", "set_data_format", "get_data_format", "self_check", "save_log",
    ]),
    ("Frecuencias y barrido", [
        "set_sweep_type", "get_sweep_type", "set_start", "get_start", "set_stop", "get_stop", "set_center", "get_center",
        "set_span", "get_span", "set_points", "get_points", "set_step", "get_step", "set_frequency_list", "get_segments",
        "get_frequencies",
    ]),
    ("Potencia y FI", [
        "set_power", "get_power", "set_rf_output", "get_rf_output", "set_if_bandwidth", "get_if_bandwidth",
        "set_if_selectivity", "get_if_selectivity",
    ]),
    ("Promediado y control del barrido", [
        "set_averaging", "get_averaging", "set_average_count", "get_average_count", "set_average_mode",
        "get_average_mode", "set_sweep_count", "get_sweep_count", "set_continuous", "get_continuous", "get_sweep_time",
        "single_sweep", "restart_sweep", "restart_averaging", "abort", "get_completed_sweeps",
    ]),
    ("Trazas", [
        "list_traces", "setup_s_parameters", "create_trace", "delete_trace", "select_trace", "get_selected_trace",
        "set_trace_parameter", "get_trace_parameter", "show_trace", "autoscale",
    ]),
    ("Formato y procesado de trazas", [
        "set_trace_format", "get_trace_format", "set_smoothing", "get_smoothing", "set_smoothing_aperture",
        "get_smoothing_aperture", "set_hold", "get_hold", "set_max_hold", "restart_hold",
    ]),
    ("Datos", ["read_trace", "read_s_parameters", "save_s2p"]),
    ("Exportación en el VNA", [
        "set_touchstone_version", "get_touchstone_version", "export_touchstone", "download_file", "export_and_download",
        "get_current_directory", "delete_file",
    ]),
]

# usage, description, SCPI commands («<c>» is the channel)
HELP: dict[str, tuple[str, str, str]] = {
    "identify": ("", "Identificación del equipo", "*IDN?"),
    "reset": ("", "Preset del equipo y espera a que termine", "*RST;*OPC?"),
    "clear_status": ("", "Borra la cola de errores y el registro de estado", "*CLS"),
    "errors": ("", "Lee y vacía la cola de errores", 'SYST:ERR? hasta 0,"No error"'),
    "wait": ("", "Espera a que terminen las operaciones pendientes", "*OPC?"),
    "scpi": ("<comando>", "Envía cualquier comando SCPI; si lleva «?» muestra la respuesta", "el comando escrito"),
    "status": ("", "Configuración del canal y de cada traza", "lee todos los parámetros"),
    "self_check": (
        "[sweep=on] [export=off] [restore=on]",
        "Comprobación completa: escribe y relee cada parámetro, trazas, barridos, datos, suavizado, max hold, "
        "lista de frecuencias y (export=on) exportación Touchstone; al final restaura la configuración",
        "todas las funciones",
    ),
    "save_log": ("<fichero>", "Guarda el registro de intercambios SCPI de la sesión", "—"),
    "set_data_format": ("ASC | REAL,32 | REAL,64", "Formato de transferencia de los datos numéricos", "FORM:BORD SWAP, FORM <formato>"),
    "get_data_format": ("", "Formato de transferencia de los datos numéricos", "FORM?, FORM:BORD?"),
    "set_frequency_list": (
        "<fichero.csv> | f1;f2;…",
        "Barrido segmentado con una lista de frecuencias (como el software: un segmento por tramo de paso constante)",
        "SENS<c>:SEGM:DEL:ALL, SENS<c>:SEGM<n>:INS …;*OPC?, SENS<c>:SEGM:BWID:CONT OFF, SENS<c>:SEGM:POW:CONT OFF, "
        "SENS<c>:SWE:TYPE SEGM",
    ),
    "get_segments": ("", "Segmentos del barrido segmentado", "SENS<c>:SEGM:COUN?, SENS<c>:SEGM<n>:FREQ:STAR?|STOP?, SENS<c>:SEGM<n>:SWE:POIN?"),
    "get_frequencies": ("", "Frecuencias del barrido", "CALC<c>:DATA:STIM?"),
    "single_sweep": ("[timeout_s]", "Un INIT en modo único (hace «sweep_count» barridos) y espera a que termine",
                     "INIT<c>;*OPC?, CALC<c>:DATA:NSW:COUN?"),
    "restart_sweep": (
        "",
        "Reinicia el barrido y el promediado, p. ej. después de conectar los cables",
        "continuo: ABOR, SENS<c>:AVER:CLE · único: SENS<c>:AVER:CLE, INIT<c>;*OPC?",
    ),
    "restart_averaging": ("", "Empieza un ciclo de promediado nuevo", "SENS<c>:AVER:CLE"),
    "abort": ("", "Aborta el barrido en curso", "ABOR"),
    "get_completed_sweeps": ("", "Barridos completados por el último INIT (modo único)", "CALC<c>:DATA:NSW:COUN?"),
    "list_traces": ("", "Trazas del canal y parámetro que mide cada una", "CALC<c>:PAR:CAT?"),
    "setup_s_parameters": (
        "[formato] [ports=2] [separate_windows=on] [delete_others=off]",
        "Crea (o reutiliza) las trazas CE_S11, CE_S21, CE_S12, CE_S22, cada una en su diagrama, con el formato indicado",
        "CALC<c>:PAR:SDEF, DISP:WIND<w>:STAT ON, DISP:WIND<w>:TRAC:EFE, CALC<c>:PAR:SEL, CALC<c>:FORM",
    ),
    "create_trace": ("<nombre> [S21] [diagrama]", "Crea una traza y la muestra en un diagrama",
                     "CALC<c>:PAR:SDEF '<nombre>','S21', DISP:WIND<w>:STAT ON, DISP:WIND<w>:TRAC:EFE '<nombre>'"),
    "delete_trace": ("<nombre>", "Borra una traza", "CALC<c>:PAR:DEL '<nombre>'"),
    "select_trace": ("<nombre>", "Activa una traza: formato, suavizado, retención y datos actúan sobre la traza activa",
                     "CALC<c>:PAR:SEL '<nombre>'"),
    "get_selected_trace": ("", "Traza activa del canal", "CALC<c>:PAR:SEL?"),
    "set_trace_parameter": ("<traza> <S11>", "Cambia el parámetro S que mide una traza", "CALC<c>:PAR:MEAS '<traza>','S11'"),
    "get_trace_parameter": ("<traza>", "Parámetro S que mide una traza", "CALC<c>:PAR:CAT?"),
    "show_trace": ("<traza> [diagrama]", "Muestra una traza en un diagrama", "DISP:WIND<w>:STAT ON, DISP:WIND<w>:TRAC:EFE '<traza>'"),
    "autoscale": ("<traza>", "Autoescala del eje vertical de la traza", "DISP:TRAC:Y:AUTO ONCE,'<traza>'"),
    "set_max_hold": ("<traza> on|off", "Activa o desactiva el max hold de la traza", "CALC<c>:PAR:SEL, CALC<c>:PHOL MAX|OFF"),
    "restart_hold": ("<traza>", "Reinicia el max hold / min hold de la traza", "CALC<c>:PHOL?, CALC<c>:PHOL OFF, CALC<c>:PHOL <modo>"),
    "read_trace": ("<traza> [SDAT|FDAT|MDAT]",
                   "Lee los datos de una traza: SDAT sin formato (complejos), FDAT con formato (lo que se ve), MDAT",
                   "CALC<c>:PAR:SEL '<traza>', CALC<c>:DATA? <tipo>"),
    "read_s_parameters": ("[ports=2]", "Lee la matriz S de las trazas del canal (datos SDAT)",
                          "CALC<c>:PAR:CAT?, CALC<c>:DATA:STIM?, CALC<c>:DATA? SDAT de cada traza"),
    "save_s2p": ("<fichero> [RI|MA|DB]", "Lee los parámetros S y los guarda en este ordenador como lo hace el software (sin suavizar)",
                 "los de read_s_parameters"),
    "export_touchstone": ("[fichero_en_vna] [COMP|LINP|LOGP] [puertos=1,2] [CIMP|PIMP]",
                          "El VNA escribe un Touchstone en su disco: COMP = RI, LINP = MA, LOGP = DB (necesita calibración completa)",
                          "MMEM:STOR:TRAC:PORT <c>,'<fichero>',<formato>,CIMP,1,2;*OPC?"),
    "download_file": ("<fichero_en_vna> <fichero_local>", "Copia un fichero del VNA a este ordenador", "MMEM:DATA? '<fichero>'"),
    "export_and_download": ("[fichero_local] [COMP|LINP|LOGP] [puertos=1,2]", "Exporta el Touchstone en el VNA y lo copia aquí",
                            "MMEM:STOR:TRAC:PORT …;*OPC?, MMEM:DATA? …"),
    "get_current_directory": ("", "Carpeta actual del VNA (donde escribe los ficheros sin ruta)", "MMEM:CDIR?"),
    "delete_file": ("<fichero_en_vna>", "Borra un fichero del VNA", "MMEM:DEL '<fichero>'"),
}
CONSOLE_COMMANDS = {
    "ayuda": "ayuda [función | grupo]: lista de funciones o detalle de una",
    "eco": "eco on | off | todo: muestra los intercambios SCPI (on oculta las lecturas vacías de SYST:ERR?)",
    "historial": "historial [n]: últimos intercambios SCPI",
    "salir": "salir: cierra la sesión",
}


def describe(name: str, channel: int = 1) -> tuple[str, str, str]:
    """Usage, description and SCPI commands of a console command."""
    key = name[4:]
    if name.startswith(("set_", "get_")) and key in SETTINGS and name not in HELP:
        setting = SETTINGS[key]
        header = setting.command(channel)
        select = f"CALC{channel}:PAR:SEL '<traza>', " if setting.per_trace else ""
        target = "<traza|*> " if setting.per_trace else ""
        options = f". Opciones: {', '.join(f'{k} ({v})' for k, v in setting.choices.items())}" if setting.choices else ""
        if name.startswith("set_"):
            return f"{target}<valor>", f"Escribe «{setting.label}» y lo lee de vuelta{options}", f"{select}{header} <valor>, {header}?"
        return target.strip(), f"Lee «{setting.label}»", f"{select}{header}?"
    usage, text, scpi = HELP.get(name, ("", "", ""))
    return usage, text, scpi.replace("<c>", str(channel))


def example(name: str) -> str:
    key = name[4:]
    if name.startswith("set_") and key in SETTINGS and SETTINGS[key].example:
        trace = "CE_S21 " if SETTINGS[key].per_trace else ""
        return f"{name} {trace}{SETTINGS[key].example}"
    return ""


def _convert(token: str, annotation: Any) -> Any:
    annotation = str(annotation).replace(" ", "")
    if annotation == "int":
        return int(token)
    if annotation == "bool":
        return parse_bool(token)
    if annotation in ("float", "float|None"):
        return float(token.replace(",", "."))
    return token


def _table(rows: list[tuple[Any, ...]], headers: tuple[str, ...]) -> str:
    texts = [tuple(str(cell) for cell in row) for row in rows]
    widths = [max(len(h), *(len(r[i]) for r in texts)) if texts else len(h) for i, h in enumerate(headers)]
    lines = ["  ".join(h.ljust(w) for h, w in zip(headers, widths)), "  ".join("─" * w for w in widths)]
    lines += ["  ".join(c.ljust(w) for c, w in zip(row, widths)) for row in texts]
    return "\n".join(lines) if texts else "(ninguna)"


def _values(values: np.ndarray) -> str:
    if len(values) == 0:
        return "sin datos"
    if np.iscomplexobj(values):
        db = 20 * np.log10(np.maximum(np.abs(values), 1e-20))
        first = ", ".join(f"{v.real:.5g}{v.imag:+.5g}j" for v in values[:3])
        return f"{len(values)} puntos complejos · |S| de {db.min():.2f} a {db.max():.2f} dB · primeros: {first} …"
    first = ", ".join(f"{v:.6g}" for v in values[:4])
    return f"{len(values)} valores · de {values.min():.6g} a {values.max():.6g} · primeros: {first} …"


def _frequencies(values: np.ndarray) -> str:
    if len(values) == 0:
        return "sin frecuencias"
    text = f"{len(values)} frecuencias · {format_frequency(values[0])} – {format_frequency(values[-1])}"
    steps = np.diff(values)
    if len(steps) and np.allclose(steps, steps[0], rtol=1e-6):
        text += f" · paso {format_frequency(steps[0])}"
    return text


def _file_head(path: Path, lines: int = 8) -> str:
    text = f"fichero {path.resolve()}"
    if path.suffix.lower().startswith(".s") and path.suffix.lower().endswith("p"):
        with path.open(encoding="utf-8", errors="replace") as handle:
            head = [line.rstrip() for _, line in zip(range(lines), handle)]
        text += "\n" + "\n".join(f"  │ {line}" for line in head) + "\n  │ …"
    return text


def render(name: str, result: Any) -> str:
    """Text shown in the console for the result of a function."""
    key = "hold" if name == "set_max_hold" else name[4:]
    if name.startswith(("set_", "get_")) and key in SETTINGS:
        setting = SETTINGS[key]
        if isinstance(result, dict):
            return "\n".join(f"{trace}: {setting.label} = {setting.show(value)}" for trace, value in result.items())
        return f"{setting.label}: {setting.show(result)}"
    if result is None:
        return "hecho"
    if name == "errors":
        return "\n".join(result) if result else "cola de errores vacía"
    if name in ("get_segments", "set_frequency_list"):
        rows = [(n, format_frequency(a), format_frequency(b), points) for n, (a, b, points) in enumerate(result, start=1)]
        return _table(rows, ("segmento", "inicio", "final", "puntos"))
    if name == "get_frequencies":
        return _frequencies(result)
    if name in ("single_sweep", "get_completed_sweeps"):
        return f"{result} barrido(s) completado(s)"
    if name in ("list_traces", "create_trace", "delete_trace"):
        return _table(result, ("traza", "parámetro"))
    if name == "setup_s_parameters":
        return _table(result, ("traza", "parámetro", "formato"))
    if name == "self_check":
        return report_text(result)
    if isinstance(result, Touchstone):
        lines = [f"{result.n_ports} puertos · {_frequencies(result.frequencies)}"]
        for i in range(result.n_ports):
            for j in range(result.n_ports):
                db = result.s_db(f"S{i + 1}{j + 1}")
                lines.append(f"  S{i + 1}{j + 1}: de {db.min():.2f} a {db.max():.2f} dB")
        return "\n".join(lines)
    if isinstance(result, Path):
        return _file_head(result)
    if isinstance(result, np.ndarray):
        return _values(result)
    if isinstance(result, bool):
        return "sí" if result else "no"
    if isinstance(result, dict):
        return _nested(result)
    return str(result)


def _nested(data: dict[str, Any], indent: int = 0) -> str:
    lines = []
    width = max((len(str(k)) for k, v in data.items() if not isinstance(v, dict)), default=0)
    for key, value in data.items():
        if isinstance(value, dict):
            lines.append(" " * indent + f"{key}")
            lines.append(_nested(value, indent + 2))
        else:
            lines.append(" " * indent + f"{str(key).ljust(width)}  {value}")
    return "\n".join(lines)


class DebugConsole(cmd.Cmd):
    prompt = "vna> "

    def __init__(self, debugger: ZnxDebugger, echo: str = "on", stdout: TextIO | None = None, log: TextIO | None = None):
        super().__init__(stdout=stdout or sys.stdout)
        self.debugger = debugger
        self.echo = echo
        self.log = log
        self.failures = 0
        self.traces: list[str] = []  # names seen in the last trace listing (Tab completion)
        debugger.listener = self._exchange
        self.functions: dict[str, Callable[..., Any]] = {
            name: getattr(debugger, name) for _group, names in GROUPS for name in names
        }

    def _print(self, text: str = "") -> None:
        self.stdout.write(text + "\n")

    def _exchange(self, exchange: ScpiExchange) -> None:
        if self.log:
            self.log.write(str(exchange) + "\n")
            self.log.flush()
        if self.echo == "todo" or (self.echo == "on" and not exchange.empty_error_queue):
            self._print("    " + exchange.line(timestamp=False))

    # ── cmd.Cmd ────────────────────────────────────────────────────────────
    def emptyline(self) -> bool:
        return False

    def completenames(self, text: str, *ignored: Any) -> list[str]:
        names = [*self.functions, *CONSOLE_COMMANDS]
        return [name for name in names if name.startswith(text)]

    def completedefault(self, text: str, line: str, begidx: int, endidx: int) -> list[str]:
        return [name for name in self.traces if name.startswith(text)]

    def default(self, line: str) -> bool:
        name, _, rest = line.strip().partition(" ")
        function = self.functions.get(name)
        if function is None:
            self._print(f"✗ «{name}» no es una función; escriba «ayuda» para ver la lista")
            self.failures += 1
            return False
        if name == "scpi":
            arguments = [rest.strip()] if rest.strip() else []  # sent as typed, quotes included
        else:
            lexer = shlex.shlex(rest, posix=True)
            lexer.whitespace_split, lexer.escape, lexer.commenters = True, "", ""  # keep backslashes of Windows paths
            try:
                arguments = list(lexer)
            except ValueError as exc:
                self._print(f"✗ {exc}")
                return False
        self.run(name, function, arguments)
        return False

    def run(self, name: str, function: Callable[..., Any], tokens: list[str]) -> None:
        signature = inspect.signature(function)
        parameters = list(signature.parameters.values())
        args: list[Any] = []
        kwargs: dict[str, Any] = {}
        try:
            if name == "scpi":
                args = tokens
            else:
                for token in tokens:
                    key, equals, value = token.partition("=")
                    if equals and key in signature.parameters:
                        kwargs[key] = _convert(value, signature.parameters[key].annotation)
                    elif len(args) < len(parameters):
                        args.append(_convert(token, parameters[len(args)].annotation))
                    else:
                        raise TypeError("sobran argumentos")
            signature.bind(*args, **kwargs)
        except (TypeError, ValueError) as exc:
            usage = describe(name, self.debugger.channel)[0]
            self._print(f"✗ argumentos no válidos ({exc}). Uso: {name} {usage}".rstrip())
            self.failures += 1
            return
        try:
            result = function(*args, **kwargs)
        except ScpiError as exc:
            self._print(f"✗ el VNA ha devuelto error en «{exc.operation}»:")
            if exc.cause:
                self._print(f"    ({exc.cause})")
            for error in exc.errors:
                self._print(f"    {error}")
            self.failures += 1
            return
        except (InstrumentError, ValueError, OSError) as exc:
            self._print(f"✗ {exc}")
            self.failures += 1
            return
        self._print(render(name, result))
        if name in ("list_traces", "create_trace", "delete_trace", "setup_s_parameters"):
            self.traces = [row[0] for row in result]
        if name == "self_check" and any(r.status == FAILED for r in result if isinstance(r, CheckResult)):
            self.failures += 1

    # ── console commands ───────────────────────────────────────────────────
    def do_ayuda(self, argument: str) -> bool:
        topic = argument.strip()
        channel = self.debugger.channel
        if topic in self.functions:
            usage, text, scpi = describe(topic, channel)
            self._print(f"{topic} {usage}".rstrip())
            self._print(f"  {text}")
            if scpi:
                self._print(f"  SCPI: {scpi}")
            if example(topic):
                self._print(f"  Ejemplo: {example(topic)}")
            return False
        groups = [(g, n) for g, n in GROUPS if not topic or topic.lower() in g.lower()]
        if not groups:
            self._print(f"✗ no hay ninguna función ni grupo «{topic}»")
            return False
        for group, names in groups:
            self._print(f"\n{group}")
            for name in names:
                self._print(f"  {name:<24} {describe(name, channel)[1].split('. Opciones')[0]}")
        if not topic:
            self._print("\nConsola")
            for text in CONSOLE_COMMANDS.values():
                self._print(f"  {text}")
            self._print("\nArgumentos separados por espacios; con nombre: self_check export=on. "
                        "Frecuencias con unidad: 10MHz, 1,5GHz. «ayuda <función>» muestra el detalle y los comandos SCPI.")
        return False

    do_help = do_ayuda

    def do_eco(self, argument: str) -> bool:
        value = argument.strip().lower()
        if value in ("on", "off", "todo"):
            self.echo = value
        self._print(f"eco {self.echo}")
        return False

    def do_historial(self, argument: str) -> bool:
        count = int(argument) if argument.strip().isdigit() else 20
        for exchange in self.debugger.history[-count:]:
            self._print(str(exchange))
        return False

    def do_salir(self, _argument: str) -> bool:
        return True

    do_exit = do_quit = do_salir

    def do_EOF(self, _argument: str) -> bool:
        self._print("")
        return True

    def run_script(self, path: Path) -> int:
        """Run the commands of a file (one per line, «#» comments); returns the number of failures."""
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            self._print(f"{self.prompt}{line}")
            if self.onecmd(line):
                break
        return self.failures


def _open(args: argparse.Namespace) -> ZnxDebugger:
    options = dict(channel=args.canal, check_errors=not args.sin_errores)
    if args.simulado:
        return ZnxDebugger.simulated(**options)
    if args.visa:
        return ZnxDebugger.open_visa(args.visa, timeout_ms=args.timeout, **options)
    return ZnxDebugger.open_socket(args.ip, args.puerto, timeout_ms=args.timeout, **options)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="cal-elektra-vna",
        description="Depuración del control remoto del VNA R&S ZNB/ZNx con los comandos SCPI del driver RsZnx 4.25.0.",
    )
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--simulado", action="store_true", help="ZNB43 simulado, sin equipo")
    target.add_argument("--visa", metavar="RECURSO", help="recurso VISA, p. ej. TCPIP::192.168.0.50::hislip0::INSTR (necesita pyvisa)")
    target.add_argument("--ip", metavar="DIRECCION", help="socket SCPI directo del equipo (puerto 5025), sin VISA")
    parser.add_argument("--puerto", type=int, default=5025, help="puerto del socket SCPI (5025)")
    parser.add_argument("--canal", type=int, default=1, help="canal del VNA (1)")
    parser.add_argument("--timeout", type=int, default=10_000, help="tiempo de espera de las respuestas en ms (10000)")
    parser.add_argument("--eco", choices=("on", "off", "todo"), default="on", help="intercambios SCPI en pantalla")
    parser.add_argument("--log", metavar="FICHERO", help="guarda cada intercambio SCPI en este fichero")
    parser.add_argument("--script", metavar="FICHERO", help="ejecuta los comandos del fichero (uno por línea) y termina")
    parser.add_argument("--comprobar", action="store_true", help="ejecuta la comprobación completa (self_check) y termina")
    parser.add_argument("--exportar", action="store_true", help="con --comprobar: incluye la exportación Touchstone")
    parser.add_argument("--informe", metavar="FICHERO", help="con --comprobar: guarda el informe y los comandos SCPI")
    parser.add_argument("--sin-errores", action="store_true", help="no lee SYST:ERR? después de cada función")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")  # consoles without UTF-8 (Windows, redirections)

    try:
        debugger = _open(args)
    except InstrumentError as exc:
        print(f"✗ {exc}")
        return 2
    log = open(args.log, "a", encoding="utf-8") if args.log else None
    if log:
        target = "simulado" if args.simulado else args.visa or f"{args.ip}:{args.puerto}"
        log.write(f"# CAL-ELEKTRA, depuración del VNA · {datetime.now():%Y-%m-%d %H:%M:%S} · {target} · canal {args.canal}\n")
    console = DebugConsole(debugger, echo="off" if args.comprobar and args.eco == "on" else args.eco, log=log)
    try:
        try:
            identity = debugger.identify()
        except InstrumentError as exc:
            print(f"✗ el equipo no responde a *IDN?: {exc}")
            return 2
        print(f"CAL-ELEKTRA · depuración del VNA · canal {debugger.channel}")
        print(f"Equipo: {identity}" + (" (simulado)" if debugger.is_simulated and "simulado" not in identity else ""))
        if args.comprobar:
            results = debugger.self_check(export=args.exportar)
            report = report_text(results)
            print(report)
            if args.informe:
                Path(args.informe).write_text(
                    f"{identity}\n\n{report}\nIntercambios SCPI\n" + "\n".join(str(e) for e in debugger.history) + "\n",
                    encoding="utf-8",
                )
                print(f"Informe guardado en {Path(args.informe).resolve()}")
            return 1 if any(r.status == FAILED for r in results) else 0
        console.onecmd("set_display_update on")  # the analyzer screen keeps updating in remote control
        if args.script:
            failures = console.run_script(Path(args.script))
            print(f"\nScript terminado: {failures} comando(s) con error")
            return 1 if failures else 0
        try:
            import readline  # noqa: F401  (line editing and history where available)
        except ImportError:
            pass
        print("Escriba «ayuda» para ver las funciones, «ayuda <función>» para el detalle y los comandos SCPI, «salir» para terminar.")
        print("Cada función envía sus comandos SCPI, lee el valor de vuelta y vacía la cola de errores (SYST:ERR?).")
        try:
            console.cmdloop()
        except KeyboardInterrupt:
            print()
        return 0
    finally:
        debugger.close()
        if log:
            log.close()


if __name__ == "__main__":
    sys.exit(main())
