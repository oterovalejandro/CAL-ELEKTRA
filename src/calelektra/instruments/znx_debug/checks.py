"""Self-check of the remote control of the analyzer.

Writes every setting and reads it back, creates traces and changes their
parameter, format, smoothing and hold, runs sweeps (single mode, averaging and
restart), compares the data in ASCII and binary, checks that smoothing and max
hold only change the formatted trace, tries the frequency list and, on request,
the Touchstone export. The settings of the channel are restored at the end.

Results: ``OK``; ``AJUSTADO`` when the analyzer kept another value without an
error (rounding, limits); ``FALLO`` when it rejected the command or the result
is not the expected one; ``INFO`` for measured effects; ``OMITIDO``.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

import numpy as np

from calelektra.core.units import format_frequency
from calelektra.instruments.base import InstrumentError
from calelektra.instruments.znx_debug.settings import (
    AVERAGE_MODES,
    DATA_FORMATS,
    EXPORT_TO_TOUCHSTONE,
    SELECTIVITIES,
    SETTINGS,
    TRACE_FORMATS,
)

if TYPE_CHECKING:
    from calelektra.instruments.znx_debug.debugger import ZnxDebugger

OK, ADJUSTED, FAILED, INFO, SKIPPED = "OK", "AJUSTADO", "FALLO", "INFO", "OMITIDO"
CHECK_PREFIX = "CE_CHK_"
# Settings restored at the end, in this order (the sweep type before the frequencies)
RESTORED = (
    "sweep_type", "start", "stop", "points", "if_bandwidth", "if_selectivity", "power", "rf_output",
    "averaging", "average_count", "average_mode", "sweep_count", "continuous", "display_update", "touchstone_version",
)


@dataclass
class CheckResult:
    group: str
    item: str
    command: str
    written: str
    read: str
    status: str
    note: str = ""


class _Checker:
    def __init__(self, vna: ZnxDebugger):
        self.vna = vna
        self.results: list[CheckResult] = []

    def add(self, group: str, item: str, command: str, written: str, read: str, status: str, note: str = "") -> None:
        self.results.append(CheckResult(group, item, command, written, read, status, note))

    def setting(self, group: str, key: str, value: Any, expected: Any = None, trace: str | None = None, note: str = "") -> Any:
        """Write a setting and read it back; with ``expected``, the value read must be that one."""
        setting = SETTINGS[key]
        item = f"{setting.label} ({trace})" if trace else setting.label
        command = f"{setting.command(self.vna.channel)} {setting.encode(value)}"
        written = setting.show(setting.value(value), describe=False)
        try:
            read = self.vna.set(key, value, trace)
        except InstrumentError as exc:
            self.add(group, item, command, written, f"✗ {exc}", FAILED, note)
            return None
        if expected is None:
            status = OK if setting.matches(value, read) else ADJUSTED
        else:
            status = OK if setting.matches(expected, read) else FAILED
        self.add(group, item, command, written, setting.show(read, describe=False), status, note)
        return read

    def value(self, group: str, key: str, expected: Any, note: str = "") -> None:
        """Read a setting that another one has changed (e.g. the points after the step)."""
        setting = SETTINGS[key]
        command = f"{setting.command(self.vna.channel)}?"
        try:
            read = self.vna.get(key)
        except InstrumentError as exc:
            self.add(group, setting.label, command, setting.show(expected, describe=False), f"✗ {exc}", FAILED, note)
            return
        status = OK if setting.matches(expected, read) else FAILED
        self.add(group, setting.label, command, setting.show(expected, describe=False), setting.show(read, describe=False), status, note)

    def call(self, group: str, item: str, command: str, action: Callable[[], Any], expect: Callable[[Any], bool] | None = None,
             show: Callable[[Any], str] = str, note: str = "") -> Any:
        try:
            result = action()
        except (InstrumentError, ValueError) as exc:
            self.add(group, item, command, "", f"✗ {exc}", FAILED, note)
            return None
        status = OK if expect is None or expect(result) else FAILED
        self.add(group, item, command, "", show(result), status, note)
        return result

    def compare(self, group: str, item: str, command: str, ok: bool, read: str, note: str = "", info: bool = False) -> None:
        self.add(group, item, command, "", read, INFO if info else (OK if ok else FAILED), note)


def _snapshot(vna: ZnxDebugger) -> dict[str, Any]:
    saved: dict[str, Any] = {}
    for key in RESTORED:
        try:
            saved[key] = vna.get(key)
        except InstrumentError:
            pass
    for name, read in (("data_format", vna.get_data_format), ("selected", vna.get_selected_trace)):
        try:
            saved[name] = read()
        except InstrumentError:
            pass
    return saved


def _restore(vna: ZnxDebugger, saved: dict[str, Any], checker: _Checker) -> None:
    problems = []
    for key in RESTORED:
        if key in saved and not (key == "points" and saved.get("sweep_type") == "SEGM"):
            try:
                vna.set(key, saved[key])
            except InstrumentError as exc:
                problems.append(f"{SETTINGS[key].label}: {exc}")
    for name, action in (("data_format", vna.set_data_format), ("selected", vna.select_trace)):
        if saved.get(name):
            try:
                action(saved[name])
            except InstrumentError as exc:
                problems.append(f"{name}: {exc}")
    checker.compare("Final", "Configuración inicial restaurada", "", not problems, "; ".join(problems) or "sí")


def _db(values: np.ndarray) -> np.ndarray:
    return 20 * np.log10(np.maximum(np.abs(values), 1e-20))


def run_self_check(vna: ZnxDebugger, sweep: bool = True, export: bool = False, restore: bool = True) -> list[CheckResult]:
    previous, vna.check_errors = vna.check_errors, True  # every step must look at the error queue
    try:
        return _run(vna, sweep, export, restore)
    finally:
        vna.check_errors = previous


def _run(vna: ZnxDebugger, sweep: bool, export: bool, restore: bool) -> list[CheckResult]:
    check = _Checker(vna)
    ch = vna.channel

    # Session
    check.call("Sesión", "Identificación", "*IDN?", vna.identify)
    pending = vna.errors()
    check.compare("Sesión", "Cola de errores al empezar", "SYST:ERR?", True, "; ".join(pending) or "vacía", info=bool(pending))
    saved = _snapshot(vna) if restore else {}
    check.setting("Sesión", "display_update", "ON")

    # Frequency and sweep
    group = "Frecuencias y barrido"
    check.setting(group, "continuous", "OFF")
    check.setting(group, "sweep_type", "LIN")
    check.setting(group, "start", 100e6)
    check.setting(group, "stop", 10e9)
    check.setting(group, "center", 5e9)
    check.setting(group, "span", 2e9)
    check.value(group, "start", 4e9, note="inicio tras centro 5 GHz y span 2 GHz")
    check.value(group, "stop", 6e9, note="final tras centro 5 GHz y span 2 GHz")
    check.setting(group, "points", 201)
    check.setting(group, "points", 1001)
    check.setting(group, "start", 1e9)
    check.setting(group, "stop", 2e9)
    check.setting(group, "step", 10e6)
    check.value(group, "points", 101, note="1–2 GHz con paso de 10 MHz")
    check.setting(group, "sweep_type", "LOG")
    check.setting(group, "points", 401)
    check.setting(group, "sweep_type", "LIN")
    check.setting(group, "points", 101)

    # Power and IF
    group = "Potencia y FI"
    check.setting(group, "power", -20)
    check.setting(group, "power", -10)
    check.setting(group, "rf_output", "OFF")
    check.setting(group, "rf_output", "ON")
    check.setting(group, "if_bandwidth", 1e3)
    check.setting(group, "if_bandwidth", 1.2e3, note="valor intermedio: el equipo lo redondea a su escalón")
    check.setting(group, "if_bandwidth", 10e3)
    for selectivity in SELECTIVITIES:
        check.setting(group, "if_selectivity", selectivity)
    check.setting(group, "if_selectivity", "NORM")

    # Averaging and sweep count
    group = "Promediado"
    check.setting(group, "averaging", "ON")
    check.setting(group, "average_count", 10)
    for mode in AVERAGE_MODES:
        check.setting(group, "average_mode", mode)
    check.setting(group, "average_mode", "AUTO")
    check.setting(group, "averaging", "OFF")
    check.setting(group, "sweep_count", 3)
    check.setting(group, "sweep_count", 1)
    check.call(group, "Reiniciar promediado", f"SENS{ch}:AVER:CLE", vna.restart_averaging, show=lambda _r: "sin error")

    # Transfer format and export option
    group = "Formatos"
    for data_format in DATA_FORMATS:
        check.call(group, f"Formato de transferencia {data_format}", f"FORM {data_format}",
                   lambda f=data_format: vna.set_data_format(f), expect=lambda r, f=data_format: r == f)
    check.call(group, "Formato de transferencia ASC", "FORM ASC", lambda: vna.set_data_format("ASC"), expect=lambda r: r == "ASC")
    for version in ("F20", "F11"):
        check.setting(group, "touchstone_version", version)

    # Traces
    group = "Trazas"
    names = {p: f"{CHECK_PREFIX}{p}" for p in ("S11", "S21", "S12", "S22")}
    existing = set(vna.trace_names())
    for parameter, name in names.items():
        if name in existing:
            vna.delete_trace(name)
        check.call(group, f"Crear traza {name}", f"CALC{ch}:PAR:SDEF '{name}','{parameter}'",
                   lambda n=name, p=parameter: dict(vna.create_trace(n, p)).get(n), expect=lambda r, p=parameter: r == p)
    s11, s21 = names["S11"], names["S21"]
    check.call(group, "Seleccionar traza", f"CALC{ch}:PAR:SEL '{s21}'", lambda: vna.select_trace(s21), expect=lambda r: r == s21)
    check.call(group, "Cambiar el parámetro medido", f"CALC{ch}:PAR:MEAS '{s11}','S22'",
               lambda: vna.set_trace_parameter(s11, "S22"), expect=lambda r: r == "S22")
    check.call(group, "Volver al parámetro medido", f"CALC{ch}:PAR:MEAS '{s11}','S11'",
               lambda: vna.set_trace_parameter(s11, "S11"), expect=lambda r: r == "S11")
    group = "Formato y procesado de trazas"
    for trace_format in TRACE_FORMATS:
        check.setting(group, "trace_format", trace_format, trace=s21)
    check.setting(group, "trace_format", "MLOG", trace=s21)
    check.setting(group, "smoothing", "ON", trace=s21)
    check.setting(group, "smoothing_aperture", 2.5, trace=s21)
    check.setting(group, "smoothing", "OFF", trace=s21)
    for mode in ("MAX", "MIN", "OFF"):
        check.setting(group, "hold", mode, trace=s21)
    check.call(group, "Reiniciar max hold", f"CALC{ch}:PHOL OFF / MAX", lambda: (vna.set_hold(s21, "MAX"), vna.restart_hold(s21))[1],
               expect=lambda r: r == "MAX")
    check.setting(group, "hold", "OFF", trace=s21)

    if sweep:
        _sweep_checks(vna, check, s11, s21)
        if saved.get("sweep_type") != "SEGM":
            _list_checks(vna, check)
        else:
            check.add("Lista de frecuencias", "Barrido segmentado", "", "", "", SKIPPED,
                      "el canal ya tenía una tabla de segmentos y no se borra")
    if export:
        _export_checks(vna, check)

    for name in names.values():
        try:
            vna.delete_trace(name)
        except InstrumentError as exc:
            check.compare("Final", f"Borrar traza {name}", f"CALC{ch}:PAR:DEL '{name}'", False, f"✗ {exc}")
    if restore:
        _restore(vna, saved, check)
    return check.results


def _sweep_checks(vna: ZnxDebugger, check: _Checker, s11: str, s21: str) -> None:
    ch = vna.channel
    group = "Barridos y datos"
    check.call(group, "Barrido único", f"INIT{ch};*OPC?", vna.single_sweep, expect=lambda n: n == 1, show=lambda n: f"{n} barrido(s)")
    frequencies = check.call(group, "Frecuencias del barrido (ASCII)", f"CALC{ch}:DATA:STIM?", vna.get_frequencies,
                             expect=lambda f: len(f) == 101 and abs(f[0] - 1e9) < 1 and abs(f[-1] - 2e9) < 1,
                             show=lambda f: f"{len(f)} puntos, {format_frequency(f[0])} – {format_frequency(f[-1])}")
    reference = vna.read_trace(s21, "SDAT") if frequencies is not None else None
    for data_format in ("REAL,32", "REAL,64"):
        try:
            vna.set_data_format(data_format)
            binary = vna.get_frequencies()
            data = vna.read_trace(s21, "SDAT")
            same = frequencies is not None and len(binary) == len(frequencies) and np.allclose(binary, frequencies, rtol=1e-6)
            same = same and reference is not None and np.allclose(data, reference, rtol=1e-5, atol=1e-6)
            check.compare(group, f"Datos en {data_format} iguales que en ASCII", f"FORM {data_format}", same,
                          f"{len(binary)} frecuencias, {len(data)} puntos de {s21}")
        except InstrumentError as exc:
            check.compare(group, f"Datos en {data_format}", f"FORM {data_format}", False, f"✗ {exc}")
    vna.set_data_format("ASC")
    check.call(group, "Datos SDAT (complejos)", f"CALC{ch}:DATA? SDAT", lambda: vna.read_trace(s21, "SDAT"),
               expect=lambda v: len(v) == 101 and np.iscomplexobj(v), show=lambda v: f"{len(v)} puntos complejos")
    check.call(group, "Datos FDAT (formato MLOG)", f"CALC{ch}:DATA? FDAT", lambda: vna.read_trace(s21, "FDAT"),
               expect=lambda v: len(v) == 101 and not np.iscomplexobj(v), show=lambda v: f"{len(v)} valores en dB")

    # Smoothing: the formatted trace changes, the unformatted data must not
    try:
        vna.set_smoothing(s11, "OFF")
        sdat_before, fdat_before = vna.read_trace(s11, "SDAT"), vna.read_trace(s11, "FDAT")
        vna.set_smoothing_aperture(s11, 5)
        vna.set_smoothing(s11, "ON")
        sdat_after, fdat_after = vna.read_trace(s11, "SDAT"), vna.read_trace(s11, "FDAT")
        vna.set_smoothing(s11, "OFF")
        check.compare("Suavizado", "SDAT no cambia con el suavizado", f"CALC{ch}:SMO ON", np.array_equal(sdat_before, sdat_after),
                      "igual" if np.array_equal(sdat_before, sdat_after) else "cambia",
                      note="los datos exportados por el software (SDAT) salen sin suavizar")
        change = float(np.max(np.abs(fdat_after - fdat_before)))
        check.compare("Suavizado", "FDAT cambia con el suavizado", f"CALC{ch}:SMO ON", True,
                      f"diferencia máxima {change:.3f} dB", info=True)
    except InstrumentError as exc:
        check.compare("Suavizado", "Efecto del suavizado", f"CALC{ch}:SMO", False, f"✗ {exc}")

    # Max hold: the formatted trace keeps the maxima of the sweeps since the (re)start
    try:
        vna.set_hold(s11, "MAX")
        for _ in range(3):
            vna.single_sweep()
        held = vna.read_trace(s11, "FDAT")
        last = _db(vna.read_trace(s11, "SDAT"))
        check.compare("Max hold", "FDAT ≥ último barrido", f"CALC{ch}:PHOL MAX", bool(np.all(held >= last - 1e-3)),
                      f"máximo retenido sobre el último barrido: hasta {float(np.max(held - last)):.3f} dB")
        vna.restart_hold(s11)
        vna.single_sweep()
        restarted = vna.read_trace(s11, "FDAT")
        current = _db(vna.read_trace(s11, "SDAT"))
        check.compare("Max hold", "Tras reiniciar, FDAT = barrido actual", f"CALC{ch}:PHOL OFF / MAX",
                      bool(np.allclose(restarted, current, atol=1e-3)), f"diferencia {float(np.max(np.abs(restarted - current))):.4f} dB")
        vna.set_hold(s11, "OFF")
    except InstrumentError as exc:
        check.compare("Max hold", "Efecto del max hold", f"CALC{ch}:PHOL", False, f"✗ {exc}")

    # Averaging with restart (single mode: one INIT runs all the averaged sweeps)
    group = "Promediado y reinicio"
    try:
        vna.set_averaging("ON")
        vna.set_average_count(4)
        vna.set_sweep_count(4)
        check.call(group, "Reiniciar barrido (modo único)", f"SENS{ch}:AVER:CLE + INIT{ch};*OPC?", vna.restart_sweep,
                   expect=lambda text: "4 barridos" in text)
        check.call(group, "Barridos completados", f"CALC{ch}:DATA:NSW:COUN?", vna.get_completed_sweeps, expect=lambda n: n == 4)
        vna.set_continuous("ON")
        check.call(group, "Reiniciar barrido (modo continuo)", "ABOR + SENS:AVER:CLE", vna.restart_sweep)
        vna.set_continuous("OFF")
        vna.set_sweep_count(1)
        vna.set_averaging("OFF")
    except InstrumentError as exc:
        check.compare(group, "Promediado", f"SENS{ch}:AVER", False, f"✗ {exc}")


def _list_checks(vna: ZnxDebugger, check: _Checker) -> None:
    group = "Lista de frecuencias"
    ch = vna.channel
    frequencies = np.concatenate([np.linspace(1e9, 1.5e9, 6), np.array([2e9, 2.5e9, 3e9, 3.5e9])])
    expected = [(1e9, 1.5e9, 6), (2e9, 3.5e9, 4)]
    check.call(group, "Segmentos de la lista", f"SENS{ch}:SEGM<n>:INS …", lambda: vna.set_frequency_list(frequencies),
               expect=lambda segments: len(segments) == len(expected) and all(
                   abs(a - c) < 1 and abs(b - d) < 1 and n == m for (a, b, n), (c, d, m) in zip(segments, expected)),
               show=lambda segments: "; ".join(f"{format_frequency(a)}–{format_frequency(b)} ({n})" for a, b, n in segments))
    check.value(group, "sweep_type", "SEGM")
    try:
        vna.single_sweep()
        stimulus = vna.get_frequencies()
        same = len(stimulus) == len(frequencies) and np.allclose(stimulus, frequencies, rtol=1e-9, atol=1)
        check.compare(group, "Frecuencias barridas = lista", f"CALC{ch}:DATA:STIM?", same, f"{len(stimulus)} puntos")
    except InstrumentError as exc:
        check.compare(group, "Barrido de la lista", f"INIT{ch}", False, f"✗ {exc}")
    check.setting(group, "sweep_type", "LIN")


def _export_checks(vna: ZnxDebugger, check: _Checker) -> None:
    from calelektra.instruments.znx_debug.debugger import read_exported

    group = "Exportación Touchstone"
    try:
        reference = vna.read_s_parameters()
    except InstrumentError as exc:
        check.compare(group, "Parámetros S de referencia", "", False, f"✗ {exc}")
        return
    folder = Path(tempfile.mkdtemp(prefix="calelektra_vna_"))
    for export_format, touchstone_format in EXPORT_TO_TOUCHSTONE.items():
        local = folder / f"calelektra_depuracion_{touchstone_format}.s2p"
        command = f"MMEM:STOR:TRAC:PORT {vna.channel},'{local.name}',{export_format},CIMP,1,2"
        try:
            vna.export_and_download(local, export_format)
            option = next((line for line in local.read_text(errors="replace").splitlines() if line.startswith("#")), "")
            data = read_exported(local)
            same = len(data.frequencies) == len(reference.frequencies) and np.allclose(data.s, reference.s, rtol=1e-3, atol=1e-4)
            ok = touchstone_format in option.upper().split() and same
            check.compare(group, f"Exportar en {export_format} ({touchstone_format})", command, ok,
                          f"«{option.strip()}», {len(data.frequencies)} puntos" + ("" if same else ", datos distintos de SDAT"))
        except InstrumentError as exc:
            check.compare(group, f"Exportar en {export_format} ({touchstone_format})", command, False, f"✗ {exc}")
        try:
            vna.delete_file(local.name)
        except InstrumentError:
            pass


def summary(results: list[CheckResult]) -> str:
    counts = {status: sum(1 for r in results if r.status == status) for status in (OK, ADJUSTED, FAILED, INFO, SKIPPED)}
    parts = [f"{counts[OK]} OK"]
    parts += [f"{counts[s]} {s.lower()}" for s in (ADJUSTED, FAILED, INFO, SKIPPED) if counts[s]]
    return f"{len(results)} comprobaciones: " + ", ".join(parts)


def report_text(results: list[CheckResult]) -> str:
    lines = []
    group = None
    for r in results:
        if r.group != group:
            group = r.group
            lines.append(f"\n{group}")
        written = f" = {r.written}" if r.written else ""
        lines.append(f"  {r.status:<9} {r.item}{written} → {r.read}")
        if r.command:
            lines.append(f"            {r.command}")
        if r.note:
            lines.append(f"            ({r.note})")
    lines.append("\n" + summary(results))
    return "\n".join(lines).lstrip("\n") + "\n"
