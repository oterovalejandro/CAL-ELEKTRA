"""Creates the high-level instrument object for an inventory equipment."""

from __future__ import annotations

from calelektra.catalog.library import Library
from calelektra.catalog.model import Equipment
from calelektra.instruments.base import InstrumentError, VectorNetworkAnalyzer
from calelektra.instruments.dummy_vna import DummyVna


def is_simulated(equipment: Equipment, library: Library) -> bool:
    model = library.model_of(equipment)
    driver = (model.communication or {}).get("driver") if model else None
    return bool(equipment.connection and equipment.connection.simulated) or driver == "dummy"


def open_vna(equipment: Equipment, library: Library) -> VectorNetworkAnalyzer:
    model = library.model_of(equipment)
    if model is None or model.category != "vna":
        raise InstrumentError(f"«{equipment.id}» no es un VNA")
    if is_simulated(equipment, library):
        frequency_range = library.frequency_range_of(equipment).range or model.nominal_range()
        if frequency_range is None:
            raise InstrumentError(f"«{equipment.id}»: el modelo no declara rango de frecuencia")
        power = model.parameters.get("potencia_salida_dbm", [-40, 0])
        return DummyVna(
            name=f"{equipment.id} ({model.display_name})",
            frequency_range=frequency_range,
            port_count=int(model.parameters.get("num_puertos", 2)),
            power_range=(float(power[0]), float(power[1])),
        )
    driver = (model.communication or {}).get("driver", "sin driver")
    raise InstrumentError(
        f"El driver «{driver}» de {model.display_name} todavía no está integrado. "
        f"Marque «simulado: true» en la conexión de «{equipment.id}» para usar el VNA dummy."
    )
