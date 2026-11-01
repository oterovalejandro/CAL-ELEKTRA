"""Evaluate the contribution of a calibration kit to the uncertainty of calibrated measurements and compare it
with the uncertainty file of a VNA (docs/16): Markdown tables on the standard output.

    python tools/evaluar_contribucion_kit.py [biblioteca] [--kit KIT-N-02] [--vna VNA-01]

For the definitions of the kit model (specifications of the manufacturer) and those of the unit (its real
values): the effective error terms that the kit leaves; U (k = 2) of a reflection of |Γ| = 0,03, 0,1, 0,33 and 1
(OSM, at the worst phase); U of S11 and S21 of a through, a matched attenuator of 20 dB and a cable of 1 dB
(TOSM with a flush through and UOSM). Beside them, what the uncertainty file of the VNA gives.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np

from calelektra.catalog.library import Library
from calelektra.gui.app import EXAMPLE_LIBRARY
from calelektra.kits.contribution import kit_contribution, two_port_dut, worst_phase
from calelektra.uncertainty.engine import UncertaintyCalculationError

BANDS = {"≤ 10 GHz": (0.0, 10e9), "10 – 18 GHz": (10e9, 18e9)}
REFLECTIONS = (0.03, 0.1, 0.33, 1.0)
DUTS = {"through 0 dB": (0.0, None), "atenuador 20 dB (adaptación −20 dB)": (-20.0, -20.0), "cable 1 dB (adaptación −26 dB)": (-1.0, -26.0)}


def _g(value: float, digits: int = 3) -> str:
    return "—" if not np.isfinite(value) else f"{value:.{digits}g}".replace(".", ",")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("biblioteca", nargs="?", default=str(EXAMPLE_LIBRARY))
    parser.add_argument("--kit", default="KIT-N-02")
    parser.add_argument("--vna", default="VNA-01")
    args = parser.parse_args()
    library = Library.load(Path(args.biblioteca))
    unit = library.equipment[args.kit]
    model = library.model_of(unit)
    engine = library.uncertainty_engine(library.equipment[args.vna])
    f = np.linspace(0.1e9, 18e9, 180)
    masks = {band: (f > low) & (f <= high) for band, (low, high) in BANDS.items()}

    def lab(parameter: str, frequency: float, value: float, unit_name: str) -> float:
        try:
            return engine.calculate(engine.equipment_model, parameter, frequency, value, unit_name).expanded_uncertainty
        except (UncertaintyCalculationError, ValueError):
            return float("nan")

    print(f"## {engine.equipment_model} {args.vna}: su fichero de incertidumbres (total, k = 2)\n")
    print("| Banda | " + " | ".join(f"U\\|Γ\\| a {_g(m)}" for m in REFLECTIONS) + " | U S21 a 0 dB | U S21 a −20 dB |")
    print("|---" * (len(REFLECTIONS) + 3) + "|")
    for band, (low, high) in BANDS.items():
        middle = (max(low, 0.1e9) + high) / 2
        cells = [_g(lab("S11", middle, m, "linear")) for m in REFLECTIONS]
        cells += [f"{_g(lab('S21', middle, 0.0, 'dB'))} dB", f"{_g(lab('S21', middle, -20.0, 'dB'))} dB"]
        print(f"| {band} | " + " | ".join(cells) + " |")
    for title, equipment in ((f"{model.designation}: especificaciones del fabricante", None), (f"{args.kit}: sus valores reales", unit)):
        print(f"\n## {title}\n")
        print("### OSM: U(\\|Γ\\|) del kit, en la peor fase, k = 2\n")
        print("| Patrones | Banda | " + " | ".join(f"\\|Γ\\| = {_g(m)}" for m in REFLECTIONS) + " | Directividad | Adaptación de fuente | Seguimiento |")
        print("|---" * (len(REFLECTIONS) + 5) + "|")
        for gender in ("macho", "hembra"):
            standards = library.calibration_standards(model, equipment, "OSM", (gender,), frequencies=f)
            worst = {m: worst_phase(standards, m)["total"] for m in REFLECTIONS}
            terms = {t.name: t for t in kit_contribution(standards, np.zeros((len(f), 1, 1))).terms}
            for band, mask in masks.items():
                cells = [_g(worst[m][mask].max()) for m in REFLECTIONS]
                cells += [f"{terms['Directividad'].db[mask].min():.1f} dB".replace(".", ","), f"{terms['Adaptación de fuente'].db[mask].min():.1f} dB".replace(".", ",")]
                tracking = terms["Seguimiento de reflexión"]
                cells.append(f"±{_g(tracking.magnitude_db[mask].max(), 2)} dB, ±{_g(tracking.phase_deg[mask].max(), 2)}°")
                print(f"| {gender} | {band} | " + " | ".join(cells) + " |")
        print("\n### Dos puertos (puerto 1 macho, puerto 2 hembra): U del kit, k = 2\n")
        print("| Calibración | DUT | Banda | U\\|S11\\| | U S21 (dB) | U ∠S21 (°) |")
        print("|---|---|---|---|---|---|")
        for method in ("TOSM", "UOSM"):
            standards = library.calibration_standards(model, equipment, method, ("macho", "hembra"), frequencies=f)
            for name, (transmission, match) in DUTS.items():
                result = kit_contribution(standards, two_port_dut(f, transmission, match))
                s11, s21, phase = (result.breakdown(r, c, q)["total"] for r, c, q in ((0, 0, "modulo"), (1, 0, "db"), (1, 0, "fase")))
                for band, mask in masks.items():
                    print(f"| {method} | {name} | {band} | {_g(s11[mask].max())} | {_g(s21[mask].max())} | {_g(phase[mask].max())} |")
            terms = {(t.name, t.port): t for t in result.terms}
            load = terms[("Adaptación de carga", "puerto 2")]
            transmission = terms[("Seguimiento de transmisión", "1 → 2")]
            for band, mask in masks.items():
                print(f"| {method} | adaptación de carga efectiva / seguimiento de transmisión | {band} | {_g(load.db[mask].min())} dB | "
                      f"±{_g(transmission.magnitude_db[mask].max(), 2)} dB | ±{_g(transmission.phase_deg[mask].max(), 2)}° |")


if __name__ == "__main__":
    main()
