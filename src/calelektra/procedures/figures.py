"""Charts of the calibration certificate of a cable, in the format of those of the laboratory.

The format is that of the chart code of the laboratory (``graphics.py`` of its
S2P tools); only the format, the values are those of ``CableResults``, the same
as the tables of the certificate:

* a figure of 16 × 4 in, saved at 300 dpi;
* the measured trace in blue (0.9), its expanded uncertainty as a light blue
  band (alpha 0.5) with its mean in the legend (``u_exp ±0.006``), the limit
  of the specification in light salmon (``Limit``);
* «Frec (Hz)» with the ticks as ``5.0e+09``, dashed grid, the scale of the
  sweep (linear or logarithmic);
* where there are limits (the range of use), x over that range and y with
  half the span of the trace above and below, plus a margin.

The figures (keys of ``FIGURES`` in ``report.py``):

* ``vswr_fwd`` / ``vswr_rev`` (Fig. 1 / 2): VSWR of port 1 (S11) / port 2 (S22), smoothed (1 %);
* ``perdida_fwd`` / ``perdida_rev`` (Fig. 3 / 4): S21 / S12, magnitude (dB) with its limit and phase (º);
* ``retardo`` (Fig. 5): group delay of S21 (ns), smoothed (1 %).
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from calelektra.procedures.cable_results import CableResults, DirectionResults

SIZE_IN = (16, 4)
DPI = 300
TRACE = {"color": "blue", "linewidth": 0.9}
BAND = {"color": "lightblue", "alpha": 0.5}
LIMIT = {"color": "lightsalmon", "linewidth": 0.9}
NAMES = {
    "vswr_fwd": "figura_1_vswr_fwd.png",
    "vswr_rev": "figura_2_vswr_rev.png",
    "perdida_fwd": "figura_3_perdida_fwd.png",
    "perdida_rev": "figura_4_perdida_rev.png",
    "retardo": "figura_5_retardo.png",
}


class FiguresUnavailable(Exception):
    """Matplotlib is not installed."""


def _sci(x: float, _pos: int) -> str:
    return f"{x:.1e}"


def _mean(values: np.ndarray, mask: np.ndarray | None = None) -> float:
    chosen = values[mask] if mask is not None else values
    finite = chosen[np.isfinite(chosen)]
    return float(finite.mean()) if len(finite) else float("nan")


def _finite(values: np.ndarray | None) -> bool:
    return values is not None and bool(np.isfinite(values).any())


def _figure(columns: int = 1):
    try:
        from matplotlib.figure import Figure
    except ImportError as exc:  # pragma: no cover - dependency of the package
        raise FiguresUnavailable("Falta matplotlib para las gráficas del certificado (pip install matplotlib)") from exc
    figure = Figure(figsize=SIZE_IN)
    axes = figure.subplots(1, columns)
    return figure, (list(axes) if columns > 1 else [axes])


def _finish(axes, ylabel: str) -> None:
    from matplotlib.ticker import FuncFormatter

    axes.set_ylabel(ylabel)
    axes.set_xlabel("Frec (Hz)")
    axes.grid(True, which="both", linestyle="--", linewidth=0.5)
    axes.legend()
    axes.xaxis.set_major_formatter(FuncFormatter(_sci))


def _span(values: np.ndarray, below: float, above: float) -> tuple[float, float] | None:
    """y limits: half the span of the values above and below, plus ``below`` / ``above``."""
    finite = values[np.isfinite(values)]
    if not len(finite):
        return None
    low, high = float(finite.min()), float(finite.max())
    return low - (high - low) * 0.5 - below, high + (high - low) * 0.5 + above


def _range_of_use(axes, f: np.ndarray, mask: np.ndarray | None, limits: tuple[float, float] | None) -> None:
    if mask is None:
        return
    axes.set_xlim(float(f[mask].min()), float(f[mask].max()))
    if limits is not None and limits[1] > limits[0]:
        axes.set_ylim(*limits)


def range_of_use(results: CableResults) -> np.ndarray | None:
    """Points where the specification gives a limit (None without limits): the range of the axes."""
    found = np.zeros(len(results.frequencies), dtype=bool)
    for d in results.directions.values():
        found |= np.isfinite(d.vswr_limit) | np.isfinite(d.insertion_loss_limit)
    return found if found.any() else None


def vswr_figure(results: CableResults, d: DirectionResults, scale: str = "linear"):
    """Fig. 1 / 2: the VSWR of a port, smoothed, with its uncertainty and its limit."""
    f, mask = results.frequencies, range_of_use(results)
    figure, (axes,) = _figure()
    axes.set_xscale(scale)
    y = d.vswr_smoothed
    axes.plot(f, y, label=f"{d.reflection} (Reflection)", **TRACE)
    if _finite(d.vswr_u):
        axes.fill_between(f, y - d.vswr_u, y + d.vswr_u, label=f"u_exp ±{_mean(d.vswr_u, mask):.3f}", **BAND)
    if _finite(d.vswr_limit):
        axes.plot(f, d.vswr_limit, label="Limit", **LIMIT)
    _finish(axes, f"VSWR Port {d.port}")
    _range_of_use(axes, f, mask, _span(y[mask], 0.1, 0.3) if mask is not None else None)
    figure.tight_layout()
    return figure


def insertion_loss_figure(results: CableResults, d: DirectionResults, scale: str = "linear"):
    """Fig. 3 / 4: the transmission (Insertion Loss), magnitude in dB with its limit and phase, side by side."""
    f, mask = results.frequencies, range_of_use(results)
    figure, axes_list = _figure(2)
    for axes, unit, values, u, limit in (
        (axes_list[0], "dB", d.insertion_loss_db, d.insertion_loss_u, d.insertion_loss_limit),
        (axes_list[1], "º", d.phase_deg, d.phase_u_deg, None),
    ):
        axes.set_xscale(scale)
        axes.plot(f, values, label=f"{d.transmission} (Insertion Loss)", **TRACE)
        if _finite(u):
            axes.fill_between(f, values - u, values + u, label=f"u_exp({unit}) ±{_mean(u):.3f}", **BAND)
        if _finite(limit):
            axes.plot(f, limit, label="Limit", **LIMIT)
        _finish(axes, f"{d.transmission} ({unit})")
        _range_of_use(axes, f, mask, _span(values[mask], 3, 3) if mask is not None else None)
    figure.tight_layout()
    return figure


def delay_figure(results: CableResults, d: DirectionResults, scale: str = "linear"):
    """Fig. 5: the group delay of the transmission, smoothed, with its uncertainty."""
    f, mask = results.frequencies, range_of_use(results)
    figure, (axes,) = _figure()
    axes.set_xscale(scale)
    y = d.group_delay_ns
    axes.plot(f, y, label=f"{d.transmission} (Transmision)", **TRACE)
    if _finite(d.group_delay_u_ns):
        u = d.group_delay_u_ns
        axes.fill_between(f, y - u, y + u, label=f"u_exp(ns) ±{_mean(u):.3f}", **BAND)
    _finish(axes, f"{d.transmission} (ns)")
    limits = None
    if mask is not None and _finite(y[mask]):
        lowest = float(np.nanmin(y[mask]))
        limits = (lowest * 0.7, lowest * 1.3) if lowest > 0 else None
    _range_of_use(axes, f, mask, limits)
    figure.tight_layout()
    return figure


def certificate_figures(results: CableResults, folder: Path, logarithmic: bool = False, dpi: int = DPI) -> dict[str, Path]:
    """The five charts of the certificate as PNG files in ``folder`` (``FiguresUnavailable`` without matplotlib)."""
    scale = "log" if logarithmic and results.frequencies[0] > 0 else "linear"
    fwd, rev = results.directions["Fwd"], results.directions["Rev"]
    figures = {
        "vswr_fwd": vswr_figure(results, fwd, scale),
        "vswr_rev": vswr_figure(results, rev, scale),
        "perdida_fwd": insertion_loss_figure(results, fwd, scale),
        "perdida_rev": insertion_loss_figure(results, rev, scale),
        "retardo": delay_figure(results, fwd, scale),
    }
    folder.mkdir(parents=True, exist_ok=True)
    paths = {}
    for key, figure in figures.items():
        paths[key] = folder / NAMES[key]
        figure.savefig(str(paths[key]), dpi=dpi)
    return paths
