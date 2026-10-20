# CAL-ELEKTRA

Software para un **laboratorio de calibración de equipos de MC (CEM/EMC)**. Toma la arquitectura de R&S ELEKTRA —plantillas, *hardware setups* por rango de frecuencia, motor de secuencia, contenedor de resultados, gráficas e informes— y la adapta a la calibración de los equipos con los que se hacen los ensayos, no a los ensayos.

Ejemplos de lo que cubre:

- Cables y paths con VNA.
- Impedancia de LISN.
- Verificación de cámara semianecoica según CISPR 16-1-4: NSA con el método clásico y S_VSWR con el método de 6 posiciones.

## Estado

**Versión 0.3** — funcional con el VNA simulado:

- **Equipos definidos por categorías con campos tipados** (rango, puertos, conectores, potencia, IFBW, métodos de calibración, kit, longitud…). Los modelos dan los valores comunes y cada equipo los suyos; altas y ediciones con formularios generados a partir de esos campos.
- **Hardware setups** con alarma de rango, **requisitos sobre los campos**, comprobación de **conectores** en cada conexión y **diseño automático de la calibración** del VNA (TOSM si los planos se unen; si no, UOSM).
- **Setup sencillo de calibración de cables solo con VNA** y procedimiento que calibra los dos puertos, verifica, mide S11, S21, S12 y S22 y **exporta un .s2p** en el rango del setup. También impedancia de LISN por línea (.s1p).
- **Ensayos** con flujo de pasos, peticiones al operador, gráfica en vivo y registro con la trazabilidad (equipos, certificados, datos S, kit, calibración).
- **VNA simulado** realista (errores sistemáticos, calibración OSM/TOSM/UOSM, CAL/CAI) y **adaptador del R&S ZNB43** sobre los comandos del driver RsZnx (pendiente de probar con el equipo).

Todavía falta: incertidumbre, procedimientos de NSA y S_VSWR, mástiles, VNA de baja frecuencia real e informes. El detalle está en [`docs/04_algoritmo_medida_vna.md`](docs/04_algoritmo_medida_vna.md).

![Ensayo de un cable con el VNA simulado](docs/capturas/v03/22_ensayo_grafica.png)

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
  procedures/    procedimientos de medida, motor de ensayos y registro de ensayos
  instruments/   interfaz de alto nivel del VNA, VNA simulado, adaptador R&S ZNx, factoría
  gui/           interfaz gráfica (PySide6)
biblioteca_ejemplo/   catálogo, inventario, S2P sintéticos, plantillas, setups y procedimientos de ejemplo
docs/                 diseño paso a paso (01 ingeniería inversa … 04 algoritmo de medida)
tools/                generador de S2P sintéticos y capturas de pantalla
tests/                pruebas con pytest
```

## Documentación

1. [Ingeniería inversa de ELEKTRA y mapeo al laboratorio](docs/01_ingenieria_inversa_elektra.md)
2. [Catálogo de equipos y hardware setups (borrador del paso 2)](docs/02_catalogo_equipos_y_hardware_setups.md)
3. [Arquitectura del software y primer incremento](docs/03_arquitectura_y_primer_incremento.md)
4. [Campos de los equipos, algoritmo de medida con VNA y versión 0.3](docs/04_algoritmo_medida_vna.md)
