# Kits de calibración: alta y definición de sus patrones — versiones 0.12 y 0.12.1

## 1. Qué cambia

- **El kit es un conjunto de patrones con identidad.**
  - En el modelo del kit, cada patrón lleva su tipo, su género, su definición y sus especificaciones eléctricas (k = 2).
  - En cada unidad del kit, cada patrón lleva su nº de serie, su estado y, si su certificado lo caracteriza, sus propios datos.
- **Tres maneras de definir un patrón**, como en METAS VNA Tools (manual §4.6):
  - importarlo de un fichero de kit de Keysight (`.ckt`);
  - escribirlo a mano con el modelo de Keysight («Agilent model»);
  - darlo por datos: un `.sdatb`, con su incertidumbre, o un Touchstone `.sNp`.
- **Vista:** cada patrón se ve en una **gráfica** y una **tabla** con su incertidumbre (k = 2).
- **Setups:** el análisis usa los patrones del kit (qué géneros tienen abierto, cortocircuito y carga en uso) y avisa de su estado (P9).

## 2. Modelo y unidad

Se sigue la regla de toda la biblioteca: **el modelo da lo común a todas las unidades** y **cada unidad lo suyo**.

| Dato del alta | Dónde va | Nota |
|---|---|---|
| Identificación | Unidad: identificador, referencia (LE) y denominación | Como cualquier equipo |
| Modelo, fabricante | Modelo | |
| Nº de serie | Unidad (del kit), y uno por patrón en la pestaña «Patrones» | |
| Familia de puertos | Modelo: «Familia de conector» | |
| Comentarios | Modelo: descripción; unidad: notas | |
| Zr real / Zr imag | Modelo: campos «Zr, parte real» y «Zr, parte imaginaria» (grupo «Patrones») | 50 Ω y 0 por defecto |
| Start / Stop | Modelo: «Rango de frecuencia (inicio – fin)» | Es el rango del kit, el mismo que limita los setups |
| Nº de puntos | Modelo: «Puntos de los patrones» | 1800 por defecto; solo para calcular, ver y exportar los patrones del modelo |
| Patrones y su definición nominal | Modelo: tarjeta «Patrones del kit» | La del fabricante (`.ckt`, hoja de datos) |
| Nº de serie, estado y datos de cada patrón | Unidad: pestaña «Patrones» | Los datos de su certificado sustituyen a los del modelo |

El modelo no está definido en DC. Por eso, un rango que empieza en DC empieza un paso por encima: DC – 18 GHz con 1800 puntos da 10 MHz, 20 MHz… 18 GHz.

## 3. Los patrones

### 3.1 Tipos

| Tipo | Puertos | Definición posible |
|---|---|---|
| Abierto | 1 | Modelo (C0–C3), datos o ninguna |
| Cortocircuito | 1 | Modelo (L0–L3), datos o ninguna |
| Carga | 1 | Modelo (línea offset terminada en una carga, por defecto de impedancia Z0), datos o ninguna |
| Through (delay/thru) | 2 | Modelo (la línea offset), datos o ninguna |
| Adaptador | 2 | Datos o ninguna: el through desconocido de la UOSM |

Cada patrón tiene:
- un identificador dentro del kit (`abierto_m`, `carga_h`, `through_hm`…);
- una etiqueta (como lo nombra el VNA o el `.ckt`, p. ej. `OPEN -M-`), una descripción y su género (dos en los de 2 puertos);
- opcionalmente, un rango propio.

### 3.2 Por el modelo de Keysight («Agilent model»)

Los campos de VNA Tools, en sus unidades:

| Campo | Unidad |
|---|---|
| Offset Z0 | Ω |
| Offset Delay | ps |
| Offset Loss | GΩ/s |
| C0, C1, C2, C3 (abierto) | 10⁻¹⁵ F, 10⁻²⁷ F/Hz, 10⁻³⁶ F/Hz², 10⁻⁴⁵ F/Hz³ |
| L0, L1, L2, L3 (cortocircuito) | 10⁻¹² H, 10⁻²⁴ H/Hz, 10⁻³³ H/Hz², 10⁻⁴² H/Hz³ |
| Impedancia de la carga (carga) | R y X en Ω; vacía: la de Z0 |

- Las fórmulas están en [`10_algoritmos_de_incertidumbre.md`](10_algoritmos_de_incertidumbre.md) §7.
- Solo patrones coaxiales. Desde 0.12.1 no hay opción de guía de onda («Standard Media», «Cutoff Frequency», «Height / Width Ratio»): el laboratorio no calibra en guía. Un YAML con `medio: guia` da error.

### 3.3 Por datos

- **`.sdatb`:** los valores y la incertidumbre del fichero, con todo su presupuesto. No se le añaden especificaciones: el fichero ya trae su incertidumbre.
- **Touchstone `.sNp`:** los valores, más la incertidumbre de las especificaciones eléctricas del patrón (como hace VNA Tools con las «Electrical Specifications»).
- **Puertos que se usan:** p. ej. el puerto 2 de un `.s2p` para un patrón de 1 puerto. Vacío: todos.
- **Copia en la biblioteca:** el fichero se copia en `catalogo/patrones/<kit>/` con su SHA-256, su referencia (el certificado o el laboratorio) y su fecha.

### 3.4 Especificaciones eléctricas (k = 2)

Una tabla con una fila por frecuencia. Entre filas se interpola linealmente y, fuera de la tabla, vale la fila más cercana.

| Tipo | Columnas |
|---|---|
| Abierto, cortocircuito | Desviación de módulo (dB) y de fase (°) |
| Carga | Pérdidas de retorno (dB): −40 dB es una incertidumbre de 0,01 en la parte real y en la imaginaria de Γ |
| Through | Pérdidas de retorno de las reflexiones; desviación de módulo (dB) y de fase (°) de la transmisión |

Cómo se convierten en incertidumbre: [`10_algoritmos_de_incertidumbre.md`](10_algoritmos_de_incertidumbre.md) §7.2. Así se construyeron los ficheros del kit AA180-VNACKN que el laboratorio comprobó en VNA Tools.

Cada especificación es una sola contribución, común a todas las frecuencias: la desviación de un patrón respecto a su definición está totalmente correlacionada entre frecuencias. No cambia la incertidumbre de cada punto, pero sí la de lo que combina frecuencias, como un retardo de grupo o una media en una banda: ver §7.2.1 de [`10_algoritmos_de_incertidumbre.md`](10_algoritmos_de_incertidumbre.md).

## 4. Importar un `.ckt`

Botón **«Importar .ckt…»** de la tarjeta «Patrones del kit»:

- **Patrones:** se crean con su definición por el modelo (retardo, pérdidas, Z0, C0–C3 o L0–L3) y su rango.
- **Si el kit ya tiene patrones**, el programa pregunta qué hacer:
  - sustituirlos, conservando las especificaciones de los que tengan el mismo identificador;
  - o añadir los nuevos.
- **Campos vacíos del modelo:** se rellenan con el nombre del kit (modelo y «Nombre del kit en el VNA»), su descripción y la familia de conector.
- **El `.ckt`** se guarda en `catalogo/patrones/<modelo>/` (`fichero_kit` del modelo).

Límites:
- El formato no está documentado: se ha descifrado por inspección del `.ckt` del AA180-VNACKN (versión 5), que se lee entero (`kits/ckt.py`).
- Un patrón de otro tipo (carga deslizante, impedancia arbitraria, patrón con datos, línea TRL) da un error claro. Hará falta descifrarlo con su propio `.ckt`.
- Las tablas por frecuencia del `.ckt`, de significado desconocido, no se usan. Las especificaciones se escriben a mano, de la hoja de datos.

## 5. Las unidades del kit

Pestaña **«Patrones»** de la unidad (Inventario → editar el kit). Muestra la validez de la calibración del kit y los avisos de sus patrones.

En cada patrón se puede indicar:
- **Nº de serie:** da el nombre del patrón al estilo de VNA Tools, p. ej. `Open(m) 975` (sin nº de serie, el del modelo del kit).
- **Estado:** en uso, dañado o sustituido, con su fecha.
- **Definición propia:** p. ej. los datos de su certificado (`.sdatb` o `.sNp`). Sustituye a la del modelo.
- **Especificaciones propias:** si no se indican, valen las del modelo.

Una copia de la unidad (nuevo equipo a partir de otro) no hereda los números de serie.

## 6. Ver un patrón

El diálogo del patrón (doble clic en su fila) tiene, a la derecha, su vista calculada al momento:

- **Gráfica:**
  - unidades: magnitud lineal, magnitud en dB, fase, parte real o parte imaginaria;
  - la incertidumbre (k = 2) se dibuja como zona sombreada;
  - un through muestra S11, S21, S12 y S22.
- **Tabla:**
  - columnas: frecuencia, parámetro, Re, Im, |S| y ∠S, y la incertidumbre expandida de cada uno;
  - «Copiar tabla» la copia separada por tabuladores, con punto decimal.

## 7. Caducidad y comprobaciones (P9)

- **Calibración del kit:** como la de cualquier equipo, un setup da error si ha caducado y aviso si caduca en 30 días o menos. La pestaña «Patrones» de la unidad lo muestra.
- **Estado de los patrones**, en el análisis del setup y en la pestaña «Patrones»:

  | Situación | Gravedad |
  |---|---|
  | Patrón dañado (no se usa) | Aviso |
  | Patrón sustituido después del certificado del kit | Aviso: la calibración no lo cubre |
  | Patrón sin definición (salvo los adaptadores) | Aviso |
  | No se encuentra su fichero de datos | Error |
  | Datos propios anteriores al último certificado del kit | Aviso |

- **Géneros disponibles:** si el modelo define sus patrones, son los géneros con abierto, cortocircuito y carga en uso y definidos. Si no, el campo «Géneros de los estándares». Con un abierto macho dañado, un plano N hembra deja de poder calibrarse.

## 8. Exportar `.sdatb`

Botón **«Exportar .sdatb…»**, en el modelo y en la unidad: un `.sdatb` por patrón con definición, con su incertidumbre, para abrirlo en VNA Tools. Los nombres siguen su convención: `Open(m) 975.sdatb`, `Thru(f-m) AA180-VNACKN.sdatb`.

## 9. YAML

```yaml
# catalogo/modelos/kit_AA180-VNACKN.yaml (extracto)
campos:
  rango_frecuencia: [DC, 18 GHz]
  familia_conector: N
  zr_real_ohm: 50
  zr_imag_ohm: 0
  puntos_patrones: 1800
patrones:
  - id: abierto_m
    tipo: abierto
    etiqueta: OPEN -M-
    descripcion: Type N (50) male open
    generos: [macho]
    definicion:
      clase: modelo
      z0_offset_ohm: 50.0
      retardo_offset_ps: 57.993
      perdidas_offset_gohm_s: 0.93
      c: [89.939, 2536.7999, -264.9901, 13.4]
    especificaciones:
      - { frecuencia: 0 Hz, modulo_db: 0.0, fase_grados: 1.5 }
  - id: adaptador_hh
    tipo: adaptador
    generos: [hembra, hembra]
    definicion: { clase: ninguna }
fichero_kit: ../patrones/aa180_vnackn/AA180-VNACKN1.ckt
```

```yaml
# catalogo/equipos/KIT-N-02.yaml (extracto)
patrones:
  abierto_m: { numero_serie: "975" }
  carga_h:
    numero_serie: "18831"
    definicion:
      clase: datos
      fichero: ../patrones/KIT-N-02/Load(f) 18831.sdatb
      referencia: Certificado 2026-0412
      fecha: 2026-04-01
      sha256: 4b1d…
  cortocircuito_h: { numero_serie: "747", estado: sustituido, fecha_estado: 2026-06-01 }
```

## 10. Lo que queda para más adelante

- **P8, el kit del VNA es el de la biblioteca:** cargar en el VNA la definición de la biblioteca, o comprobar la que tiene.
- **Peticiones y registro con el patrón exacto:** p. ej. «Conecte el abierto hembra nº 975».
- **Calibre de los patrones.**
- **Otros tipos de patrón:** carga deslizante, líneas TRL.
- **Incertidumbre de la medida:** la de los patrones no entra en la incertidumbre de las medidas, que sigue siendo el algoritmo del laboratorio.

## 11. Código y pruebas

| Fichero | Contenido |
|---|---|
| `kits/model.py` | `KitStandard`, `StandardDefinition`, `SpecRow` y `UnitStandard`; nombres de VNA Tools (`standard_name`) |
| `kits/agilent.py` | Modelo de Keysight: línea offset, terminaciones, through |
| `kits/specs.py` | Especificaciones eléctricas → entradas del presupuesto |
| `kits/standards.py` | `ResolvedStandard` (modelo + unidad), `kit_frequencies`, `standard_data`, `table_rows` y `export_sdatb` |
| `kits/ckt.py` | Lectura de los `.ckt` de Keysight |
| `catalog/model.py` | `EquipmentModel.patrones`, `fichero_kit`; `Equipment.patrones` |
| `catalog/library.py` | `kit_settings`, `kit_standards`, `standard_data`, `copy_standard_file`, `export_standards`, `kit_genders` y `kit_issues` |
| `setups/connections.py`, `setups/analysis.py` | Géneros a partir de los patrones; avisos de los patrones del kit |
| `gui/kit_standards.py` | Tablas del modelo y de la unidad, diálogos del patrón, vista (gráfica y tabla) |
| `tests/test_kits.py`, `tests/test_gui_kits.py` | Con el `.ckt` del AA180-VNACKN (`tests/datos/kits/`) y la carga de VNA Tools |

![El modelo del kit con sus patrones](capturas/v12/38_kit_modelo_patrones.png)

| | |
|---|---|
| ![Abierto macho: fase con su incertidumbre](capturas/v12/38b_patron_abierto_fase.png) | ![Abierto macho: la tabla](capturas/v12/38c_patron_abierto_tabla.png) |
| ![Carga: parte real, ± 0,0268](capturas/v12/38d_patron_carga_parte_real.png) | ![La unidad del kit: sus patrones](capturas/v12/38f_kit_unidad_patrones.png) |
