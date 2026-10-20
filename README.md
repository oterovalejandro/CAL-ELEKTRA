# CAL-ELEKTRA

Software para un **laboratorio de calibración de equipos de MC (CEM/EMC)**. Toma la arquitectura de R&S ELEKTRA —plantillas, *hardware setups* por rango de frecuencia, motor de secuencia, contenedor de resultados, gráficas e informes— y la adapta a la calibración de los equipos con los que se hacen los ensayos, no a los ensayos.

Ejemplos de lo que cubre:

- Cables y paths con VNA.
- Impedancia de LISN.
- Verificación de cámara semianecoica según CISPR 16-1-4: NSA con el método clásico y S_VSWR con el método de 6 posiciones.

## Estado

**Versión 0.4** — funcional con el VNA simulado:

- **Equipos definidos por categorías con campos tipados** (rango, puertos, conectores, potencia, IFBW, métodos de calibración, kit, longitud…). Los modelos dan los valores comunes y cada equipo los suyos; altas y ediciones con formularios generados a partir de esos campos.
- **Hardware setups** con alarma de rango, **requisitos sobre los campos**, comprobación de **conectores** en cada conexión y **diseño automático de la calibración** del VNA (TOSM si los planos se unen; si no, UOSM). El setup describe el banco de medida; el equipo a calibrar se elige en cada ensayo.
- **Plantillas de ensayo** (las *test templates* de ELEKTRA) con el **rango de frecuencias** (lineal por nº de puntos o por paso, logarítmico o lista CSV), **IFBW, promedios y suavizado** para todas las trazas o por traza, calibración, verificación, **ficheros S2P de especificación** para el **pasa / falla** y exportación.
- **Ensayos en carpetas**: se lanza una plantilla en la carpeta elegida, con un hardware setup compatible (visible como diagrama), el **cable a calibrar** y sus especificaciones. Cada ensayo guarda su definición, las copias de sus ficheros, los resultados (.s2p/.s1p, CSV), el **veredicto** con márgenes y la trazabilidad (equipos, certificados, datos S, kit, calibración).
- Setup sencillo de **calibración de cables solo con VNA**: calibra los dos puertos, verifica, mide S11, S21, S12 y S22 y exporta un .s2p. También impedancia de LISN por línea (.s1p).
- **VNA simulado** realista (errores sistemáticos, calibración OSM/TOSM/UOSM, CAL/CAI) y **adaptador del R&S ZNB43** sobre los comandos del driver RsZnx, con barrido segmentado para las listas (pendiente de probar con el equipo).

Todavía falta: incertidumbre, procedimientos de NSA y S_VSWR, mástiles, VNA de baja frecuencia real e informes. El detalle está en [`docs/04_algoritmo_medida_vna.md`](docs/04_algoritmo_medida_vna.md) y [`docs/05_plantillas_de_ensayo_y_ensayos.md`](docs/05_plantillas_de_ensayo_y_ensayos.md).

![Ensayo de un cable con pasa / falla frente a la máscara de especificación](docs/capturas/v04/23_ensayo_grafica.png)

## Puesta en marcha

Requisitos: Python 3.11 o superior.

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate      Linux/macOS: source .venv/bin/activate
pip install -e ".[dev]"                # añada ",instrumentos" para conectar VNA reales (pyvisa)
python -m calelektra                  # abre la biblioteca de ejemplo
python -m calelektra C:\ruta\biblioteca   # o una biblioteca propia
python -m pytest                      # pruebas (núcleo + GUI)
```

También se puede indicar la biblioteca con la variable de entorno `CALELEKTRA_BIBLIOTECA`.

## Estructura

```
src/calelektra/
  core/          unidades, rangos y cobertura, Touchstone, conectores, cálculo de la calibración
  catalog/       categorías con campos tipados, modelos, equipos y biblioteca YAML
  setups/        plantillas, setups configurados, requisitos, conexiones y su validación
  procedures/    plantillas de ensayo, frecuencias, especificaciones, motor y registro de ensayos
  instruments/   interfaz de alto nivel del VNA, VNA simulado, adaptador R&S ZNx, factoría
  gui/           interfaz gráfica (PySide6)
biblioteca_ejemplo/   catálogo, inventario, S2P sintéticos, setups, plantillas de ensayo, listas y especificaciones de ejemplo
docs/                 diseño paso a paso (01 ingeniería inversa … 05 plantillas de ensayo y ensayos)
tools/                generador de S2P sintéticos y capturas de pantalla
tests/                pruebas con pytest
```

## Documentación

1. [Ingeniería inversa de ELEKTRA y mapeo al laboratorio](docs/01_ingenieria_inversa_elektra.md)
2. [Catálogo de equipos y hardware setups (borrador del paso 2)](docs/02_catalogo_equipos_y_hardware_setups.md)
3. [Arquitectura del software y primer incremento](docs/03_arquitectura_y_primer_incremento.md)
4. [Campos de los equipos, algoritmo de medida con VNA y versión 0.3](docs/04_algoritmo_medida_vna.md)
5. [Plantillas de ensayo (frecuencias, trazas, especificaciones) y ensayos en carpetas — versión 0.4](docs/05_plantillas_de_ensayo_y_ensayos.md)
