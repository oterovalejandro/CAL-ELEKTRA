# Paso 6 — Depuración del control del VNA (sin interfaz gráfica)

El adaptador del ZNB43 (`instruments/rs_znx.py`) usa los comandos SCPI que envía el driver **RsZnx 4.25.0** de R&S, sacados de su código fuente, pero todavía no se ha probado con el equipo. Para comprobarlos uno a uno hay una herramienta sin interfaz gráfica: funciones sencillas que **escriben un parámetro, lo leen de vuelta y vacían la cola de errores** del VNA, y una consola para ir lanzándolas.

- Paquete: `src/calelektra/instruments/znx_debug/`.
- Consola: `python -m calelektra.instruments.znx_debug` (o `cal-elektra-vna` después de `pip install -e .`).
- Desde Python: clase `ZnxDebugger`.
- Sin equipo: un **ZNB43 simulado a nivel SCPI** (`SimulatedZnb`) que responde a los mismos comandos.

## 1. Qué hace cada función

Cada función:

1. Envía los comandos SCPI del driver para un parámetro o una acción. Los parámetros de una traza (formato, suavizado, max hold, datos) actúan sobre la **traza activa** del canal, así que la función la selecciona antes (`CALC1:PAR:SEL '<traza>'`).
2. **Lee el valor de vuelta** (`<comando>?`) y lo devuelve: así se ve si el VNA lo ha aceptado tal cual o lo ha ajustado. Por ejemplo, la IFBW de 1,2 kHz se queda en 1,5 kHz.
3. **Vacía la cola de errores** (`SYST:ERR?` hasta `0,"No error"`). Si el VNA ha devuelto errores, la función falla con `ScpiError` y los muestra. Si una consulta se queda sin respuesta (tiempo de espera agotado), se lee la cola para saber por qué.

Todos los intercambios se muestran en pantalla con su tiempo (`→ comando   ← respuesta   (ms)`), se guardan en el historial de la sesión y, con `--log`, en un fichero.

## 2. Puesta en marcha

```bash
# Sin equipo (ZNB43 simulado)
python -m calelektra.instruments.znx_debug --simulado

# ZNB43 por VISA (HiSLIP o VXI-11): pip install -e ".[instrumentos]" y un backend VISA (R&S VISA, NI-VISA o pyvisa-py)
python -m calelektra.instruments.znx_debug --visa TCPIP::192.168.0.50::hislip0::INSTR --log sesion.log

# ZNB43 por socket SCPI (puerto 5025), sin instalar VISA
python -m calelektra.instruments.znx_debug --ip 192.168.0.50 --log sesion.log
```

| Opción | Uso |
|---|---|
| `--simulado` / `--visa RECURSO` / `--ip DIRECCION` | Equipo con el que se trabaja (uno de los tres) |
| `--puerto` | Puerto del socket SCPI (5025) |
| `--canal` | Canal del VNA (1) |
| `--timeout` | Tiempo de espera de las respuestas, en ms (10 000). Los barridos largos usan un tiempo calculado a partir de `SENS1:SWE:TIME?` |
| `--eco on\|off\|todo` | Intercambios SCPI en pantalla. Con `on` no se muestran las lecturas de `SYST:ERR?` que devuelven la cola vacía |
| `--log FICHERO` | Guarda todos los intercambios con fecha y hora |
| `--script FICHERO` | Ejecuta los comandos del fichero (uno por línea, `#` para comentarios) y termina |
| `--comprobar [--exportar] [--informe FICHERO]` | Ejecuta la comprobación completa (§5) y termina |
| `--sin-errores` | No lee `SYST:ERR?` después de cada función |

Al empezar se identifica el equipo (`*IDN?`). En la consola y con `--script` se activa además `SYST:DISP:UPD ON`, para que la pantalla del VNA se siga actualizando en control remoto.

En la consola se escribe el nombre de la función y sus argumentos separados por espacios: `set_start 10MHz`, `set_trace_format CE_S21 PHAS`, `self_check export=on`. Las frecuencias se escriben con unidad (`10MHz`, `1,5GHz`). Con `*` en lugar del nombre de la traza, la función actúa sobre todas las trazas del canal. `ayuda` muestra la lista de funciones y `ayuda <función>` el detalle con sus comandos SCPI. También están `eco`, `historial [n]` y `salir`. La tecla Tab completa los nombres.

## 3. Ejemplo de sesión

La secuencia de [`tools/vna_secuencia_ejemplo.txt`](../tools/vna_secuencia_ejemplo.txt) recorre todos los parámetros que usa el software. Se ejecuta con `--script`. Extracto con el VNA simulado:

```text
vna> set_step 10MHz
    → SENS1:SWE:STEP 10000000   (0 ms)
    → SENS1:SWE:STEP?   ← 10000000   (0 ms)
Paso de frecuencia (barrido lineal): 10 MHz
vna> get_points
    → SENS1:SWE:POIN?   ← 1800   (0 ms)
Número de puntos: 1800
vna> set_frequency_list biblioteca_ejemplo/hardware_setups/listas/puntos_cable_N.csv
    → SOUR1:POW?   ← -10   (0 ms)
    → SENS1:BAND?   ← 10000   (0 ms)
    → SENS1:SEGM:DEL:ALL   (0 ms)
    → SENS1:SEGM1:INS 30000000,1000000000,98,-10,0,0,10000,AUTO,NORM,AUTO,STEP;*OPC?   ← 1   (0 ms)
    → SENS1:SEGM2:INS 1100000000,18000000000,170,-10,0,0,10000,AUTO,NORM,AUTO,STEP;*OPC?   ← 1   (0 ms)
    → SENS1:SEGM:BWID:CONT OFF   (0 ms)
    → SENS1:SEGM:POW:CONT OFF   (0 ms)
    → SENS1:SWE:TYPE SEGM   (0 ms)
    …
segmento  inicio   final   puntos
1         30 MHz   1 GHz   98
2         1,1 GHz  18 GHz  170
vna> set_max_hold CE_S21 on
    → CALC1:PAR:SEL 'CE_S21'   (0 ms)
    → CALC1:PHOL MAX   (0 ms)
    → CALC1:PHOL?   ← MAX   (0 ms)
Retención (max hold / min hold): MAX (Retención de máximos (max hold))
vna> restart_sweep
    → INIT1:CONT?   ← 0   (0 ms)
    → SENS1:AVER:CLE   (0 ms)
    → SENS1:SWE:TIME?   ← 0.509265   (0 ms)
    → SENS1:SWE:COUN?   ← 10   (0 ms)
    → INIT1;*OPC?   ← 1   (3 ms)
    → CALC1:DATA:NSW:COUN?   ← 10   (0 ms)
barrido único: SENS:AVER:CLE + INIT, 10 barridos completados
vna> export_and_download cable_vna.s2p LOGP
    → MMEM:STOR:TRAC:PORT 1,'cable_vna.s2p',LOGP,CIMP,1,2;*OPC?   ← 1   (13 ms)
    → MMEM:DATA? 'cable_vna.s2p'   ← <bloque binario de 38977 bytes>   (0 ms)
fichero …/cable_vna.s2p
  │ # HZ S DB R 50
  │ 10000000 -64.390803 -13.507550 -0.044312 -10.299173 …
vna> set_power 30
    → SOUR1:POW 30   (0 ms)
    → SOUR1:POW?   ← -10   (0 ms)
    → SYST:ERR?   ← -222,"Data out of range;SOUR1:POW 30"   (0 ms)
✗ el VNA ha devuelto error en «set_power»:
    -222,"Data out of range;SOUR1:POW 30"
```

## 4. Funciones y comandos SCPI

Los parámetros tienen una función `set_…`, que escribe y devuelve el valor leído, y otra `get_…`, que solo lee. `<c>` es el canal y `<t>` la traza.

| Parámetro | Funciones | Comando SCPI | Valores |
|---|---|---|---|
| Escala de frecuencia | `set_sweep_type`, `get_sweep_type` | `SENS<c>:SWE:TYPE` | `LIN` (lineal), `LOG` (logarítmico), `SEGM` (lista); también `POW`, `CW`, `POIN` |
| Frecuencia de inicio / final | `set_start`, `set_stop` | `SENS<c>:FREQ:STAR`, `SENS<c>:FREQ:STOP` | `10MHz`, `18GHz`… |
| Centro / span | `set_center`, `set_span` | `SENS<c>:FREQ:CENT`, `SENS<c>:FREQ:SPAN` | |
| Número de puntos | `set_points` | `SENS<c>:SWE:POIN` | 1 – 100 001 |
| Paso | `set_step` | `SENS<c>:SWE:STEP` | Solo en barrido lineal: el VNA recalcula los puntos (y la frecuencia final) |
| Lista de frecuencias | `set_frequency_list`, `get_segments` | `SENS<c>:SEGM:DEL:ALL`, `SENS<c>:SEGM<n>:INS …;*OPC?`, `SENS<c>:SEGM:BWID:CONT OFF`, `SENS<c>:SEGM:POW:CONT OFF`, `SENS<c>:SWE:TYPE SEGM` | Fichero CSV (como en los setups) o `1GHz;1,5GHz;2GHz`. Un segmento por tramo de paso constante, como en el software |
| Frecuencias barridas | `get_frequencies` | `CALC<c>:DATA:STIM?` | |
| Potencia | `set_power` | `SOUR<c>:POW` | dBm |
| Salida RF | `set_rf_output` | `OUTP<c>` | on / off |
| IFBW | `set_if_bandwidth` | `SENS<c>:BAND` | El VNA redondea a 1 – 1,5 – 2 – 3 – 5 – 7 × 10ⁿ Hz |
| Selectividad de FI | `set_if_selectivity` | `SENS<c>:BAND:SEL` | `NORM`, `MED`, `HIGH` |
| Promediado | `set_averaging`, `set_average_count`, `set_average_mode` | `SENS<c>:AVER`, `SENS<c>:AVER:COUN`, `SENS<c>:AVER:MODE` | on / off; 1 – 1000; `AUTO`, `FLAT`, `RED`, `MOV` |
| Barridos por INIT | `set_sweep_count` | `SENS<c>:SWE:COUN` | En modo único, igual al número de promedios para que un INIT complete el promedio |
| Barrido continuo | `set_continuous` | `INIT<c>:CONT` | on / off |
| Tiempo de barrido | `get_sweep_time` | `SENS<c>:SWE:TIME?` | Solo lectura |
| Formato de la traza | `set_trace_format <t> <formato>` | `CALC<c>:PAR:SEL '<t>'`, `CALC<c>:FORM` | `MLOG` (dB), `MLIN`, `PHAS`, `UPH`, `SWR`, `REAL`, `IMAG`, `GDEL`, `SMIT`, `POL`, `ISM` |
| Suavizado | `set_smoothing <t> on`, `set_smoothing_aperture <t> <%>` | `CALC<c>:SMO`, `CALC<c>:SMO:APER` | Apertura 0,05 – 100 % |
| Max hold / min hold | `set_max_hold <t> on`, `set_hold <t> MAX\|MIN\|OFF`, `restart_hold <t>` | `CALC<c>:PHOL` | El reinicio es `PHOL OFF` y vuelta al modo anterior |
| Pantalla en remoto | `set_display_update` | `SYST:DISP:UPD` | on / off |
| Formato de transferencia | `set_data_format`, `get_data_format` | `FORM`, `FORM:BORD` | `ASC`, `REAL,32`, `REAL,64` (binario en orden `SWAP`) |
| Versión Touchstone | `set_touchstone_version` | `MMEM:STOR:TRAC:OPT:FORM` | `F11`, `F20`, `L11` |

| Acción | Función | Comandos SCPI |
|---|---|---|
| **Reiniciar el barrido tras conectar los cables** (el promediado empieza de cero) | `restart_sweep` | Continuo: `ABOR`, `SENS<c>:AVER:CLE`. Único: `SENS<c>:AVER:CLE`, `INIT<c>;*OPC?` (espera a todos los barridos) y `CALC<c>:DATA:NSW:COUN?` |
| Reiniciar solo el promediado | `restart_averaging` | `SENS<c>:AVER:CLE` |
| Barrido único | `single_sweep` | `INIT<c>;*OPC?`, `CALC<c>:DATA:NSW:COUN?` |
| Abortar | `abort` | `ABOR` |
| Las cuatro trazas S11, S21, S12, S22 | `setup_s_parameters [formato]` | `CALC<c>:PAR:SDEF 'CE_S21','S21'`, `DISP:WIND<n>:STAT ON`, `DISP:WIND<n>:TRAC:EFE 'CE_S21'`, `CALC<c>:PAR:SEL`, `CALC<c>:FORM` |
| Trazas | `list_traces`, `create_trace`, `delete_trace`, `select_trace`, `show_trace`, `autoscale` | `CALC<c>:PAR:CAT?`, `…:PAR:SDEF`, `…:PAR:DEL`, `…:PAR:SEL`, `DISP:WIND<n>:TRAC:EFE`, `DISP:TRAC:Y:AUTO ONCE` |
| Parámetro que mide una traza | `set_trace_parameter <t> S11` | `CALC<c>:PAR:MEAS '<t>','S11'` |
| Datos de una traza | `read_trace <t> SDAT\|FDAT\|MDAT` | `CALC<c>:PAR:SEL '<t>'`, `CALC<c>:DATA? SDAT` |
| S2P como lo exporta el software | `read_s_parameters`, `save_s2p <fichero> RI\|MA\|DB` | `CALC<c>:DATA:STIM?` y `CALC<c>:DATA? SDAT` de cada traza (sin suavizar) |
| **S2P exportado por el VNA** | `export_touchstone [fichero] COMP\|LINP\|LOGP [1,2]`, `download_file`, `export_and_download` | `MMEM:STOR:TRAC:PORT <c>,'<fichero>',COMP\|LINP\|LOGP,CIMP,1,2;*OPC?` (COMP = RI, LINP = MA, LOGP = DB), `MMEM:DATA? '<fichero>'` |
| Errores y estado | `errors`, `clear_status`, `status`, `identify`, `reset`, `wait` | `SYST:ERR?`, `*CLS`, `*IDN?`, `*RST;*OPC?`, `*OPC?` |
| Cualquier otro comando | `scpi <comando>` | El comando tal cual; si lleva `?` se muestra la respuesta |

`status` resume la configuración del canal y de cada traza. `save_log <fichero>` guarda los intercambios de la sesión.

### Desde Python

```python
from calelektra.instruments.znx_debug import ZnxDebugger

vna = ZnxDebugger.open_socket("192.168.0.50", listener=print)   # o open_visa(recurso), simulated()
vna.set_sweep_type("LIN"); vna.set_start("10 MHz"); vna.set_stop("18 GHz"); vna.set_points(801)
vna.setup_s_parameters("MLOG")
vna.set_smoothing("CE_S21", True)
vna.restart_sweep()
s21 = vna.read_trace("CE_S21", "SDAT")      # numpy, complejos
vna.save_s2p("cable.s2p", "RI")
```

## 5. Comprobación completa (`self_check`, `--comprobar`)

```bash
python -m calelektra.instruments.znx_debug --ip 192.168.0.50 --comprobar --informe informe_vna.txt
python -m calelektra.instruments.znx_debug --ip 192.168.0.50 --comprobar --exportar --informe informe_vna.txt   # con calibración de 2 puertos
```

La comprobación hace, en este orden:

1. Identificación y estado de la cola de errores al empezar.
2. Escribe y relee cada parámetro: escala lineal y logarítmica, inicio, final, centro y span (y los valores que resultan), puntos, paso (y los puntos que salen), potencia, salida RF, IFBW (con un valor intermedio para ver el redondeo), selectividad, promediado y sus cuatro modos, barridos por INIT, barrido continuo, formatos de transferencia y versión Touchstone.
3. Crea cuatro trazas de prueba `CE_CHK_S11…S22`, las selecciona y cambia el parámetro medido; prueba los once formatos, el suavizado con su apertura, el max hold y el min hold, y el reinicio del hold.
4. Hace barridos y lee datos:
   - comprueba que las frecuencias son las pedidas;
   - comprueba que los datos en ASCII y en binario de 32 y 64 bits son iguales;
   - comprueba que **el suavizado no cambia los datos SDAT**, que son los que exporta el software, y mide cuánto cambia FDAT;
   - comprueba que **el max hold retiene máximos** y que, tras reiniciarlo, FDAT vuelve a ser el barrido actual;
   - prueba el **reinicio del barrido** en modo único, donde comprueba que se completan los barridos del promedio, y en modo continuo.
5. Prueba la lista de frecuencias: segmentos leídos de vuelta y frecuencias barridas. Se omite si el canal ya tenía una tabla de segmentos, para no borrarla.
6. Con `export=on` / `--exportar`, prueba la exportación Touchstone del VNA en COMP, LINP y LOGP. Descarga cada fichero, comprueba la línea de opciones (`# HZ S RI|MA|DB R 50`) y compara sus datos con SDAT.
7. Borra las trazas de prueba y **restaura la configuración inicial** del canal.

Resultado de cada paso:

| Resultado | Significado |
|---|---|
| `OK` | El VNA ha aceptado el comando y el valor leído es el esperado |
| `AJUSTADO` | El VNA ha aceptado el comando sin error pero ha guardado otro valor (redondeo, límites) |
| `FALLO` | El VNA ha devuelto un error o el resultado no es el esperado |
| `INFO` | Medida informativa, por ejemplo cuánto cambia FDAT al suavizar |
| `OMITIDO` | Paso no ejecutado |

Con `--informe`, el fichero guarda el informe y todos los intercambios SCPI. Es lo que hay que revisar, o enviar, para corregir el adaptador. Con el ZNB simulado y `--exportar` salen 97 comprobaciones: 95 OK, 1 ajustada (la IFBW de 1,2 kHz) y 1 informativa. El programa termina con código 1 si hay algún `FALLO`.

## 6. ZNB43 simulado

`SimulatedZnb` guarda el estado de los canales, las trazas y los diagramas. Interpreta los comandos en forma corta o larga (`SENSe1:FREQuency:STARt 1GHz`), con rutas relativas (`SENS1:FREQ:STAR 1e9;STOP 2e9`), nodos opcionales y unidades, y llena la cola de errores con los códigos SCPI (`-113` cabecera desconocida, `-222` fuera de rango, `-221` conflicto, `-213` INIT ignorado, `-410` consulta interrumpida…). Una consulta que falla no recibe respuesta, como en el equipo, y el cliente ve un tiempo de espera agotado.

El DUT simulado es un cable coaxial de unos 60 cm entre los puertos 1 y 2. El ruido depende de la IFBW y de la potencia, así que el promediado, el suavizado y el max hold tienen un efecto visible. Los datos SDAT incluyen el promediado pero no el suavizado ni la retención, que solo cambian FDAT.

También sirve de transporte para el adaptador `RsZnxVna`: un test ejecuta con él el preset, el barrido, los promedios, el barrido segmentado y la lectura de los parámetros S, sin ningún error de la cola.

El simulador reproduce lo que se espera del equipo, **no el firmware del ZNB**. Lo que acepte o rechace el ZNB43 real manda.

## 7. Qué hay que confirmar en el ZNB43

| Punto | Cómo verlo |
|---|---|
| Todos los comandos de §4 (salen del driver, no del equipo) | `--comprobar --informe`; cualquier `FALLO` muestra el error del VNA |
| `restart_sweep` en modo continuo (`ABOR` + `SENS1:AVER:CLE`): ¿el contador de promedios de la pantalla vuelve a empezar? | En la consola, con la pantalla del VNA a la vista |
| El suavizado y el max hold no cambian SDAT | Pasos «SDAT no cambia con el suavizado» y «Max hold» de la comprobación |
| `MMEM:STOR:TRAC:PORT` necesita una calibración completa de los puertos exportados, según la documentación del driver | `export_and_download` con y sin calibración |
| `DISP:WIND<n>:TRAC:EFE` sobre una traza que ya está en ese diagrama | `setup_s_parameters` dos veces |
| Redondeo de la IFBW, ajuste de puntos y frecuencia final con el paso, límites de potencia del ZNB43 | Resultados `AJUSTADO` de la comprobación |
| Tiempos: `INIT1;*OPC?` con muchos promedios y la descarga de ficheros | Tiempos en ms de cada intercambio; `--timeout` si hace falta |
| Significado de `CALC1:DATA:NSW:COUN?` (barridos completados en modo único) | `restart_sweep` en modo único con promedios |

Una nota del driver: su tabla `ExportFormat` asigna `Unformatted` a `"FORM"` y `Formatted` a `"UNF"`, que parece al revés. Solo afecta a la exportación CSV (`MMEM:STOR:TRAC`), que ni el software ni esta herramienta usan.

## 8. Ficheros

| Fichero | Contenido |
|---|---|
| `instruments/znx_debug/settings.py` | Tabla de parámetros: comando SCPI, tipo, opciones y textos |
| `instruments/znx_debug/debugger.py` | `ZnxDebugger`: funciones `set_…`/`get_…`, trazas, barridos, datos, exportación, `status` |
| `instruments/znx_debug/checks.py` | Comprobación completa e informe |
| `instruments/znx_debug/simulator.py` | ZNB43 simulado a nivel SCPI |
| `instruments/znx_debug/console.py` | Consola, `--script`, `--comprobar` |
| `instruments/rs_znx.py` | Transporte por socket (`SocketTransport`) y bloques binarios (`query_block`, para `MMEM:DATA?`), además del transporte VISA |
| `tools/vna_secuencia_ejemplo.txt` | Secuencia de ejemplo para `--script` |
| `tests/test_znx_debug.py` | Pruebas con el simulador, por socket TCP, de la consola y del adaptador `RsZnxVna` |
