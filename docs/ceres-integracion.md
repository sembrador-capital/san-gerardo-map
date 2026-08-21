# Integrar datos de Ceres Imaging al mapa San Gerardo

Documento de trabajo. Guardalo en `docs/ceres-integracion.md` y trabajá contra él.
Toda la investigación de la API ya está hecha y verificada contra la cuenta real:
**los identificadores, endpoints y formatos de abajo son correctos, no los redescubras.**

---

# 0. Cómo trabajar en este repo

Leé esta sección antes de abrir un archivo.

**Empezá con un plan, no con código.** Presentámelo y esperá aprobación antes de editar.

**`index.html` pesa 525KB / ~8.100 líneas. No lo leas completo.** Se te va la mitad del
contexto en una operación y después editás a ciegas. Usá `grep -n` para ubicar la sección y
leé solo ese rango. Hay un mapa del archivo en §1.

**El mapa no funciona abierto con `file://`.** Hace `fetch('./archivo.json')` y CORS lo
bloquea, así que vas a ver un mapa sin datos y vas a creer que rompiste algo. Servilo siempre:

```bash
python3 -m http.server 8000     # → http://localhost:8000
```

**Rama aparte, nunca `main`. Mostrame el diff antes de cada commit.** El archivo es grande y
un `replace_all` mal apuntado hace daño silencioso.

**Este repo es público.** Ningún token, ninguna credencial, en ningún archivo. Nunca.

**Trabajá por etapas y commiteá entre medio.** Script → datos verificados → integración →
workflow. No arranques la integración al mapa sin tener el JSON real en la mano.

---

# 1. El repo

Mapa interactivo Mapbox GL del predio San Gerardo (nogales, Región del Maule, Chile),
operado por Sembrador Capital. Sitio estático en GitHub Pages.

```
index.html            ← TODO el mapa: HTML + CSS + JS en un solo archivo
data-version.json     ← {"<dataset>": "<version>"} para cache-busting
soil_data.json  foliar_data.json  agro_data.json
financial_data.json  pest_data.json  disease_data.json
tools/
```

El predio son **5 equipos de riego (E1–E5) subdivididos en 23 sectores**:
E1→5, E2→3, E3→5, E4→5, E5→5.

## Mapa del archivo

Líneas aproximadas; se corren al editar, verificá con `grep`.

| Qué | Líneas |
|---|---|
| Tokens de diseño `:root` | 16–160 |
| Botones de pestaña (HTML) | 1797–1805 |
| `GEOJSON` — los 23 sectores, inline | 2629 |
| `I18N` (es/en) | 2644–3079 |
| `SEM('--token')` — lee tokens CSS desde JS | 3170 |
| `DATA_VERSION` / `DATA_ERRORS` / `DATA_LABELS` / `MODE_DATASET` | 3178–3181 |
| `loadDataset()` + carga de todos los datasets | 3183–3230 |
| `EQ_NAMES` / `EQ_COLORS` / `EQ_SECTORS` | 3273, 3332–3333 |
| `STATUS_COLORS` / `STATUS_PALETTES` | 3335–3352 |
| `hexToRgb()` / `mixHex()` | 3374–3392 |
| `getStatus()` / `getFoliarColor()` — patrón de bandas | 3397–3450 |
| **Módulo de riego — referencia canónica** | **7487–7960** |
| `riegoKey()` / `getRiego()` | 7870–7871 |

**El módulo de riego es el patrón a imitar.** Ya resuelve estado por lado A/B, expresión de
color Mapbox, leyenda, panel lateral, popup y carga asíncrona. No inventes una arquitectura
nueva: seguí esa.

## Convenciones no negociables

Romperlas es un bug, no una preferencia de estilo.

1. **Ningún literal de color fuera de `:root`.** Ni hex, ni `rgb()`, ni `rgba()`. En CSS,
   `var(--token)`; en JS, `SEM('--token')`. Colores nuevos se declaran como tokens en `:root`
   con un comentario que explique de qué color de marca derivan.
2. **Ningún `z-index` fuera de la escala**
   `--z-map/legend/controls/panel/popup/dropdown/tooltip/modal`.
3. **Todo texto visible en español e inglés**, vía `t()` o `data-i18n`. Incluye títulos de
   gráficos, etiquetas de ejes, tooltips y estados vacíos.
4. **Un solo archivo.** No separes CSS ni JS.
5. **Los datos van en JSON externo** cargado con `loadDataset()`. No hardcodees datasets en
   el HTML.
6. **Paleta de marca (`--sem-*`) para el chrome, escalas propias (`--dat-*`) para los datos.**
   No los mezcles: si el dato toma prestado el navy de marca, el usuario confunde identidad
   con valor.

## ⚠️ Las dos claves de sector

Confundirlas es el bug más fácil de cometer en este repo:

- `GEOJSON.features[].properties.name` → `"E1 - S1"` **con espacios**
- `riegoKey(equipo_num, sector_num)`   → `"E1-S1"` **sin espacios**

Ceres usa el formato **sin espacios**. **Nunca hagas string matching entre los dos.**
Resolvé siempre vía `equipo_num` / `sector_num`, como hace `getRiego()`.

---

# 2. La API de Ceres

## Autenticación

Token DRF permanente en header:

```
Authorization: Token <CERES_TOKEN>
```

Existe también JWT (`POST /api/token/`), **no lo uses**: expira en 15 minutos con rotación de
refresh token, complejidad sin beneficio acá.

El token se lee de la variable de entorno `CERES_TOKEN` en local, y de
`${{ secrets.CERES_TOKEN }}` en CI. Si falta, error claro y `exit(1)`.

Base URL: `https://works.ceresimaging.net/api`

## Identificadores de San Gerardo (verificados)

| Entidad | Valor |
|---|---|
| user_id | `7868` |
| Cliente | `ATF Gestion` → `CFF\|4527` |
| Predio | `San Gerardo` → id `6524` → `CFF\|4527.6524` |
| Grid type sectores | `7` |
| Grid type equipos | `18` |

| Equipo | field_id | Nombre en Ceres |
|---|---|---|
| E1 | 85036 | San Gerardo - Nogales E1 |
| E2 | 85037 | San Gerardo - Nogales E2 |
| E3 | 85039 | San Gerardo - Nogales E3 |
| E4 | 85040 | San Gerardo - Nogales E4 |
| E5 | 85038 | San Gerardo - Nogales E5 |

## Endpoint 1 — listar vuelos

```
GET /api/admin_groups/weeks/7868/CFF%7C4527.6524/
```

Devuelve semanas ISO. **Quedate solo con `level === 0`** (`level 1` son subsemanas y duplican).
Cada una trae `key` (ej. `"2026.13.A"`, `A` = aéreo) y `capture_dates`.

Histórico actual — **14 vuelos, todos aéreos**:

```
2022.11.A  2022-03-18      2024.11.A  2024-03-12      2025.48.A  2025-11-25
2022.46.A  2022-11-14/15   2024.46.A  2024-11-12      2026.5.A   2026-01-26
2022.51.A  2022-12-20      2024.51.A  2024-12-17      2026.13.A  2026-03-23
2023.44.A  2023-11-03      2025.8.A   2025-02-18
2023.47.A  2023-11-20      2025.12.A  2025-03-18
```

**Los vuelos se concentran entre noviembre y marzo. De abril a octubre no hay ninguno.**
Este hecho manda sobre el diseño de los gráficos y de los selectores. Volvé a él.

## Endpoint 2 — valores de un vuelo

```
GET /api/tables/flight_summary/?admin_group=CFF%7C4527.6524&week=<WEEK_KEY>&grid_type_id=<7|18>
```

`grid_type_id=7` → 23 sectores. `grid_type_id=18` → 5 equipos.

**Llamá a los dos. No promedies los sectores para obtener el equipo:** Ceres agrega sobre los
píxeles y eso no es un promedio de promedios. Son 28 llamadas para toda la historia.

Respuesta (array; un elemento por unidad):

```json
{
  "field_id": 85036,
  "field_name": "San Gerardo - Nogales E1",
  "block_name": "E1-S3",
  "overlays": [
    { "overlay_type": "water_stress", "value": 0.3714, "color": "#01a001",
      "area": 61139.0, "plants": 1414, "capture_date": "2026-03-23" }
  ]
}
```

`block_name` es exactamente el output de `riegoKey()`. Cero mapeo necesario.
`area` en m², `plants` es el conteo de árboles.

## Los indicadores

| `overlay_type` | ES | EN | Rango | Dirección |
|---|---|---|---|---|
| `water_stress` | Estrés hídrico | Water stress | 0–1 | **alto = peor** |
| `absolute_ndvi` | NDVI | NDVI | 0–1 | alto = mejor |
| `season_average_ndvi` | NDVI promedio temporada | Season avg NDVI | 0–1 | alto = mejor |
| `chlorophyll_class` | Clorofila | Chlorophyll | 0–1 | alto = mejor |
| `cumulative_thermal_stress` | Estrés térmico acumulado | Cumulative thermal stress | 0–1 | **alto = peor** |
| `colorized_ndvi` | — | — | 0–1 | ignorar: duplica `absolute_ndvi` |

Extraé los 5 útiles: vienen todos en la misma respuesta, no cuestan nada extra.

## Umbrales: Ceres ya los publica

**No inventes rangos.** Cada overlay trae en `download_urls` una URL con un parámetro
`colorMap` URL-encoded con las bandas oficiales. Verificado para `water_stress`:

```json
[["#ff0101", 0.75, 1.00],
 ["#ffff01", 0.50, 0.75],
 ["#01a001", 0.25, 0.50],
 ["#0101ff", 0.00, 0.25]]
```

Formato `[color, min, max]`. Parseá ese `colorMap` **por `overlay_type`** y guardá los cortes.
Cada indicador tiene su propia definición: no asumas 4 bandas ni los mismos cortes para todos.

Los colores de Ceres son referencia; en el mapa se remapean a tokens del sistema de diseño.
Lo que importa son los **cortes numéricos**.

## Datos por árbol (existen, fase 2)

Confirmado para San Gerardo: **285 overlays `source=tree_data`** (valor por árbol individual)
y **305 `source=tree_count`** (posición y variedad de cada árbol).

```
GET /api/overlays/?admin_group=CFF%7C4527.6524&source=tree_data&ordering=-capture_date
```

Cada uno entrega en `url` un template MVT
`https://tiler.ceresimaging.net/tree/data/{id}/{z}/{x}/{y}.mvt`, y en `download_urls` un
`geotiff` y un `png`. Ver §5.3 para cómo tratarlo.

---

# 3. Tarea 1 — `tools/fetch_ceres.py`

Python 3, solo `requests` como dependencia externa.

- Token desde `CERES_TOKEN`. Si falta: error claro y `exit(1)`.
- Listá las semanas, filtrá `level === 0`, ordená por fecha ascendente.
- Por cada semana, llamá a `flight_summary` con `grid_type_id=7` y con `18`.
- Extraé las bandas desde el `colorMap` de `download_urls`, por `overlay_type`.
- Reintentos con backoff exponencial (3 intentos), timeout 60s. **Si una semana falla después
  de los reintentos, registrala y seguí** — no abortes la corrida entera.
- Modo incremental: si `ceres_data.json` existe, consultá solo las semanas ausentes.
  Flag `--full` para refetch completo.
- Resumen al terminar: vuelos nuevos, unidades por vuelo, y **advertencia ruidosa si un vuelo
  trae ≠ 23 sectores o ≠ 5 equipos** (señal de que cambió la grilla en Ceres).
- Salida ≠ 0 solo si no se pudo escribir nada útil.

## Esquema de `ceres_data.json`

Indexado por fecha, nivel y unidad, para que el mapa no tenga que recorrer arrays.

```json
{
  "generated_at": "2026-08-21T14:00:00Z",
  "source": "Ceres Imaging",
  "farm": { "name": "San Gerardo", "customer": "ATF Gestion",
            "admin_group": "CFF|4527.6524" },

  "params": [
    {
      "id": "water_stress",
      "es": "Estrés hídrico", "en": "Water stress",
      "min": 0, "max": 1,
      "higher_is_better": false,
      "bands": [
        { "min": 0.00, "max": 0.25, "es": "Óptimo",   "en": "Optimal",  "status": "opt"  },
        { "min": 0.25, "max": 0.50, "es": "Adecuado", "en": "Adequate", "status": "bajo" },
        { "min": 0.50, "max": 0.75, "es": "Alerta",   "en": "Warning",  "status": "alto" },
        { "min": 0.75, "max": 1.00, "es": "Crítico",  "en": "Critical", "status": "exc"  }
      ],
      "bands_source": "ceres"
    }
  ],

  "sectors": {
    "E1-S1": { "equipo": 1, "sector": 1, "field_id": 85036,
               "area_m2": 56003, "plants": 1282, "plants_per_ha": 228.9 }
  },
  "equipos": {
    "E1": { "equipo": 1, "field_id": 85036, "n_sectores": 5,
            "area_m2": 295550, "plants": 6818, "plants_per_ha": 230.7 }
  },

  "flights": [
    {
      "week_key": "2026.13.A",
      "date": "2026-03-23",
      "season": "2025-26",
      "sectors": { "E1-S1": { "water_stress": 0.3561, "absolute_ndvi": 0.8786 } },
      "equipos": { "E1":    { "water_stress": 0.3402, "absolute_ndvi": 0.8801 } },
      "deltas":  { "sectors": { "E1-S1": { "water_stress": 0.0412 } },
                   "equipos": { "E1":    { "water_stress": 0.0388 } } },
      "compliance": {
        "water_stress": {
          "by_sector":  { "opt": 2, "bajo": 18, "alto": 3, "exc": 0 },
          "by_area_m2": { "opt": 110000, "bajo": 1050000, "alto": 175000, "exc": 0 },
          "by_plants":  { "opt": 2540, "bajo": 24300, "alto": 4050, "exc": 0 }
        }
      }
    }
  ]
}
```

Notas:

- `flights` ordenado por fecha ascendente.
- `season` en formato `"2025-26"` (julio a junio), **igual que `buildSeasonsGrid()`** en
  index.html, para que los selectores del mapa sean coherentes entre modos.
- `bands` sale del `colorMap` de Ceres. `status` mapea a las clases que **ya existen** en
  index.html (`STATUS_COLORS` / `STATUS_PALETTES`, tokens `--dat-st-*`): no crees un sistema
  de estados paralelo.
- `bands_source`: `"ceres"` o `"custom"`. Si existe `ceres_thresholds.json` en la raíz, **sus
  bandas pisan a las de Ceres** y pasa a `"custom"`. Así agronomía ajusta los cortes al nogal
  en Chile editando un JSON, sin tocar script ni mapa. Documentá ese archivo en el script.
- `deltas`: diferencia contra el vuelo anterior, por unidad e indicador. Calculado en el
  script, no en el navegador. Es lo que permite responder "qué se deterioró" (ver §5).
- `compliance`: cuántos sectores, cuánta superficie y cuántas plantas caen en cada banda.
  También precalculado.
- `area_m2` y `plants` toman el valor del vuelo más reciente que los traiga.
- Sin dato para un indicador en un vuelo: **omitir la clave**, no poner `null`.

Al final, **bumpeá `data-version.json`**: agregá/actualizá la clave `"ceres"` con la fecha del
vuelo más reciente, preservando el resto de las claves intactas.

---

# 4. Tarea 2 — `.github/workflows/ceres.yml`

- Cron semanal (lunes ~06:00 UTC) + `workflow_dispatch`.
- Setup Python, `pip install requests`, correr el script.
- Commitear `ceres_data.json` y `data-version.json` **solo si cambiaron**
  (`git diff --quiet || git commit`). Nada de commits vacíos.
- `permissions: contents: write` y nada más.
- Actions fijadas a SHA, no a `@v4`.
- Token desde `${{ secrets.CERES_TOKEN }}`. **Nunca lo imprimas, nada de `set -x`.**
- Solo `schedule` y `workflow_dispatch`. **Jamás `pull_request_target`**: en un repo público
  ese evento entrega los secrets a PRs de forks.

El cron semanal es holgado a propósito: hay 3–4 vuelos por temporada y ninguno entre abril y
octubre.

---

# 5. Tarea 3 — Integrar al mapa

Nueva pestaña **"Ceres"** entre Riego y Agroquímicos, modelada sobre el módulo de riego.

Registrala en `DATA_LABELS` (`ceres: 'Ceres Imaging'`), en `MODE_DATASET` (`ceres: 'ceres'`),
y agregá el `loadDataset('ceres','ceres_data.json', …)` junto a los otros.

## 5.1 Controles

- **Nivel**: toggle `Equipo` / `Sector` → 5 polígonos agregados o los 23 sectores. Usá el
  valor nativo de cada nivel del JSON, nunca promedios calculados en el cliente.
- **Indicador**: los 5 útiles, etiquetas vía `t()`.
- **Vuelo**: las 14 fechas agrupadas por temporada, más reciente primero.
- Debe funcionar con el **comparador A/B** (`mapbox-gl-compare`) que ya existe: estado por
  lado, como `RIEGO_STATE = { A: …, B: … }`. Comparar dos vuelos lado a lado es el caso de uso
  más valioso con datos estacionales.

## 5.2 Pintado, umbrales y leyenda

Expresión Mapbox `['match', ['get','name'], …]` recorriendo `GEOJSON.features`, igual que
`getRiegoColorExpr()`. Recordá las dos claves de sector (§1).

**El color sale de la banda de umbral, no de una rampa continua.** Eso es lo que hace que el
mapa se lea como cumplimiento y no como un degradé decorativo. Reutilizá `STATUS_COLORS` /
`STATUS_PALETTES` y los tokens `--dat-st-*` que ya usan los foliares; dentro de una banda
podés interpolar con `mixHex()`, como `getFoliarColor()`. Sin dato: `--sem-grey`.

**Leyenda**: una fila por banda con su rango numérico y su etiqueta, más "Sin datos". Al lado,
el resumen de cumplimiento del vuelo activo leído de `compliance`, en chips tipo
`18/23 sectores · 72% superficie`. Que el número esté a la vista, no escondido en un popup.

## 5.3 Por planta

**Nivel A — inmediato, con los datos que ya trae el JSON:**

- `plants_per_ha` por sector y equipo, en popup y panel.
- Cumplimiento ponderado por plantas (`compliance.by_plants`): no es lo mismo que 3 de 23
  sectores estén críticos, a que esos 3 concentren 4.000 árboles. Mostrá las dos lecturas.
- Marcá sectores con densidad anómala respecto a la mediana del predio (±15%): suele delatar
  fallas de plantación o replante.

**Nivel B — capa por árbol individual. Fase 2, recién después de que todo lo anterior funcione.**

Antes de escribir código, **verificá empíricamente si `tiler.ceresimaging.net` exige el header
`Authorization`** (pedí un tile con y sin él).

- Si **no** exige auth: consumí el MVT directo como source vectorial de Mapbox GL.
- Si **sí** exige auth: **NO** inyectes el token con `transformRequest`. Este sitio es público
  y eso lo filtraría a cualquier visitante. La única ruta válida es pre-extraer en el build:
  decodificar los MVT con `mapbox-vector-tile` sobre el bbox del predio a z≈16, o samplear el
  `geotiff`, y escribir `ceres_trees_<fecha>.geojson` con **solo el último vuelo**.

Son ~30.000 árboles. Cargá la capa **bajo demanda** al activar el toggle, nunca en el load
inicial; capa `circle` con radio interpolado por zoom; visible desde z≥15 — más abajo es un
manchón ilegible.

## 5.4 Gráficos (Chart.js, ya cargado en el `<head>`)

⚠️ **Lo más importante:** 14 vuelos en 4 años, concentrados entre noviembre y marzo. Un eje
`type:'time'` lineal deja más de la mitad del gráfico vacío y vuelve ilegible la temporada.
Usá **eje de categoría** con las fechas como etiquetas, agrupadas visualmente por temporada.
No uses un eje temporal ingenuo.

1. **Evolución de una unidad**: línea del indicador a través de los 14 vuelos, con las bandas
   de umbral pintadas de fondo como franjas horizontales. Se ve de un vistazo cuándo cruzó a
   Alerta.
2. **Todos los sectores de un equipo superpuestos**: 3–5 líneas. Sirve para detectar el sector
   que se despega del resto — probablemente el uso más valioso de todos.
3. **Comparación entre temporadas**: eje X = mes, una serie por temporada. Responde "¿venimos
   peor que el año pasado a la misma altura?".
4. **Distribución del vuelo activo**: barras horizontales de los 23 sectores ordenadas de peor
   a mejor, coloreadas por banda. El ranking accionable del día.

Las vistas 1 y 2 en el panel al hacer clic en una unidad; 3 y 4 en el panel lateral del modo.
Todos los títulos, ejes y tooltips vía `t()`.

---

# 6. Qué hace que este mapa quede bien

El objetivo no es mostrar datos: es que un agrónomo abra la pestaña y en cinco segundos sepa
**dónde ir mañana**. Optimizá para eso.

**El estado inicial tiene que ser útil solo.** Al entrar a la pestaña, sin tocar nada:
último vuelo, estrés hídrico, nivel sector, ranking visible. Cero configuración para obtener
valor. Un modo que arranca vacío pidiendo que elijas tres cosas ya perdió.

**Mostrá el cambio, no solo el estado.** El dato más accionable no es "este sector está en
0.52", es "este sector pasó de Adecuado a Alerta desde el vuelo anterior". Por eso `deltas`
está precalculado. Que el popup y el ranking muestren la flecha y la magnitud del cambio.

**Sé explícito con la antigüedad del dato.** El último vuelo es del 23-mar-2026 y hoy puede
ser agosto. El mapa debe decir "Vuelo del 23 de marzo de 2026 · hace 5 meses" de forma
visible, no en letra chica. Un dato de hace cinco meses presentado como actual es peor que no
mostrarlo. Ya existe el patrón `dataStamp` / `fmtDataVersion` en el archivo: reutilizalo.

**Estados vacíos reales, no pantallas en blanco.** Sin datos para un sector, sin vuelos en la
temporada, fallo de carga: cada uno con su mensaje en es/en. Los fallos van a `DATA_ERRORS`,
que ya existe. No los silencies.

**Que se sienta el mismo producto.** Misma tipografía, mismos radios, mismas sombras, mismo
comportamiento de panel y popup que Riego y Plagas. Si se nota que es una pestaña pegada
aparte, está mal.

**No bloquees el load inicial.** El JSON es chico, pero la capa de árboles no. Cargá lo pesado
bajo demanda.

**Móvil.** El mapa se usa en terreno, en un teléfono, con sol. Los controles tienen que ser
tocables y la leyenda legible sin zoom.

## Anti-patrones — no hagas esto

- Rampa de color continua en vez de bandas de umbral: se ve linda y no permite decidir nada.
- Promediar sectores para obtener el valor del equipo, teniendo el agregado nativo de Ceres.
- Umbrales inventados o hardcodeados en el JS, teniendo el `colorMap` de Ceres y el
  `ceres_thresholds.json` para overrides.
- Eje temporal lineal en los gráficos.
- Calcular deltas o cumplimiento en el navegador: es trabajo del script.
- Mostrar los 6 indicadores incluyendo `colorized_ndvi`, que duplica `absolute_ndvi`.
- Meter el token en cualquier cosa que llegue al navegador.
- Un `<canvas>` de Chart.js por sector sin destruir la instancia anterior: fuga de memoria
  clásica al cambiar de sector.

---

# 7. Verificación antes de dar por terminado

**Datos**

- `--full` produce 14 vuelos, con 23 sectores y 5 equipos cada uno.
- Las 23 claves de `sectors` calzan exactamente con las que produce
  `riegoKey(equipo_num, sector_num)` recorriendo `GEOJSON.features`. Si sobra o falta una,
  fallá ruidosamente.
- Se extrajeron bandas del `colorMap` para **cada** indicador.
- `compliance` suma exactamente 23 sectores por indicador y vuelo.
- Correr el script dos veces seguidas no genera cambios la segunda vez (idempotente).

**Mapa**

- Servido por HTTP, entrar a Ceres: pinta sin tocar nada, con el último vuelo.
- Cambiar indicador, vuelo y nivel: los 23 sectores y los 5 equipos pintan siempre; ninguno
  queda gris por error de clave.
- El toggle Equipo/Sector muestra valores distintos y nativos, no el mismo dato promediado.
- Comparador A/B con dos vuelos distintos funciona en ambos lados.
- Los 4 gráficos: ninguno con huecos de 7 meses en el eje.
- Cambiar de sector 10 veces seguidas no degrada el rendimiento (instancias de Chart.js
  destruidas correctamente).
- Alternar es/en: no queda ningún texto sin traducir, gráficos incluidos.

**Código**

- `grep -nE '#[0-9a-fA-F]{3,8}|rgba?\(' index.html` no devuelve nada fuera del bloque `:root`.
- Ningún `z-index` fuera de la escala.
- Sin errores ni warnings en consola.

---

# 8. Orden de trabajo

1. Plan, aprobado por mí.
2. `tools/fetch_ceres.py` + correrlo con `--full` → tener `ceres_data.json` real.
3. Inspeccionar el JSON generado y correr las verificaciones de datos. **No sigas hasta que
   pasen todas.**
4. Integración a `index.html`: primero pintado + leyenda + umbrales; después panel y popup;
   después gráficos.
5. Workflow.
6. Fase 2 (capa por árbol) solo si 1–5 están terminados y verificados.

Diff antes de cada commit.
