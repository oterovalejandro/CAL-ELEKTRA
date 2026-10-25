"""Creates the high-level instrument object of an inventory equipment from its fields."""

from __future__ import annotations

from calelektra.catalog.fields import NumberRange
from calelektra.catalog.library import Library
from calelektra.catalog.model import Equipment
from calelektra.core.frequency import FrequencyRange
from calelektra.instruments.base import InstrumentError, VectorNetworkAnalyzer
from calelektra.instruments.dummy_vna import DummyVna
from calelektra.instruments.rs_znx import RsZnxVna

SUPPORTED_METHODS = ("OSM", "TOSM", "UOSM", "normalizacion")
DRIVERS = {
    "dummy": "VNA simulado de CAL-ELEKTRA",
    "rs_znx": "R&S ZNB/ZNx (comandos SCPI del driver RsZnx)",
}


def is_simulated(equipment: Equipment, library: Library) -> bool:
    model = library.model_of(equipment)
    driver = model.driver if model else None
    return equipment.simulated or driver in (None, "dummy")


def driver_description(equipment: Equipment, library: Library) -> str:
    model = library.model_of(equipment)
    driver = model.driver if model else None
    if is_simulated(equipment, library):
        real = f" (driver real: {DRIVERS.get(driver, driver)})" if driver not in (None, "dummy") else ""
        return f"Simulado{real}"
    return DRIVERS.get(driver or "", f"driver «{driver}» no integrado")


def open_vna(equipment: Equipment, library: Library, seed: int | None = None) -> VectorNetworkAnalyzer:
    """VNA object configured with the limits of the equipment fields (not connected yet)."""
    model = library.model_of(equipment)
    if model is None or model.category != "vna":
        raise InstrumentError(f"«{equipment.id}» no es un VNA")
    frequency_range = library.frequency_range_of(equipment).range or library.field(equipment, "rango_frecuencia")
    if not isinstance(frequency_range, FrequencyRange):
        raise InstrumentError(f"«{equipment.id}»: falta el campo «Rango de frecuencia»")
    power = library.field(equipment, "potencia_salida_dbm")
    ifbw = library.field(equipment, "ifbw")
    methods = [m for m in library.field(equipment, "metodos_calibracion", []) if m in SUPPORTED_METHODS]
    limits = dict(
        frequency_range=frequency_range,
        port_count=int(library.field(equipment, "num_puertos", 2)),
        power_range=(power.low, power.high) if isinstance(power, NumberRange) else None,
        if_bandwidth_range=(ifbw.start, ifbw.stop) if isinstance(ifbw, FrequencyRange) else None,
        max_points=library.field(equipment, "puntos_max"),
        calibration_methods=methods or SUPPORTED_METHODS,
    )
    name = f"{equipment.id} ({model.display_name})"
    if is_simulated(equipment, library):
        if limits["power_range"] is None:
            limits["power_range"] = (-40.0, 0.0)
        if limits["if_bandwidth_range"] is None:
            limits["if_bandwidth_range"] = (1.0, 10e6)
        if limits["max_points"] is None:
            limits["max_points"] = 100001
        return DummyVna(name=name, seed=seed, **limits)  # type: ignore[arg-type]
    if model.driver == "rs_znx":
        connection = equipment.connection
        resource = connection.resource if connection else None
        if not resource:
            raise InstrumentError(f"«{equipment.id}»: indique el recurso VISA en la conexión del equipo")
        timeout = connection.timeout_ms if connection and connection.timeout_ms else 10_000
        return RsZnxVna(name=name, resource=resource, timeout_ms=timeout, **limits)  # type: ignore[arg-type]
    raise InstrumentError(
        f"El driver «{model.driver}» de {model.display_name} todavía no está integrado. "
        f"Marque «simulado» en la conexión de «{equipment.id}» para usar el VNA simulado."
    )
