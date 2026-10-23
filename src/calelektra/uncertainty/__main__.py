"""Command line of the uncertainty engine (same options as the laboratory script).

    python -m calelektra.uncertainty --yaml VNA_ZNB43_101683_uncertainties_calculation.yaml --equipment ZNB43 \
        --parameter S21 --frequency-hz 1e9 --value -0.5 --unit dB --if-bandwidth-hz 1000
"""

from __future__ import annotations

import argparse
import json

from calelektra.uncertainty.engine import calculate_expanded_uncertainty


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Incertidumbre expandida a partir del fichero de incertidumbres de un VNA.")
    parser.add_argument("--yaml", dest="yaml_path", required=True, help="Fichero YAML de incertidumbres.")
    parser.add_argument("--equipment", required=True, help="Equipo o modelo, p. ej. E5061B.")
    parser.add_argument("--parameter", required=True, help="S11, S22, S21, S12 o frequency.")
    parser.add_argument("--frequency-hz", type=float, required=True, help="Frecuencia en Hz.")
    parser.add_argument("--value", type=float, required=True, help="Valor medido.")
    parser.add_argument("--unit", required=True, help="Unidad del valor: dB, degree/deg, linear/ratio o Hz (no VSWR).")
    parser.add_argument("--output-unit", default=None, help="Unidad de la incertidumbre: dB, degree/deg, linear/ratio o Hz.")
    parser.add_argument("--level-value", type=float, default=None, help="Nivel que selecciona los rangos (si se omite, --value).")
    parser.add_argument("--level-unit", default=None, help="Unidad de --level-value: dB o linear/ratio (si se omite, --unit).")
    parser.add_argument("--if-bandwidth-hz", type=float, default=None, help="IFBW usada, en Hz.")
    parser.add_argument("--temp-min-c", type=float, default=None, help="Temperatura mínima en °C.")
    parser.add_argument("--temp-max-c", type=float, default=None, help="Temperatura máxima en °C.")
    parser.add_argument("--k", type=float, default=None, help="Factor de cobertura final (2 por defecto).")
    parser.add_argument("--no-trace-noise", action="store_true", help="Sin la contribución del ruido de traza.")
    parser.add_argument("--no-temperature", action="store_true", help="Sin la estabilidad con la temperatura.")
    return parser


def main() -> None:
    args = build_arg_parser().parse_args()
    result = calculate_expanded_uncertainty(
        yaml_path=args.yaml_path,
        equipment=args.equipment,
        parameter=args.parameter,
        frequency_hz=args.frequency_hz,
        value=args.value,
        unit=args.unit,
        temp_min_c=args.temp_min_c,
        temp_max_c=args.temp_max_c,
        coverage_factor=args.k,
        include_trace_noise=not args.no_trace_noise,
        include_temperature_stability=not args.no_temperature,
        if_bandwidth_hz=args.if_bandwidth_hz,
        output_unit=args.output_unit,
        level_value=args.level_value,
        level_unit=args.level_unit,
    )
    print(json.dumps(result, indent=2, ensure_ascii=False, allow_nan=True))


if __name__ == "__main__":
    main()
