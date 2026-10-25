# Especificación de cables por fórmula (K1, K2 y VSWR de los conectores) — versión 0.9

Hasta ahora, el pasa / falla de un cable se daba con **ficheros S2P** del modelo: la máscara de las pérdidas de inserción máximas y de las pérdidas de retorno mínimas, siempre en negativo (|S21| ≥ máscara, |S11| ≤ máscara).

Ahora hay una **segunda forma**: los datos de la hoja del fabricante.
- **En el modelo del cable:** los coeficientes K1 y K2 de la atenuación y el VSWR de sus conectores.
- **En cada unidad:** su longitud.
- **En la plantilla de ensayo:** las tolerancias que dan los límites del pasa / falla.

Las dos formas pueden convivir en un mismo modelo; entonces se evalúan las dos.

## 1. Datos del modelo del cable

Campos nuevos de la categoría «cable», en el grupo **Especificación por fórmula** del modelo:

| Campo | Clave YAML | Valores |
|---|---|---|
| K1 (término en √f) | `k1_atenuacion` | dB por la longitud de referencia |
| K2 (término en f) | `k2_atenuacion` | dB por la longitud de referencia |
| Unidad de f en K1 y K2 | `unidad_frecuencia_k` | `Hz`, `kHz`, `MHz`, `GHz` |
| K1 y K2 por cada | `longitud_normalizacion_k` | `1 m`, `100 m`, `1 ft`, `100 ft`, `1 in` |
| VSWR del conector A | `vswr_conector_a` | VSWR (≥ 1) |
| VSWR del conector B | `vswr_conector_b` | VSWR; si no se indica, el del conector A |

La **longitud del cable** es su campo «Longitud» (`longitud_m`, en metros). Lo normal es darla en cada unidad del inventario; si no, vale la del modelo.

```yaml
# catalogo/modelos/cable_coaxial_292_hh_k.yaml (extracto)
campos:
  k1_atenuacion: 0.26              # dB/100 ft por √MHz
  k2_atenuacion: 0.00091           # dB/100 ft por MHz
  unidad_frecuencia_k: MHz
  longitud_normalizacion_k: 100 ft
  vswr_conector_a: 1.15
  vswr_conector_b: 1.15
```

## 2. Tolerancias de la plantilla de ensayo

| Tolerancia | Clave YAML | Límite |
|---|---|---|
| Pérdidas de inserción (dB) | `tolerancias_cable.perdidas_insercion_db` | IL_máx(f) = A(f) + t_IL |
| VSWR (%) | `tolerancias_cable.vswr_pct` | VSWR_máx = VSWR_cable · (1 + t_VSWR / 100) |

```yaml
# procedimientos/cable_CAB-DUT-0004.yaml (extracto)
tolerancias_cable:
  perdidas_insercion_db: 0.5   # IL máx. = A(f) + 0,5 dB
  vswr_pct: 10                 # VSWR máx. = VSWR del cable · 1,10
```

- En el editor de plantillas están en la tarjeta del EUT, en «Especificación por fórmula (cables): tolerancias». Ahí también se ve la fórmula y el VSWR del cable del EUT elegido.
- «— (sin tolerancia)» deja el límite en el valor nominal. La comprobación avisa: «La plantilla no fija las tolerancias del cable».
- Valen para los ensayos de parámetros S (`vna_2_puertos`) y de transmisión (`vna_transmision`). En la transmisión solo se mide S21, así que solo se evalúan las pérdidas de inserción.

## 3. Cálculo

### 3.1 Atenuación nominal

```
A(f) = ( K1·√(f/u) + K2·(f/u) ) · L / L_ref                 [dB]
```

| Símbolo | Significado |
|---|---|
| f | Frecuencia medida (Hz) |
| u | Unidad de f de los coeficientes: 1 Hz, 1 kHz, 1 MHz o 1 GHz |
| L | Longitud del cable (m) |
| L_ref | Longitud de referencia de los coeficientes, en metros: 1 m, 100 m, 0,3048 m (1 ft), 30,48 m (100 ft) o 0,0254 m (1 in) |

K1 es el término del conductor (efecto pelicular, ∝ √f) y K2 el del dieléctrico (∝ f), como en las hojas de datos.

**Ejemplo:** K1 = 0,26 y K2 = 0,00091 dB/100 ft con f en MHz, cable de 1,5 m, a 40 GHz (f/u = 40 000):

```
A = (0,26·200 + 0,00091·40 000) · 1,5 / 30,48 = 88,4 · 0,04921 = 4,350 dB
IL_máx = 4,350 + 0,5 = 4,850 dB        →  |S21|, |S12| ≥ −4,850 dB
```

### 3.2 VSWR del cable (suma lineal de las reflexiones)

Cada conector devuelve una parte de la onda:

```
|Γ_i| = (VSWR_i − 1) / (VSWR_i + 1)
```

La reflexión es pequeña, así que se supone que el primer conector deja pasar toda la onda. El cable devuelve lo que refleja el primer conector más lo que refleja el segundo, que vuelve entero. En el peor caso las dos están en fase y se suman linealmente:

```
|Γ_cable|  = |Γ_A| + |Γ_B|
VSWR_cable = (1 + |Γ_cable|) / (1 − |Γ_cable|)
VSWR_máx   = VSWR_cable · (1 + t_VSWR / 100)
|Γ_máx|    = (VSWR_máx − 1) / (VSWR_máx + 1)
RL_mín     = −20·log10 |Γ_máx|                 →  |S11|, |S22| ≤ −RL_mín
```

La segunda reflexión no se atenúa con la ida y vuelta por el cable. Es la decisión tomada: el peor caso, independiente de la frecuencia.

**Ejemplo:** conectores de VSWR 1,15:

```
|Γ_A| = |Γ_B| = 0,15 / 2,15 = 0,06977      →  |Γ_cable| = 0,13953
VSWR_cable = 1,13953 / 0,86047 = 1,3243
VSWR_máx = 1,3243 · 1,10 = 1,4568           →  |Γ_máx| = 0,18592  →  RL_mín = 14,61 dB
```

### 3.3 Pasa / falla

- Se evalúa dentro del **rango de frecuencia nominal** del cable (su `rango_frecuencia`), con los mismos criterios que los ficheros S2P:
  - pérdidas de inserción: margen = |S21| (dB) − (−IL_máx(f)) ≥ 0;
  - pérdidas de retorno: margen = −RL_mín − |S11| (dB) ≥ 0.
- La evaluación da dos especificaciones, cada una con sus parámetros (S21 y S12; S11 y S22), con resultado y margen mínimo:
  - «Pérdidas de inserción máximas (fórmula K1, K2)»;
  - «Pérdidas de retorno mínimas (VSWR de los conectores)».
- El veredicto del ensayo junta estas y las de los ficheros S2P, si las hay: FALLA si alguna falla, PASA si todas pasan y NO EVALUADO en otro caso.
- Como en el resto del programa, el pasa / falla compara el valor medido con el límite y **no usa la incertidumbre**.

### 3.4 Datos que faltan

La comprobación previa avisa de lo que no se puede evaluar, y el resto se evalúa igual:

| Falta | Efecto |
|---|---|
| La longitud del cable, la unidad de f o la longitud de referencia | No se evalúan las pérdidas de inserción |
| El VSWR de los conectores | No se evalúan las pérdidas de retorno |
| \|Γ_A\| + \|Γ_B\| ≥ 1 (VSWR muy altos) | No se evalúan las pérdidas de retorno |
| Las tolerancias en la plantilla | Los límites son los valores nominales (aviso) |

## 4. Dónde se ve

- **Comprobación de la plantilla y ventana del ensayo**:
  - línea «Especificación por fórmula» con la fórmula, la longitud, el VSWR de los conectores y el del cable, y los límites;
  - las curvas límite en la gráfica desde el principio del ensayo: discontinuas, en dB y en VSWR.
- **Tabla «Pasa / falla»:** una fila por especificación y parámetro, con el criterio, por ejemplo «IL ≤ A(f) + 0,5 dB; A = (0,26·√f + 0,00091·f) dB/100 ft con f en MHz, L = 1,5 m» o «VSWR ≤ 1,457 (RL ≥ 14,61 dB): conectores 1,15 y 1,15 → cable 1,324 + 10 %».
- **Registro del ensayo** (`especificacion_cable`): lo que se usó, para dibujar los límites después y para la trazabilidad:
  - K1 y K2, sus unidades, la longitud y el VSWR de los conectores;
  - las tolerancias;
  - el VSWR del cable, el VSWR máximo, el RL mínimo y el rango nominal.

## 5. Ejemplo de la biblioteca

| Pieza | Fichero |
|---|---|
| Modelo | `catalogo/modelos/cable_coaxial_292_hh_k.yaml`: cable 2,92 mm (h) – (h) flexible; K1 = 0,26, K2 = 0,00091 dB/100 ft con f en MHz; conectores de VSWR 1,15 |
| Unidad | `catalogo/equipos/CAB-DUT-0004.yaml`: 1,5 m |
| Plantilla | `procedimientos/cable_CAB-DUT-0004.yaml`: setup «Cables 2,92 mm — solo VNA (ZNB43)»; +0,5 dB y +10 % |

Con el VNA simulado el ensayo **PASA**:
- pérdidas de inserción con un margen mínimo de +0,49 dB a 39,9 GHz;
- pérdidas de retorno con +1,39 dB a 40 GHz.

Sin tolerancias **FALLA**, porque la pérdida medida queda unas centésimas por encima de A(f). Los tests lo comprueban.

| | |
|---|---|
| ![Gráfica con los límites de la fórmula](capturas/v09/27h_cable_formula_grafica.png) | ![Pasa / falla por fórmula](capturas/v09/27j_cable_formula_pasa_falla.png) |
| ![Tolerancias en la plantilla de ensayo](capturas/v09/07c_plantilla_cable_formula.png) | ![Las mismas curvas en «VSWR, Magnitud (dB)»](capturas/v09/27i_cable_formula_vswr.png) |

## 6. Ficheros del código

| Fichero | Contenido |
|---|---|
| `procedures/cable_specs.py` | `CableSpecification`: atenuación nominal, VSWR del cable, límites, curvas, evaluación y registro; `cable_specification` a partir de los campos |
| `procedures/model.py` | `CableTolerances` (`tolerancias_cable` de la plantilla) |
| `procedures/engine.py` | Comprobación previa (`RunPlan.cable_spec`) y paso «Evaluación pasa / falla» |
| `procedures/specs.py` | `entries_verdict`: veredicto de todas las evaluaciones del registro |
| `gui/procedures_view.py` | Tolerancias en el editor de plantillas |
| `gui/test_common.py` | Línea de la comprobación y curvas límite (del plan y del registro) |
| `tests/test_cable_specs.py` | Unidades, suma de reflexiones, datos que faltan, evaluación y ensayo de ejemplo |
