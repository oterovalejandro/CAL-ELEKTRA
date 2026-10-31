"""Summary of a file of S-parameter data with uncertainties, and conversion to another format.

    python -m calelektra.sdata Example_Loadf_18831_20150824.sdatb
    python -m calelektra.sdata Example_Loadf_18831_20150824.sdatb --convertir carga.sdatcv
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from calelektra.core.units import format_frequency
from calelektra.sdata.files import FORMATS, load_sparam_data, save_sparam_data
from calelektra.sdata.model import SParamData
from calelektra.sdata.sdatb import sdatb_version
from calelektra.sdata.unclib import SDataError


def summary(path: Path) -> tuple[list[str], SParamData]:
    data = load_sparam_data(path)
    kind = f".sdatb versión {sdatb_version(path)}" if path.suffix.lower() == ".sdatb" else path.suffix.lower()
    f = data.frequencies
    lines = [
        path.name,
        f"  Formato: {kind}",
        f"  Frecuencias: {data.n_points}, de {format_frequency(f[0])} a {format_frequency(f[-1])}",
        f"  Puertos: {', '.join(p.label for p in data.ports)} (Zr: {', '.join(f'{z.real:g}' + (f'{z.imag:+g}j' if z.imag else '') + ' Ω' for z in data.zr)})",
        f"  Presupuesto: {len(data.vector.inputs)} entradas, {len(data.vector.indices)} dependencias",
    ]
    if data.conversions is not None:
        lines.append("  Con conversión de frecuencia")
    u_re, u_im = data.standard_uncertainty()
    lines.append("  Incertidumbre típica máxima (parte real / imaginaria):")
    for r in range(data.n_ports):
        for c in range(data.n_ports):
            lines.append(f"    S{r + 1}{c + 1}: {u_re[:, r, c].max():.3g} / {u_im[:, r, c].max():.3g}")
    return lines, data


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=f"Resumen de un fichero de parámetros S con incertidumbre ({FORMATS}).")
    parser.add_argument("fichero", type=Path, help="Fichero que se lee.")
    parser.add_argument("--convertir", type=Path, metavar="DESTINO", help="Escribe los datos en otro formato (.sdatb, .sdatcv o .sNp).")
    args = parser.parse_args(argv)
    try:
        lines, data = summary(args.fichero)
        print("\n".join(lines))
        if args.convertir:
            save_sparam_data(args.convertir, data, comments=[f"Convertido de {args.fichero.name} con CAL-ELEKTRA"])
            print(f"Escrito: {args.convertir}")
    except SDataError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
