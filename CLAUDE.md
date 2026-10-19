# CAL-ELEKTRA — notas para desarrollo

- Python 3.11+, PySide6 (GUI), pyqtgraph (gráficas), pydantic v2 + PyYAML (formatos), numpy.
- Idiomas:
  - Identificadores de código en **inglés**.
  - Textos de la GUI y mensajes de validación en **español**.
  - Claves de los YAML en **español sin tildes**: son el formato de datos del laboratorio y se mapean con `Field(alias=...)`.
- La biblioteca es una carpeta de YAML. Cada fichero lleva `formato: calelektra/<tipo>@0.2`. El ejemplo está en `biblioteca_ejemplo/` y lo usan los tests.
- Regla central: el rango efectivo de un setup es la intersección de los rangos útiles de los equipos (`setups/analysis.py`, `core/frequency.py`). El DUT no limita.
- Los instrumentos solo se usan a través de `instruments/base.py`. Los drivers reales serán adaptadores de esa interfaz. De momento solo existe el VNA dummy.

## Comandos

```bash
python -m venv .venv && . .venv/bin/activate && pip install -e ".[dev]"
python -m pytest                                   # núcleo + GUI (offscreen)
QT_QPA_PLATFORM=offscreen python tools/capturas.py capturas   # capturas PNG de la GUI
python tools/generar_s2p_sinteticos.py             # regenera los S2P sintéticos de ejemplo
```

En contenedores Linux sin pantalla, Qt necesita `libegl1`, `libgl1`, `libxkbcommon0` y `libfontconfig1`.
