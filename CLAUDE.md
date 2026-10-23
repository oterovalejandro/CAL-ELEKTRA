# CAL-ELEKTRA — notas para desarrollo

- Python 3.11+, PySide6 (GUI), pyqtgraph (gráficas), pydantic v2 + PyYAML (formatos), numpy.
- Idiomas:
  - Identificadores de código en **inglés**.
  - Textos de la GUI y mensajes de validación en **español**.
  - Claves de los YAML en **español sin tildes**: son el formato de datos del laboratorio y se mapean con `Field(alias=...)`.
- La biblioteca es una carpeta de YAML. Cada fichero lleva `formato: calelektra/<tipo>@0.3` (salvo los ficheros de incertidumbres de los VNA, que son del laboratorio). El ejemplo está en `biblioteca_ejemplo/` y lo usan los tests (siempre sobre una copia: los ensayos escriben en `ensayos/`). La E5061B (`VNA-BF-01`) tiene la calibración vencida (30/07/2026): los tests de la LISN usan `E5061B_VALID_DAY` o `renew_calibration` de `conftest.py`.
- Regla central: el rango efectivo de un setup es la intersección de los rangos útiles de los equipos (`setups/analysis.py`, `core/frequency.py`). El DUT no limita.
- Los equipos se describen con los **campos tipados de su categoría** (`catalog/fields.py`); el código lee valores con `library.field(equipo, "clave")` y nunca con claves sueltas del YAML. Setups (`setups/requirements.py`, `setups/connections.py`) y ensayos (`procedures/engine.py`) usan esos campos.
- Estructura de un ensayo: el **hardware setup** da el banco y las frecuencias (`setups/frequencies.py`: lineal por puntos o paso, logarítmico, lista CSV); el **modelo del EUT** da las especificaciones S2P (`especificaciones_s`, `procedures/specs.py`); la **plantilla de ensayo** (`procedures/model.py`) llama a un setup (`hardware_setup`), define el EUT (`eut`) y los ajustes de trazas (el suavizado solo afecta a la visualización); un **ensayo** es el registro de un lanzamiento: `ProcedureRunner(library, plantilla, folder=…)` crea `<carpeta>/<ensayo>/ensayo.yaml` con `library.new_test_record`.
- Los instrumentos solo se usan a través de `instruments/base.py`. Hay un VNA simulado (`dummy_vna.py`) y el adaptador R&S ZNx (`rs_znx.py`, comandos SCPI del driver RsZnx 4.25.0, sin probar aún con el equipo). `instruments/factory.py` elige uno u otro según los campos y la conexión del equipo.
- Incertidumbre (`uncertainty/`): `engine.py` es el algoritmo del laboratorio (mismos resultados que su script; no cambiar el cálculo sin comprobarlo contra él) y `traces.py` lo aplica punto a punto. El fichero YAML de incertidumbres es de cada **unidad** de VNA (`incertidumbre` del equipo, `catalogo/incertidumbres/`, claves en inglés del laboratorio). El paso «incertidumbre» del motor la guarda en el registro y en `<resultado>_incertidumbre.csv`; `SParameterPlot.set_uncertainty` la sombrea.
- Depuración del ZNx sin GUI (`instruments/znx_debug/`): tabla de parámetros con su comando SCPI (`settings.py`), `ZnxDebugger` (cada `set_…` escribe, relee y vacía `SYST:ERR?`), comprobación completa (`checks.py`), consola (`console.py`) y un ZNB simulado a nivel SCPI (`simulator.py`) que también sirve de transporte para `RsZnxVna` en los tests. Es la única pieza que habla SCPI fuera del adaptador.

## Comandos

```bash
python -m venv .venv && . .venv/bin/activate && pip install -e ".[dev]"   # ",instrumentos" añade pyvisa
python -m pytest                                   # núcleo + GUI (offscreen)
QT_QPA_PLATFORM=offscreen python tools/capturas.py capturas   # capturas PNG de la GUI
python tools/generar_s2p_sinteticos.py             # regenera los S2P sintéticos de ejemplo
python -m calelektra.instruments.znx_debug --simulado   # consola de depuración del VNA (--ip/--visa con el equipo)
python -m calelektra.uncertainty --yaml <fichero> --equipment ZNB43 --parameter S21 --frequency-hz 1e9 --value -0.5 --unit dB
```

En contenedores Linux sin pantalla, Qt necesita `libegl1`, `libgl1`, `libxkbcommon0` y `libfontconfig1`.
