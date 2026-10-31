# Parámetros S con incertidumbre: los ficheros de METAS VNA Tools — versión 0.11

## 1. Para qué

Es el primer paso de los kits de calibración. En VNA Tools, un patrón se puede definir de dos maneras (manual de usuario V3.0.2, §4.6):

- por su modelo (offset, C0…C3, L0…L3);
- **con datos** (*databased standard*, §4.6.2): un fichero `.sdatb` con el valor y el **presupuesto de incertidumbre** de cada frecuencia y parámetro. Así entregan sus datos los institutos nacionales de metrología y los laboratorios acreditados.

CAL-ELEKTRA lee y escribe esos ficheros con un **motor propio**: Python con numpy, sin el software de METAS. Está escrito a partir de los documentos de formatos que publica METAS (§8).

## 2. Formatos

| Formato | Qué guarda | Lectura | Escritura |
|---|---|---|---|
| `.sdatb` (versiones 1 a 5) | Todo: valores y presupuesto completo. Cada entrada lleva su identificador, descripción y distribución, con su sensibilidad. Conserva la correlación entre partes real e imaginaria, entre parámetros, entre frecuencias y **entre ficheros** | Sí | Sí: la primera versión que guarda los datos, sin comprimir; la versión 1, comprimida, si se pide |
| `.sdatcv` | Valores y matriz de covarianza de cada frecuencia, sin correlación entre frecuencias ni entre ficheros | Sí | Sí, con la matriz completa |
| Touchstone `.sNp`, `.ts` | Solo los valores (Zr real e igual en todos los puertos) | Sí | `.sNp` |
| `.sdatx` (XML) | Lo mismo que `.sdatb` | No (todavía) | No |

## 3. Qué hay dentro de un `.sdatb`

Data Formats V3.0.2, §2. Orden de bytes *little-endian*, con los tipos de .NET: enteros de 32 y 16 bits, `double`, «enteros de 7 bits» de longitud variable y textos con su longitud delante, en UTF-8.

1. Cabecera `%SDATA`, versión, número de frecuencias y de puertos, y las frecuencias en Hz.
2. Puertos: su número y, desde la versión 3, su modo (single-ended, diferencial `d` o común `c`) y su índice (`:I`, `:II`…).
3. Conversión de frecuencia de cada puerto, f = numerador / denominador · f + offset:
   - versión 4: una sola;
   - versión 5: una para el receptor de test, otra para el de referencia y otra para la fuente.
4. Los números, cada uno con su parte real e imaginaria: primero la Zr de cada puerto y después los datos, por frecuencia, puerto receptor y puerto fuente (S11, S12, S21, S22).
   - **Versión 1:** cada número complejo lleva sus propias entradas. El fichero va comprimido con GZIP.
   - **Desde la versión 2:** un «vector plano». Contiene los valores, una tabla de entradas (identificador, descripción y distribución) y, de cada valor, sus dependencias: un puntero a la entrada, relativo al anterior, y la sensibilidad.

La incertidumbre típica y las covarianzas salen del presupuesto: u² = Σ (J·u_entrada)², V = J·diag(u²)·Jᵀ ([`10_algoritmos_de_incertidumbre.md`](10_algoritmos_de_incertidumbre.md) §6).

## 4. En CAL-ELEKTRA

| Fichero | Contenido |
|---|---|
| `sdata/unclib.py` | Números de METAS UncLib con propagación lineal: `Distribution`, `Input`, `UncNumber`, `UncVector` (vector plano). También sus formatos binarios, la covarianza y `cross_covariance` entre dos ficheros |
| `sdata/model.py` | `SParamData`: frecuencias, `PortDescription`, Zr, `s[:, receptor, fuente]` y `FrequencyConversion`. Calcula la incertidumbre típica (`standard_uncertainty`) y la covarianza de una frecuencia (`point_covariance`, en el orden del `.sdatcv`), y convierte a Touchstone |
| `sdata/sdatb.py` | `read_sdatb`, `write_sdatb`, `sdatb_version` |
| `sdata/sdatcv.py` | `read_sdatcv`, `write_sdatcv` |
| `sdata/files.py` | `load_sparam_data` y `save_sparam_data`, según la extensión |
| `sdata/__main__.py` | Resumen de un fichero y conversión a otro formato |

```python
from calelektra.sdata import load_sparam_data, save_sparam_data

data = load_sparam_data("Example_Loadf_18831_20150824.sdatb")
data.frequencies, data.ports, data.zr, data.s   # data.s[:, 1, 0] es S21
u_re, u_im = data.standard_uncertainty()        # típicas de la parte real y de la imaginaria
data.point_covariance(k)                        # matriz de covarianza de la frecuencia k
save_sparam_data("carga.sdatcv", data)          # o .sdatb, .s1p
```

Desde la consola:

```bash
python -m calelektra.sdata tests/datos/metas/Example_Loadf_18831_20150824.sdatb --convertir carga.sdatcv
```

```
Example_Loadf_18831_20150824.sdatb
  Formato: .sdatb versión 2
  Frecuencias: 533, de 9 kHz a 45 GHz
  Puertos: 1 (Zr: 50 Ω)
  Presupuesto: 12792 entradas, 25584 dependencias
  Incertidumbre típica máxima (parte real / imaginaria):
    S11: 0.00418 / 0.00452
Escrito: carga.sdatcv
```

## 5. Comprobación con los ficheros del laboratorio

Los dos ficheros son de VNA Tools y están en `tests/datos/metas/`:

| Fichero | Contenido |
|---|---|
| `Example_Loadf_18831_20150824.sdatb` (1,6 MB) | Carga hembra de 1 puerto, con 533 frecuencias de 9 kHz a 45 GHz. Tiene 12 792 entradas normales estándar, 24 por frecuencia, de un conjunto de datos de patrones (`MM008073_MM009287_nc.scolcv`) |
| `Example_Ada_ABf-f_101388_20211217.sdatb` (24 MB) | Adaptador de 2 puertos, con 400 frecuencias de 0,1 a 40 GHz. Tiene 265 018 entradas, normales y normales estándar, con 1 346 056 dependencias. S21 ≈ S12 |

- **Lectura completa** de los dos ficheros, sin bytes de más.
- **Reescritura idéntica byte a byte** de los dos.
- **Punteros relativos bien interpretados:** cada una de las 25 584 dependencias de la carga apunta a una entrada de su misma frecuencia. Lo dice su descripción: `Frequency Index: k`.
- **Correlación entre ficheros:** el adaptador comparte 9 598 entradas con la carga. Son las del conjunto de datos de la carga, que se usó en la calibración con la que se midió el adaptador. `cross_covariance` da su covarianza, que no es nula.
- **Tiempos:** el adaptador se lee en unos 2,4 s y se escribe en 0,9 s; la carga, en menos de 0,1 s.
- **`.sdatcv`:**
  - la carga se escribe y se vuelve a leer con los mismos valores y la misma covarianza en cada frecuencia;
  - los ejemplos de la especificación (1 puerto y 2 puertos con matriz reducida) se leen como dice el documento.
- **Versiones 1, 3, 4 y 5:** con ficheros sintéticos, escritos y releídos. No hay ficheros de VNA Tools de esas versiones.

Pruebas: `tests/test_sdata.py`.

## 6. Límites

- No se leen ni se escriben todavía:
  - `.sdatx` (XML);
  - las colecciones (`.scolb`, `.scolcv`);
  - los datos de VNA (`.vdatb`);
  - los de ruido (`.sndatb`).
- Las entradas del vector plano de la versión 1 (`UncInput`) siguen el documento, pero no se han podido comparar con ficheros reales.
- Para `RandomChoicesFromSamples`, la incertidumbre típica es la desviación típica de las muestras: los documentos no la definen.
- Las entradas nuevas tienen identificadores aleatorios de 16 bytes, como los de METAS UncLib. CAL-ELEKTRA las crea, por ejemplo, al leer un `.sdatcv`.
- CAL-ELEKTRA no usa ni incluye software de METAS: la licencia de METAS UncLib no permite distribuirla.

## 7. Siguiente paso: los kits

Un patrón con datos del kit hará referencia a un `.sdatb` e irá con su identificación, su fecha y los puertos que se usan, como en el asistente de VNA Tools (§4.6.3).

- Sus valores llegarán al VNA como Touchstone. En los R&S, con `MMEM:LOAD:CKIT:SDAT`.
- Su incertidumbre la tendrá en cuenta CAL-ELEKTRA.

## 8. Fuentes

- *METAS VNA Tools – Data Formats V3.0.2*, M. Wollensack, METAS, julio de 2026: §1 (datos), §2 (`.sdatb`), §3 (`.sdatx`) y §4 (`.sdatcv`).
- *METAS UncLib – Data Formats V3.0.2*, julio de 2026, en el paquete `metas-unclib` 3.0.3 de PyPI: formatos binarios y XML de los números con propagación lineal.
- *METAS VNA Tools – User Manual V3.0.2*: §3.1 (tipos de fichero) y §4.6.2 – 4.6.3 (patrones con datos).
