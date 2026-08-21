#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fetch_ceres.py — Descarga los vuelos de Ceres Imaging del predio San Gerardo y
escribe ceres_data.json en la raiz del repo, listo para que el mapa lo lea con
loadDataset('ceres', ...).

────────────────────────────────────────────────────────────────────────────────
CREDENCIAL

El token DRF permanente de Ceres se lee, en este orden:

  1. Variable de entorno CERES_TOKEN   <- lo que usa CI (secrets.CERES_TOKEN)
  2. Archivo .ceres_token en la raiz   <- conveniencia para correr en local

.ceres_token esta en .gitignore. Este repo es publico: el token no va en ningun
archivo rastreado, ni en un log, ni en un mensaje de error. Si falta, el script
aborta con exit(1) sin imprimir nada del valor.

────────────────────────────────────────────────────────────────────────────────
UMBRALES

Cada indicador declara en PARAMS como se clasifica (bands_policy). Nada se
adivina en runtime: la decision es humana y esta escrita con su motivo.

  water_stress               "ceres"      4 clases, cortes publicados por Ceres
                                          (0/0,25/0,50/0,75/1), rotuladas 1..4
                                          como en la plataforma
  cumulative_thermal_stress  "share:"     hereda los cortes de water_stress: es
                                          su promedio de temporada (verificado,
                                          error 0,0000 en 322 observaciones)
  absolute_ndvi              "fixed"      9 clases con cortes de agronomia. El
  season_average_ndvi                     colorMap de Ceres es una rampa
                                          uniforme de 0,05: escala fija, pero
                                          despliegue y no clasificacion
  chlorophyll_class          "relative"   4 clases relativas AL VUELO. Es un
                                          indice relativo: la auditoria de los
                                          845 overlays encuentra 67 colorMap
                                          distintos en 69, recalculados por
                                          campo y por vuelo. La plataforma
                                          tampoco muestra numeros ahi, muestra
                                          1-Mas bajo .. 4-Mas alto

Los cortes publicados salen del parametro colorMap de download_urls en
/api/overlays/ (NO de flight_summary, cuyos overlays vienen sin download_urls).

Un indicador relativo guarda sus cortes DENTRO de cada vuelo y por nivel, en
flights[].relative_bands, porque no son los mismos entre fechas. El mapa lo
advierte: dos vuelos no son comparables entre si.

Queda una cuarta modalidad, "unclassified", para el indicador que no tenga con
que clasificarse. Hoy ninguno cae ahi, pero es la red si Ceres cambia algo.

Para ajustar cualquier corte sin tocar codigo ni mapa, crea
ceres_thresholds.json en la raiz del repo. Un override SIEMPRE gana sobre la
politica, incluso convierte un relativo en cortes fijos:

    {
      "water_stress": {
        "bands": [
          {"min": 0.00, "max": 0.30},
          {"min": 0.30, "max": 0.55},
          {"min": 0.55, "max": 0.80},
          {"min": 0.80, "max": 1.00}
        ]
      }
    }

Sus bandas pisan las del indicador y bands_source pasa a "custom". Las etiquetas
y los codigos de estado se derivan de la cantidad de bandas y de la direccion del
indicador; no hace falta escribirlas.

────────────────────────────────────────────────────────────────────────────────
USO

    pip install requests
    python tools/fetch_ceres.py --full      # historia completa (28 llamadas)
    python tools/fetch_ceres.py             # incremental: solo vuelos ausentes

Correr dos veces seguidas no genera cambios la segunda vez: si el JSON
resultante es identico al que ya esta en disco, no se reescribe (ni siquiera
generated_at).
"""

import argparse
import json
import os
import sys
import time
import urllib.parse
from collections import OrderedDict
from datetime import datetime, timezone

try:
    import requests
except ImportError:
    sys.stderr.write(
        "ERROR: falta la dependencia `requests`.\n"
        "       Instalala con:  pip install requests\n"
    )
    sys.exit(1)

# ── Identificadores de San Gerardo (verificados contra la cuenta real) ───────
BASE_URL = "https://works.ceresimaging.net/api"
USER_ID = "7868"
ADMIN_GROUP = "CFF|4527.6524"
FARM_NAME = "San Gerardo"
CUSTOMER = "ATF Gestion"

GRID_TYPE_SECTORES = 7
GRID_TYPE_EQUIPOS = 18

# Cuantas unidades tiene que devolver cada vuelo. Si no calza, cambio la grilla
# en Ceres y hay que revisar antes de confiar en el dato.
N_SECTORES = 23
N_EQUIPOS = 5

# field_id -> equipo. Sirve para etiquetar el nivel equipo, donde block_name no
# necesariamente viene con el formato "E<n>".
FIELD_TO_EQUIPO = {
    85036: "E1",
    85037: "E2",
    85039: "E3",
    85040: "E4",
    85038: "E5",
}

# bands_policy, por indicador (ver el detalle en el encabezado del archivo):
#
#   "ceres"          los cortes publicados son clases agronomicas de verdad
#   "share:<otro>"   toma prestados los cortes de otro indicador que mide lo mismo
#   "fixed"          cortes definidos por agronomia, en `cuts`
#   "relative"       clases recalculadas en cada vuelo, `n_classes`
#   "unclassified"   sin nada con que clasificar; el mapa lo muestra en gris
#
# Es una decision humana verificada contra el dato real de San Gerardo (14 vuelos,
# 322 observaciones por indicador): vive aca escrita y con su motivo, y no se
# infiere en runtime.

# Cortes de NDVI definidos por agronomia. No los publica Ceres como umbral: su
# colorMap es una rampa uniforme de 0,05. Nueve clases, con la primera y la
# ultima abiertas (<0,50 y >0,85).
NDVI_CUTS = [0.0, 0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 1.0]

# Los nombres en espanol son los de la plataforma de Ceres, tal como los ve
# agronomia: si el mapa los llamara de otra forma, habria que traducir de cabeza
# entre las dos herramientas. Los de ingles son equivalentes razonables (solo se
# verifico la interfaz en espanol).
#
# `group` reproduce la agrupacion de la plataforma, para que el selector del mapa
# se lea igual.
PARAMS = OrderedDict([
    ("water_stress", {
        "es": "Estrés Hídrico - perennes", "en": "Water stress - perennials",
        "desc_es": "Déficit de transpiración",
        "desc_en": "Transpiration deficit",
        "group_es": "Irrigación", "group_en": "Irrigation",
        "higher_is_better": False,
        "bands_policy": "ceres",
        # Los cuatro tramos publicados son las cuatro clases que la plataforma
        # rotula 1..4; se usan sus nombres y no unos genericos.
        "labels": "stress_4",
    }),
    ("cumulative_thermal_stress", {
        "es": "Estrés Acumulado", "en": "Cumulative stress",
        "desc_es": "Déficit promedio de transpiración esta temporada",
        "desc_en": "Average transpiration deficit this season",
        "group_es": "Irrigación", "group_en": "Irrigation",
        "higher_is_better": False,
        # Verificado sobre las 322 observaciones con error 0.0000: este indicador
        # es el promedio corrido de water_stress dentro de la temporada. Misma
        # magnitud fisica (deficit de transpiracion), misma escala 0-1, mismo
        # significado, asi que le corresponden los MISMOS cortes publicados que a
        # water_stress. No es un umbral inventado: es el umbral de Ceres aplicado
        # a la media de Ceres de la misma cantidad. La rampa de 10 tramos que
        # publica para este overlay es una eleccion de despliegue del mapa de
        # calor, no una clasificacion.
        "bands_policy": "share:water_stress",
        "labels": "stress_4",
    }),
    ("absolute_ndvi", {
        "es": "Índice de Vegetación Absoluto", "en": "Absolute vegetation index",
        "desc_es": "Crecimiento del dosel",
        "desc_en": "Canopy growth",
        "group_es": "Desarrollo de cultivos", "group_en": "Crop development",
        "higher_is_better": True,
        # El colorMap de Ceres es una rampa uniforme de 0,05 (escala fija, pero
        # despliegue y no clasificacion). Los cortes de abajo los define
        # agronomia: nueve clases, con el primero y el ultimo abiertos.
        "bands_policy": "fixed",
        "cuts": NDVI_CUTS,
        "labels": "ranges",
    }),
    ("season_average_ndvi", {
        "es": "Índice de Vegetación promedio temporada",
        "en": "Season average vegetation index",
        "desc_es": "Crecimiento promedio del dosel esta temporada",
        "desc_en": "Average canopy growth this season",
        "group_es": "Desarrollo de cultivos", "group_en": "Crop development",
        "higher_is_better": True,
        # Es el promedio corrido del NDVI dentro de la temporada (verificado,
        # error 0.0000 en 299 obs), asi que vive en la misma escala y le
        # corresponden los mismos cortes.
        "bands_policy": "fixed",
        "cuts": NDVI_CUTS,
        "labels": "ranges",
    }),
    ("chlorophyll_class", {
        "es": "Clorofila", "en": "Chlorophyll",
        "desc_es": "Crecimiento relativo del dosel",
        "desc_en": "Relative canopy growth",
        "group_es": "Desarrollo de cultivos", "group_en": "Crop development",
        "higher_is_better": True,
        # La plataforma lo describe como "crecimiento RELATIVO del dosel", y la
        # auditoria de los 845 overlays lo confirma: 67 colorMap DISTINTOS en 69
        # overlays, recalculados por campo Y por vuelo. Por eso NO se le pueden
        # poner cortes fijos: el mismo valor caeria en clases distintas segun el
        # vuelo. Y por eso la plataforma no muestra numeros, muestra cuatro
        # clases relativas (1 - Mas bajo .. 4 - Mas alto).
        #
        # Se clasifica igual que ahi: cuartiles de la distribucion DE CADA VUELO,
        # calculados en el script y por nivel. Los cortes viven en el vuelo y no
        # en el indicador, porque cambian vuelo a vuelo.
        "bands_policy": "relative",
        "n_classes": 4,
        "labels": "relative",
        "relative_es": "Clases relativas al vuelo: los cortes se recalculan en cada vuelo, así que no son comparables entre fechas.",
        "relative_en": "Classes relative to the flight: cuts are recomputed per flight, so they are not comparable across dates.",
    }),
])

# colorized_ndvi queda fuera: la plataforma lo describe como "crecimiento
# RELATIVO del dosel" frente al "crecimiento del dosel" del absoluto, o sea es el
# mismo NDVI renderizado contra la distribucion del vuelo. Como valor por sector
# no es comparable entre vuelos, que es justo lo que el mapa necesita.
# cir (Infrarroja Color) y core_thermal (Térmica) tampoco entran: son imagenes
# para mirar, no indices con un valor por unidad.
IGNORED_OVERLAYS = {"colorized_ndvi", "cir", "core_thermal"}

# ── Escalera de estados ──────────────────────────────────────────────────────
# Los codigos salen de STATUS_COLORS / STATUS_PALETTES de index.html: no se crea
# un sistema de estados paralelo. severity (0 = mejor) es lo que el mapa usa
# para elegir el color, porque el orden de los tokens en la escala foliar es
# posicional (def -> bajo -> optimo -> alto -> exc, de dos lados) y no una rampa
# de severidad: pintar por status daria naranjo antes que amarillo.
STATUS_LADDER = {
    2: ["opt", "exc"],
    3: ["opt", "alto", "exc"],
    4: ["opt", "bajo", "alto", "exc"],
    5: ["opt", "bajo", "alto", "exc", "def"],
}

BAND_LABELS = {
    2: [("Óptimo", "Optimal"), ("Crítico", "Critical")],
    3: [("Óptimo", "Optimal"), ("Alerta", "Warning"), ("Crítico", "Critical")],
    4: [("Óptimo", "Optimal"), ("Adecuado", "Adequate"),
        ("Alerta", "Warning"), ("Crítico", "Critical")],
    5: [("Óptimo", "Optimal"), ("Adecuado", "Adequate"), ("Moderado", "Moderate"),
        ("Alerta", "Warning"), ("Crítico", "Critical")],
}

# Etiquetas de la plataforma de Ceres, indexadas por SEVERIDAD (0 = mejor). Se
# usan en vez de las genericas de arriba cuando el indicador las declara, para
# que el mapa nombre las clases igual que la herramienta que usa agronomia.
PLATFORM_LABELS = {
    "stress_4": [
        ("1 - No estresadas", "1 - Not stressed"),
        ("2 - Estrés bajo", "2 - Low stress"),
        ("3 - Estrés moderado", "3 - Moderate stress"),
        ("4 - Estrés alto", "4 - High stress"),
    ],
}


def relative_labels(n):
    """
    Clases relativas 1..n por posicion de VALOR ascendente: 1 es el mas bajo del
    vuelo. Se generan para el n que resulte y no se leen de una tabla fija,
    porque los cuartiles pueden colapsar: con 5 equipos y dos valores empatados
    quedan 3 clases, y ahi una tabla de 4 entradas dejaria ese vuelo con nombres
    distintos a los demas.
    """
    out = []
    for i in range(n):
        cls = i + 1
        if n == 1:
            out.append(("Única clase", "Single class"))
        elif cls == 1:
            out.append(("1 - Más bajo", "1 - Lowest"))
        elif cls == n:
            out.append(("%d - Más alto" % cls, "%d - Highest" % cls))
        else:
            out.append((str(cls), str(cls)))
    return out


def range_labels(cuts, decimals=2):
    """
    Etiquetas de rango a partir de los cortes, en orden de valor ascendente:
    "<0,50", "0,50–0,55", ..., ">0,85". El primero y el ultimo se abren, porque
    sus extremos son el limite del indice y no un corte real.
    """
    def es(v):
        return ("%." + str(decimals) + "f") % v
    def num_es(v):
        return es(v).replace(".", ",")
    out = []
    n = len(cuts) - 1
    for i in range(n):
        lo, hi = cuts[i], cuts[i + 1]
        if i == 0:
            out.append(("<" + num_es(hi), "<" + es(hi)))
        elif i == n - 1:
            out.append((">" + num_es(lo), ">" + es(lo)))
        else:
            out.append((num_es(lo) + "–" + num_es(hi), es(lo) + "–" + es(hi)))
    return out



REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DEFAULT = os.path.join(REPO_ROOT, "ceres_data.json")
DATA_VERSION_PATH = os.path.join(REPO_ROOT, "data-version.json")
OVERRIDES_PATH = os.path.join(REPO_ROOT, "ceres_thresholds.json")
TOKEN_FILE = os.path.join(REPO_ROOT, ".ceres_token")

TIMEOUT = 60
RETRIES = 3


# ═══════════════════════════════════════════════════════════════════════════
# Credencial
# ═══════════════════════════════════════════════════════════════════════════

def read_token():
    tok = (os.environ.get("CERES_TOKEN") or "").strip()
    if tok:
        return tok
    if os.path.isfile(TOKEN_FILE):
        try:
            with open(TOKEN_FILE, "r", encoding="utf-8") as fh:
                tok = fh.read().strip()
        except OSError as exc:
            sys.stderr.write("ERROR: no se pudo leer .ceres_token: %s\n" % exc)
            sys.exit(1)
        if tok:
            return tok
    sys.stderr.write(
        "ERROR: falta el token de Ceres.\n"
        "\n"
        "  Local:  pone el token en el archivo .ceres_token de la raiz del repo\n"
        "          (ya esta en .gitignore), o exporta CERES_TOKEN.\n"
        "  CI:     define el secret CERES_TOKEN en el repositorio.\n"
    )
    sys.exit(1)


# ═══════════════════════════════════════════════════════════════════════════
# HTTP
# ═══════════════════════════════════════════════════════════════════════════

class CeresError(Exception):
    """Falla de red o de la API tras agotar los reintentos."""


def api_get(session, path, params=None, retries=RETRIES):
    """GET con backoff exponencial. Nunca incluye el token en el mensaje."""
    url = BASE_URL + path
    last = None
    for attempt in range(1, retries + 1):
        try:
            resp = session.get(url, params=params, timeout=TIMEOUT)
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code in (401, 403):
                raise CeresError(
                    "HTTP %d - el token fue rechazado por Ceres." % resp.status_code
                )
            last = "HTTP %d" % resp.status_code
        except CeresError:
            raise
        except requests.RequestException as exc:
            last = type(exc).__name__
        except ValueError:
            last = "respuesta no era JSON"
        if attempt < retries:
            wait = 2 ** (attempt - 1)
            sys.stderr.write("    reintento %d/%d en %ds (%s)\n"
                             % (attempt, retries - 1, wait, last))
            time.sleep(wait)
    raise CeresError(last or "sin respuesta")


# ═══════════════════════════════════════════════════════════════════════════
# Endpoint 1 - vuelos
# ═══════════════════════════════════════════════════════════════════════════

def list_flights(session):
    """Semanas ISO con vuelo. Solo level 0: level 1 son subsemanas y duplican."""
    path = "/admin_groups/weeks/%s/%s/" % (
        USER_ID, urllib.parse.quote(ADMIN_GROUP, safe="")
    )
    raw = api_get(session, path)
    if isinstance(raw, list):
        weeks = raw
    else:
        weeks = raw.get("results") or raw.get("weeks") or []

    flights = []
    for wk in weeks:
        if not isinstance(wk, dict):
            continue
        if wk.get("level") != 0:
            continue
        key = wk.get("key")
        dates = [d for d in (wk.get("capture_dates") or []) if d]
        if not key or not dates:
            continue
        flights.append({
            "week_key": key,
            "date": max(dates),
            "capture_dates": sorted(dates),
        })

    flights.sort(key=lambda f: (f["date"], f["week_key"]))
    return flights


def season_of(date_str):
    """Temporada jul-jun en el formato de la casa: "2025-26"."""
    year, month = int(date_str[0:4]), int(date_str[5:7])
    start = year if month >= 7 else year - 1
    return "%d-%02d" % (start, (start + 1) % 100)


# ═══════════════════════════════════════════════════════════════════════════
# Endpoint 2 - valores de un vuelo
# ═══════════════════════════════════════════════════════════════════════════

def flight_summary(session, week_key, grid_type_id):
    rows = api_get(session, "/tables/flight_summary/", params={
        "admin_group": ADMIN_GROUP,
        "week": week_key,
        "grid_type_id": grid_type_id,
    })
    if isinstance(rows, list):
        return rows
    return rows.get("results") or []


# ═══════════════════════════════════════════════════════════════════════════
# Endpoint 3 - umbrales publicados
# ═══════════════════════════════════════════════════════════════════════════

# flight_summary devuelve overlays "flacos": overlay_type, value, color, area,
# plants, capture_date. El colorMap con los cortes oficiales NO viene ahi: vive
# en download_urls de /api/overlays/. Por eso los umbrales se piden aparte.
OVERLAYS_PAGE_CAP = 12


def fetch_colormaps(session, warn):
    """
    Recorre /api/overlays/ hasta juntar el colorMap de los 5 indicadores.
    Devuelve {overlay_type: [{"min":.., "max":..}]}. Corta en cuanto los tiene
    todos: la cuenta tiene cientos de overlays y no hace falta paginarlos.
    """
    found = {}
    params = {"admin_group": ADMIN_GROUP, "ordering": "-capture_date"}
    page = 1
    while page <= OVERLAYS_PAGE_CAP:
        # La primera pagina va sin `page`: si el endpoint no estuviera paginado,
        # mandar el parametro podria hacerlo fallar sin necesidad.
        query = dict(params) if page == 1 else dict(params, page=page)
        try:
            payload = api_get(session, "/overlays/", params=query)
        except CeresError as exc:
            warn("no se pudo leer /overlays/ pagina %d (%s); los umbrales que "
                 "falten quedan sin banda." % (page, exc))
            break

        if isinstance(payload, list):
            rows, has_next = payload, False
        else:
            rows = payload.get("results") or []
            has_next = bool(payload.get("next"))

        for overlay in rows:
            otype = overlay.get("overlay_type")
            if not otype or otype in found or otype not in PARAMS:
                continue
            bands = extract_colormap(overlay)
            if bands:
                found[otype] = bands

        missing = [p for p in PARAMS if p not in found]
        if not missing:
            break
        if not rows or not has_next:
            break
        page += 1

    print("  umbrales: %d/%d indicadores con colorMap%s"
          % (len(found), len(PARAMS), "" if len(found) == len(PARAMS)
             else " (faltan: %s)" % ", ".join(p for p in PARAMS if p not in found)))
    return found


def inspect_overlays(session):
    """
    Diagnostico: imprime la forma de los overlays de /api/overlays/ para poder
    ajustar el parseo si Ceres cambia el esquema. Nunca imprime una URL
    completa (pueden venir firmadas): solo host, path y NOMBRES de parametros.
    El unico valor que muestra es colorMap, que son umbrales, no una credencial.
    """
    payload = api_get(session, "/overlays/", params={
        "admin_group": ADMIN_GROUP, "ordering": "-capture_date",
    })
    rows = payload if isinstance(payload, list) else (payload.get("results") or [])
    if not isinstance(payload, list):
        print("paginado: count=%r next=%s" % (payload.get("count"),
                                             bool(payload.get("next"))))
    print("overlays en la primera pagina: %d" % len(rows))

    tipos = {}
    for overlay in rows:
        tipos.setdefault(overlay.get("overlay_type"), 0)
        tipos[overlay.get("overlay_type")] += 1
    print("overlay_type presentes: %s" % json.dumps(tipos, ensure_ascii=False, indent=2))

    shown = set()
    for overlay in rows:
        otype = overlay.get("overlay_type")
        if otype not in PARAMS or otype in shown:
            continue
        shown.add(otype)
        print("")
        print("── %s ─────────────────────────────────" % otype)
        print("  claves del overlay: %s" % sorted(overlay.keys()))
        urls = overlay.get("download_urls")
        print("  download_urls es %s" % type(urls).__name__)
        cands = []
        if isinstance(urls, dict):
            print("  sus claves: %s" % sorted(urls.keys()))
            cands = [(k, v) for k, v in urls.items() if isinstance(v, str)]
        elif isinstance(urls, list):
            cands = [(i, v) for i, v in enumerate(urls) if isinstance(v, str)]
        elif isinstance(urls, str):
            cands = [("(str)", urls)]
        for name, url in cands:
            parsed = urllib.parse.urlparse(url)
            qs = urllib.parse.parse_qs(parsed.query)
            print("    [%s] %s%s  params=%s" % (name, parsed.netloc, parsed.path,
                                                sorted(qs.keys())))
            if "colorMap" in qs:
                print("       colorMap = %s" % qs["colorMap"][0])
        bands = extract_colormap(overlay)
        print("  -> extract_colormap: %s" % (bands if bands else "NADA"))
        if len(shown) >= len(PARAMS):
            break

    audit_colormaps(session)


def audit_colormaps(session):
    """
    Recorre TODOS los overlays y junta los colorMap distintos por overlay_type.
    Contesta la pregunta que decide la politica de bandas: los cortes de un
    indicador son fijos, o cambian de vuelo en vuelo?

      1 colorMap distinto  -> escala fija; los cortes son candidatos a umbral.
      N colorMaps distintos -> se recalculan por vuelo; NO son umbrales, porque
                               el mismo valor caeria en categorias distintas
                               segun que vuelo definio la escala.
    """
    print("")
    print("=" * 70)
    print("AUDITORIA: cuantos colorMap distintos publica cada indicador")
    print("=" * 70)

    vistos = {}          # overlay_type -> {firma: [fechas]}
    page, total = 1, 0
    while page <= OVERLAYS_PAGE_CAP:
        query = {"admin_group": ADMIN_GROUP, "ordering": "-capture_date"}
        if page > 1:
            query["page"] = page
        try:
            payload = api_get(session, "/overlays/", params=query)
        except CeresError as exc:
            print("  (corte en la pagina %d: %s)" % (page, exc))
            break
        if isinstance(payload, list):
            rows, has_next = payload, False
        else:
            rows = payload.get("results") or []
            has_next = bool(payload.get("next"))
        total += len(rows)
        for overlay in rows:
            otype = overlay.get("overlay_type")
            if otype not in PARAMS:
                continue
            bands = extract_colormap(overlay)
            if not bands:
                continue
            firma = json.dumps([[b["min"], b["max"]] for b in bands])
            vistos.setdefault(otype, {}).setdefault(firma, []).append(
                overlay.get("capture_date") or "?")
        if not rows or not has_next:
            break
        page += 1

    print("overlays revisados: %d" % total)
    for otype in PARAMS:
        firmas = vistos.get(otype) or {}
        if not firmas:
            print("")
            print("  %-27s ningun colorMap publicado" % otype)
            continue
        print("")
        print("  %-27s %d colorMap distinto(s) en %d overlays"
              % (otype, len(firmas), sum(len(v) for v in firmas.values())))
        for firma, fechas in sorted(firmas.items(), key=lambda kv: -len(kv[1])):
            cortes = [round(x[0], 4) for x in json.loads(firma)]
            cortes.append(round(json.loads(firma)[-1][1], 4))
            print("      %d overlays  cortes=%s" % (len(fechas), cortes))
            print("                  fechas=%s%s"
                  % (", ".join(sorted(set(fechas))[:4]),
                     " ..." if len(set(fechas)) > 4 else ""))
        print("      => %s" % verdict(firmas))


def verdict(firmas):
    """
    Veredicto sobre un conjunto de colorMap observados.

    Un test de "uno contra muchos" es demasiado grosero: absolute_ndvi publica
    dos firmas que difieren SOLO en el piso del primer tramo (-1.0 vs 0.0) y
    tienen los cortes interiores identicos, o sea es una escala fija con una
    variante cosmetica, no una escala recalculada. Por eso se comparan tambien
    los cortes interiores, y se marca por separado la uniformidad: una rampa de
    tramos iguales es una eleccion de despliegue, no una clasificacion.
    """
    n_over = sum(len(v) for v in firmas.values())
    cortes = [json.loads(f) for f in firmas]

    # Cortes interiores: los extremos del rango son los que suelen variar.
    interiores = {tuple(round(x[0], 4) for x in c[1:]) for c in cortes}

    if len(firmas) == 1:
        base = "ESCALA FIJA"
    elif len(interiores) == 1:
        base = ("ESCALA FIJA con %d variantes solo en los extremos" % len(firmas))
    elif len(firmas) >= n_over * 0.5:
        return ("SE RECALCULA POR OVERLAY (%d firmas en %d overlays) -> "
                "no son umbrales: el mismo valor cambiaria de categoria segun "
                "el campo y el vuelo" % (len(firmas), n_over))
    else:
        return ("VARIA ENTRE VUELOS (%d firmas) -> no son umbrales"
                % len(firmas))

    # Escala fija: falta decidir si es clasificacion o rampa de despliegue.
    anchos = [round(x[1] - x[0], 4) for x in cortes[0]]
    interior = anchos[1:-1] if len(anchos) > 2 else anchos
    uniforme = len(set(interior)) == 1
    if uniforme and len(anchos) > 5:
        return ("%s, pero es una RAMPA de %d tramos iguales de %s -> eleccion de "
                "despliegue, no umbrales agronomicos"
                % (base, len(anchos), interior[0]))
    return "%s -> los cortes son candidatos a umbral" % base


def unit_key(row, level):
    """
    Clave de la unidad. A nivel sector es block_name, que ya viene exactamente
    como el output de riegoKey(): "E1-S3". Cero mapeo, cero string matching
    contra el "E1 - S1" del GEOJSON.
    """
    block = (row.get("block_name") or "").strip()
    if level == "sectors":
        return block or None
    if block and block.upper().startswith("E") and "-" not in block:
        return block.upper()
    return FIELD_TO_EQUIPO.get(row.get("field_id"))


def extract_colormap(overlay):
    """
    Las bandas oficiales del indicador viajan URL-encoded en el parametro
    colorMap de alguna de las URLs de download_urls. Formato [color, min, max].
    Devuelve [{"min":.., "max":..}] ordenado ascendente, o None.
    """
    urls = overlay.get("download_urls")
    candidates = []
    if isinstance(urls, dict):
        candidates = [v for v in urls.values() if isinstance(v, str)]
    elif isinstance(urls, list):
        for item in urls:
            if isinstance(item, str):
                candidates.append(item)
            elif isinstance(item, dict):
                candidates.extend(v for v in item.values() if isinstance(v, str))
    elif isinstance(urls, str):
        candidates = [urls]

    for url in candidates:
        query = urllib.parse.urlparse(url).query
        raw = urllib.parse.parse_qs(query).get("colorMap")
        if not raw:
            continue
        try:
            parsed = json.loads(raw[0])
        except (ValueError, IndexError):
            continue
        bands = []
        for entry in parsed:
            if not isinstance(entry, (list, tuple)) or len(entry) < 3:
                continue
            try:
                lo, hi = float(entry[1]), float(entry[2])
            except (TypeError, ValueError):
                continue
            if hi < lo:
                lo, hi = hi, lo
            bands.append({"min": lo, "max": hi})
        if bands:
            bands.sort(key=lambda b: b["min"])
            return bands
    return None


# ═══════════════════════════════════════════════════════════════════════════
# Bandas -> params
# ═══════════════════════════════════════════════════════════════════════════

def label_bands(raw_bands, higher_is_better, labels_spec=None):
    """
    Ordena las bandas por valor ascendente y les cuelga severidad, codigo de
    estado y etiqueta es/en. severity 0 = mejor: si el indicador es "alto =
    peor", la severidad crece con el valor; si es "alto = mejor", decrece. El
    mapa pinta por severidad, no por el orden del arreglo.

    labels_spec:
      None            -> etiquetas genericas por cantidad de bandas
      "ranges"        -> el rango numerico como etiqueta ("<0,50", "0,50–0,55")
      <clave>         -> etiquetas de la plataforma, indexadas por severidad
    """
    bands = sorted(raw_bands, key=lambda b: b["min"])
    n = len(bands)
    ladder = STATUS_LADDER.get(n)

    # Etiquetas por severidad. Las de rango se resuelven por posicion de valor,
    # no por severidad, asi que se calculan aparte.
    by_severity = None
    by_value = None
    if labels_spec == "ranges":
        cuts = [b["min"] for b in bands] + [bands[-1]["max"]]
        by_value = range_labels(cuts)
    elif labels_spec == "relative":
        # Por posicion de valor, no por severidad: "1 - Mas bajo" es el valor mas
        # bajo del vuelo, cualquiera sea la direccion del indicador.
        by_value = relative_labels(n)
    elif labels_spec and labels_spec in PLATFORM_LABELS:
        cand = PLATFORM_LABELS[labels_spec]
        if len(cand) == n:
            by_severity = cand
    if by_severity is None and by_value is None:
        by_severity = BAND_LABELS.get(n)

    out = []
    for idx, band in enumerate(bands):
        severity = (n - 1 - idx) if higher_is_better else idx
        # El status solo existe para clasificar en compliance; con mas bandas que
        # la escalera de estados se cae a un codigo numerico, que igual es unico.
        status = ladder[severity] if ladder else ("b%d" % severity)
        if by_value is not None:
            es, en = by_value[idx]
        elif by_severity is not None:
            es, en = by_severity[severity]
        else:
            es = en = "%s - %s" % (band["min"], band["max"])
        out.append(OrderedDict([
            ("min", round(band["min"], 6)),
            ("max", round(band["max"], 6)),
            ("es", es),
            ("en", en),
            ("status", status),
            ("severity", severity),
        ]))
    return out


def bands_from_cuts(cuts):
    return [{"min": cuts[i], "max": cuts[i + 1]} for i in range(len(cuts) - 1)]


def quantile_cuts(values, n_classes):
    """
    Cortes por cuantiles sobre los valores de UN vuelo. Devuelve n_classes+1
    limites. Si hay empates y un corte se repite, se colapsa: es mejor entregar
    menos clases que dos bandas de ancho cero.
    """
    vals = sorted(v for v in values if v is not None)
    if len(vals) < 2:
        return None
    lo, hi = vals[0], vals[-1]
    if hi <= lo:
        return None
    cuts = [lo]
    for i in range(1, n_classes):
        pos = (len(vals) - 1) * i / float(n_classes)
        lower = int(pos)
        frac = pos - lower
        upper = min(lower + 1, len(vals) - 1)
        cuts.append(vals[lower] + (vals[upper] - vals[lower]) * frac)
    cuts.append(hi)
    out = [cuts[0]]
    for c in cuts[1:]:
        if c > out[-1] + 1e-9:
            out.append(c)
    return out if len(out) >= 3 else None


def compute_relative_bands(flights, params, warn):
    """
    Para los indicadores relativos, los cortes se recalculan en CADA vuelo y por
    nivel, igual que hace la plataforma. Van dentro del vuelo y no del indicador,
    porque no son los mismos entre fechas.
    """
    rel = [p for p in params if p.get("bands_source") == "relative"]
    if not rel:
        return
    meta = {pid: PARAMS[pid] for pid in PARAMS}
    for flight in flights:
        out = OrderedDict()
        for param in rel:
            pid = param["id"]
            per_level = OrderedDict()
            for level in ("sectors", "equipos"):
                vals = [v.get(pid) for v in (flight.get(level) or {}).values()]
                vals = [v for v in vals if v is not None]
                cuts = quantile_cuts(vals, meta[pid].get("n_classes", 4))
                if not cuts:
                    continue
                per_level[level] = label_bands(
                    bands_from_cuts(cuts), meta[pid]["higher_is_better"],
                    meta[pid].get("labels"))
            if per_level:
                out[pid] = per_level
            else:
                warn("vuelo %s / %s: no se pudieron calcular clases relativas "
                     "(valores insuficientes o todos iguales)."
                     % (flight["week_key"], pid))
        flight["relative_bands"] = out


def load_overrides():
    if not os.path.isfile(OVERRIDES_PATH):
        return {}
    try:
        with open(OVERRIDES_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        sys.stderr.write("ADVERTENCIA: ceres_thresholds.json ilegible (%s); se "
                         "usan las bandas de Ceres.\n" % exc)
        return {}
    out = {}
    for pid, cfg in (data or {}).items():
        bands = (cfg or {}).get("bands")
        if not isinstance(bands, list) or not bands:
            continue
        clean = []
        for band in bands:
            try:
                clean.append({"min": float(band["min"]), "max": float(band["max"])})
            except (KeyError, TypeError, ValueError):
                clean = []
                break
        if clean:
            out[pid] = clean
    return out


def build_params(colormaps, overrides, warn, value_ranges=None):
    """
    Arma params[]. Un override de ceres_thresholds.json siempre gana: es la via
    para que agronomia habilite un indicador que Ceres deja sin umbrales.
    """
    value_ranges = value_ranges or {}
    params = []
    for pid, meta in PARAMS.items():
        policy = meta.get("bands_policy", "unclassified")
        reason_es = reason_en = None

        if pid in overrides:
            raw, source = overrides[pid], "custom"
        elif policy == "fixed":
            # Cortes definidos por agronomia, fijos y comparables entre vuelos.
            raw, source = bands_from_cuts(meta["cuts"]), "agronomia"
        elif policy == "relative":
            # Los cortes no viven aca: se recalculan por vuelo en
            # compute_relative_bands(). El indicador queda marcado y sin bandas
            # propias, y el mapa las toma del vuelo activo.
            raw, source = [], "relative"
        elif policy == "ceres" and pid in colormaps:
            raw, source = colormaps[pid], "ceres"
        elif policy == "ceres":
            raw, source = [], "unclassified"
            reason_es = "No se pudo leer el colorMap de Ceres."
            reason_en = "Could not read the Ceres colorMap."
            warn("`%s` tiene politica `ceres` pero no se pudo extraer su "
                 "colorMap: queda sin clasificar." % pid)
        elif policy.startswith("share:"):
            # Toma prestados los cortes publicados de otro indicador que mide la
            # MISMA magnitud. La procedencia queda escrita en bands_source, para
            # que se pueda auditar de donde salio cada banda.
            donor = policy.split(":", 1)[1]
            if donor in overrides:
                raw, source = overrides[donor], "custom:" + donor
            elif donor in colormaps:
                raw, source = colormaps[donor], "ceres:" + donor
            else:
                raw, source = [], "unclassified"
                reason_es = "No se pudo leer el colorMap de %s, del que toma sus cortes." % donor
                reason_en = "Could not read the colorMap of %s, whose cuts it borrows." % donor
                warn("`%s` toma sus cortes de `%s`, pero no se pudo extraer ese "
                     "colorMap: queda sin clasificar." % (pid, donor))
        else:
            raw, source = [], "unclassified"
            reason_es = meta.get("why_es")
            reason_en = meta.get("why_en")

        bands = label_bands(raw, meta["higher_is_better"], meta.get("labels")) if raw else []

        # Rango del eje: con bandas manda la banda; sin bandas, el rango real de
        # los datos, para que los graficos tengan un eje utilizable igual.
        if bands:
            lo = min(b["min"] for b in bands)
            hi = max(b["max"] for b in bands)
        elif pid in value_ranges:
            lo, hi = value_ranges[pid]
        else:
            lo, hi = 0.0, 1.0

        entry = OrderedDict([
            ("id", pid),
            ("es", meta["es"]),
            ("en", meta["en"]),
            ("desc_es", meta.get("desc_es", "")),
            ("desc_en", meta.get("desc_en", "")),
            ("group_es", meta.get("group_es", "")),
            ("group_en", meta.get("group_en", "")),
            ("min", round(lo, 6)),
            ("max", round(hi, 6)),
            ("higher_is_better", meta["higher_is_better"]),
            ("bands", bands),
            ("bands_source", source),
        ])
        if source == "unclassified":
            entry["unclassified_es"] = reason_es or "Sin umbrales definidos."
            entry["unclassified_en"] = reason_en or "No thresholds defined."
            # Cuantas bandas publico Ceres y se descartaron. Queda anotado para
            # que se note si algun dia Ceres empieza a publicar umbrales reales.
            entry["ceres_bands_found"] = len(colormaps.get(pid) or [])
        if source == "relative":
            # El mapa tiene que decirlo: dos vuelos no son comparables entre si.
            entry["relative_es"] = meta.get("relative_es", "")
            entry["relative_en"] = meta.get("relative_en", "")
            entry["n_classes"] = meta.get("n_classes", 4)
        params.append(entry)
    return params


# ═══════════════════════════════════════════════════════════════════════════
# Derivados: deltas y cumplimiento
# ═══════════════════════════════════════════════════════════════════════════

def band_of(param, value):
    """Banda que contiene el valor. El ultimo tramo incluye su tope."""
    bands = param.get("bands") or []
    if value is None or not bands:
        return None
    for i, band in enumerate(bands):
        last = (i == len(bands) - 1)
        if band["min"] <= value < band["max"] or (last and value <= band["max"]):
            return band
    return bands[-1] if value > bands[-1]["max"] else bands[0]


def compute_deltas(flights):
    """Diferencia contra el vuelo anterior, por unidad e indicador."""
    for i, flight in enumerate(flights):
        deltas = OrderedDict([("sectors", OrderedDict()), ("equipos", OrderedDict())])
        if i > 0:
            prev = flights[i - 1]
            for level in ("sectors", "equipos"):
                for unit in flight[level]:
                    now = flight[level][unit]
                    before = prev[level].get(unit) or {}
                    diff = OrderedDict()
                    for pid in PARAMS:
                        if pid in now and pid in before:
                            diff[pid] = round(now[pid] - before[pid], 4)
                    if diff:
                        deltas[level][unit] = diff
        flight["deltas"] = deltas


def compute_compliance(flights, params, sectors_meta, equipos_meta, warn):
    """
    Cuantas unidades, cuanta superficie y cuantas plantas caen en cada banda.
    Precalculado aca: el navegador no debe contar nada.

    Se calcula para los DOS niveles. El mapa pinta 5 equipos o 23 sectores segun
    el toggle, y si solo existiera el cumplimiento por sector, al mirar equipos la
    leyenda diria "22 de 23" sobre un mapa de 5 poligonos.
    """
    for flight in flights:
        flight["compliance"] = _compliance_for(
            flight, "sectors", params, sectors_meta, N_SECTORES, warn)
        flight["compliance_equipos"] = _compliance_for(
            flight, "equipos", params, equipos_meta, N_EQUIPOS, warn)


def flight_bands(flight, param, level):
    """
    Bandas vigentes para un indicador en un vuelo y nivel. Los indicadores
    relativos las tienen dentro del vuelo, porque cambian vuelo a vuelo; el
    resto las tiene en el propio indicador.
    """
    if param.get("bands_source") == "relative":
        rel = (flight.get("relative_bands") or {}).get(param["id"]) or {}
        return rel.get(level) or []
    return param.get("bands") or []


def _compliance_for(flight, level, params, meta_by_unit, expected, warn):
    out = OrderedDict()
    for param in params:
        bands = flight_bands(flight, param, level)
        if not bands:
            continue
        statuses = [b["status"] for b in bands]
        by_unit = OrderedDict((s, 0) for s in statuses)
        by_area = OrderedDict((s, 0) for s in statuses)
        by_plants = OrderedDict((s, 0) for s in statuses)
        classified = 0
        for unit, values in (flight.get(level) or {}).items():
            band = band_of({"bands": bands}, values.get(param["id"]))
            if band is None:
                continue
            meta = meta_by_unit.get(unit) or {}
            by_unit[band["status"]] += 1
            by_area[band["status"]] += int(meta.get("area_m2") or 0)
            by_plants[band["status"]] += int(meta.get("plants") or 0)
            classified += 1
        if classified and classified != expected:
            warn("vuelo %s / %s / %s: %d unidades clasificadas, se esperaban %d."
                 % (flight["week_key"], level, param["id"], classified, expected))
        # La clave de conteo se llama by_sector en el nivel sector, que es el
        # nombre que ya documenta el esquema; en equipos, by_equipo.
        out[param["id"]] = OrderedDict([
            ("by_sector" if level == "sectors" else "by_equipo", by_unit),
            ("by_area_m2", by_area),
            ("by_plants", by_plants),
        ])
    return out


# ═══════════════════════════════════════════════════════════════════════════
# Descarga de un vuelo
# ═══════════════════════════════════════════════════════════════════════════

def fetch_flight(session, flight, colormaps, meta_sink, warn):
    """
    Devuelve {"sectors": {...}, "equipos": {...}} con los valores nativos de
    cada nivel. Los sectores NO se promedian para obtener el equipo: Ceres
    agrega sobre los pixeles y un promedio de promedios no es lo mismo.
    """
    result = {"sectors": OrderedDict(), "equipos": OrderedDict()}
    levels = (("sectors", GRID_TYPE_SECTORES), ("equipos", GRID_TYPE_EQUIPOS))
    for level, grid in levels:
        rows = flight_summary(session, flight["week_key"], grid)
        for row in rows:
            key = unit_key(row, level)
            if not key:
                warn("vuelo %s / %s: fila sin unidad reconocible (field_id=%r, "
                     "block_name=%r); se ignora."
                     % (flight["week_key"], level, row.get("field_id"),
                        row.get("block_name")))
                continue
            values = OrderedDict()
            area = plants = None
            for overlay in (row.get("overlays") or []):
                otype = overlay.get("overlay_type")
                if not otype or otype in IGNORED_OVERLAYS or otype not in PARAMS:
                    continue
                if otype not in colormaps:
                    bands = extract_colormap(overlay)
                    if bands:
                        colormaps[otype] = bands
                val = overlay.get("value")
                if val is None:
                    continue      # sin dato: se omite la clave, no va null
                try:
                    values[otype] = round(float(val), 4)
                except (TypeError, ValueError):
                    continue
                if overlay.get("area") is not None:
                    try:
                        area = max(area or 0.0, float(overlay["area"]))
                    except (TypeError, ValueError):
                        pass
                if overlay.get("plants") is not None:
                    try:
                        plants = max(plants or 0, int(overlay["plants"]))
                    except (TypeError, ValueError):
                        pass
            if values:
                result[level][key] = values
            # La metadata (superficie, plantas) se guarda con la fecha del vuelo:
            # despues gana el vuelo mas reciente que la traiga.
            if area or plants:
                sink = meta_sink[level].setdefault(key, {})
                if flight["date"] >= sink.get("_date", ""):
                    sink["_date"] = flight["date"]
                    if area:
                        sink["area_m2"] = int(round(area))
                    if plants:
                        sink["plants"] = int(plants)
                    if row.get("field_id") is not None:
                        sink["field_id"] = row.get("field_id")

        got = len(result[level])
        want = N_SECTORES if level == "sectors" else N_EQUIPOS
        if got != want:
            warn("vuelo %s (%s): trae %d %s, se esperaban %d. Revisa si cambio "
                 "la grilla en Ceres antes de confiar en este vuelo."
                 % (flight["week_key"], flight["date"], got, level, want))
    return result


# ═══════════════════════════════════════════════════════════════════════════
# Metadata de unidades
# ═══════════════════════════════════════════════════════════════════════════

def per_ha(plants, area_m2):
    if not plants or not area_m2:
        return None
    return round(plants / (area_m2 / 10000.0), 1)


def parse_sector_key(key):
    eq, sec = key.split("-", 1)
    return int(eq.lstrip("Ee")), int(sec.lstrip("Ss"))


def sector_sort(key):
    try:
        return parse_sector_key(key)
    except (ValueError, IndexError):
        return (99, 99)


def equipo_num(key):
    try:
        return int(key.lstrip("Ee"))
    except ValueError:
        return 99


def build_unit_meta(meta_sink):
    sectors, equipos = OrderedDict(), OrderedDict()

    for key in sorted(meta_sink["sectors"], key=sector_sort):
        raw = meta_sink["sectors"][key]
        try:
            eq, sec = parse_sector_key(key)
        except (ValueError, IndexError):
            eq = sec = None
        sectors[key] = OrderedDict([
            ("equipo", eq),
            ("sector", sec),
            ("field_id", raw.get("field_id")),
            ("area_m2", raw.get("area_m2")),
            ("plants", raw.get("plants")),
            ("plants_per_ha", per_ha(raw.get("plants"), raw.get("area_m2"))),
        ])

    for key in sorted(meta_sink["equipos"], key=equipo_num):
        raw = meta_sink["equipos"][key]
        eq = equipo_num(key)
        n_sec = sum(1 for k in sectors if sectors[k]["equipo"] == eq)
        equipos[key] = OrderedDict([
            ("equipo", eq),
            ("field_id", raw.get("field_id")),
            ("n_sectores", n_sec),
            ("area_m2", raw.get("area_m2")),
            ("plants", raw.get("plants")),
            ("plants_per_ha", per_ha(raw.get("plants"), raw.get("area_m2"))),
        ])
    return sectors, equipos


def seed_meta_from_existing(existing, meta_sink):
    """
    Reinyecta la metadata de unidades de un archivo previo, para que una corrida
    incremental no pierda area_m2 / plants de los vuelos que no volvio a pedir.
    Lo que trajo esta corrida (marcado con _date) siempre gana.
    """
    for level in ("sectors", "equipos"):
        source = (existing or {}).get(level) or {}
        for key, meta in source.items():
            sink = meta_sink[level].setdefault(key, {})
            if "_date" in sink:
                continue
            for field in ("area_m2", "plants", "field_id"):
                if meta.get(field) is not None and sink.get(field) is None:
                    sink[field] = meta[field]


# ═══════════════════════════════════════════════════════════════════════════
# Escritura
# ═══════════════════════════════════════════════════════════════════════════

def read_existing(path):
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh, object_pairs_hook=OrderedDict)
    except (OSError, ValueError) as exc:
        sys.stderr.write("ADVERTENCIA: %s ilegible (%s); se rehace completo.\n"
                         % (os.path.basename(path), exc))
        return None


def dump(payload):
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def bump_data_version(latest_date):
    """
    Agrega/actualiza la clave "ceres" de data-version.json con la fecha del
    vuelo mas reciente, dejando el resto de las claves intactas.
    """
    try:
        with open(DATA_VERSION_PATH, "r", encoding="utf-8") as fh:
            versions = json.load(fh, object_pairs_hook=OrderedDict)
    except (OSError, ValueError) as exc:
        sys.stderr.write("ADVERTENCIA: no se pudo leer data-version.json (%s); "
                         "no se bumpeo la version.\n" % exc)
        return False
    if versions.get("ceres") == latest_date:
        return False
    versions["ceres"] = latest_date
    with open(DATA_VERSION_PATH, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(versions, ensure_ascii=False, indent=2) + "\n")
    return True


def order_units(units, keyfn):
    return OrderedDict((k, units[k]) for k in sorted(units, key=keyfn))


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def summarize(flights, params, sectors_meta, equipos_meta, failed, warnings):
    print("")
    print("-- Resumen ------------------------------------------------")
    print("Vuelos:      %d  (%s -> %s)"
          % (len(flights), flights[0]["date"], flights[-1]["date"]))
    print("Unidades:    %d sectores / %d equipos" % (len(sectors_meta), len(equipos_meta)))
    banded = [p for p in params
              if p.get("bands") or p.get("bands_source") == "relative"]
    print("Indicadores: %d de %d clasificados" % (len(banded), len(params)))
    for p in params:
        n = len(p.get("bands") or [])
        if p.get("bands_source") == "relative":
            rb = ((flights[-1].get("relative_bands") or {}).get(p["id"]) or {}).get("sectors") or []
            print("               %-27s %d clases relativas por vuelo  [relative]"
                  % (p["id"], len(rb)))
            continue
        line = "               %-27s %d bandas  [%s]" % (p["id"], n, p.get("bands_source"))
        if p.get("bands_source") == "unclassified":
            line += "  <- %s" % p.get("unclassified_es", "")
            if p.get("ceres_bands_found"):
                line += " (Ceres publica %d tramos)" % p["ceres_bands_found"]
        print(line)
    if len(banded) < len(params):
        print("")
        print("  Los indicadores sin clasificar se muestran en gris en el mapa, con")
        print("  su valor y su evolucion, pero sin banda de color. Para habilitarlos,")
        print("  agrega sus cortes a ceres_thresholds.json (ver el encabezado).")
    print("Vuelos por fecha:")
    for flight in flights:
        print("  %-11s %s  %-8s  %2d sectores / %d equipos"
              % (flight["week_key"], flight["date"], flight["season"],
                 len(flight["sectors"]), len(flight["equipos"])))
    if failed:
        print("Vuelos fallidos: %s" % ", ".join(failed))
    if warnings:
        print("")
        print("%d advertencia(s) - revisalas antes de commitear." % len(warnings))
    else:
        print("")
        print("Sin advertencias.")


# ═══════════════════════════════════════════════════════════════════════════
# main
# ═══════════════════════════════════════════════════════════════════════════

def main():
    ap = argparse.ArgumentParser(
        description="Descarga los vuelos de Ceres Imaging de San Gerardo.")
    ap.add_argument("--full", action="store_true",
                    help="refetch completo: ignora los vuelos ya guardados")
    ap.add_argument("--out", default=OUT_DEFAULT,
                    help="ruta de salida (default: ceres_data.json en la raiz)")
    ap.add_argument("--inspect-overlays", action="store_true",
                    help="diagnostico: imprime la forma de /api/overlays/ para "
                         "ajustar el parseo del colorMap. No escribe nada.")
    ap.add_argument("--check-token", action="store_true",
                    help="diagnostico: valida el token con una sola llamada y "
                         "describe su FORMA (largo, espacios, prefijo) sin "
                         "imprimir el valor. Sirve para depurar el secret de CI.")
    args = ap.parse_args()

    warnings = []

    def warn(msg):
        warnings.append(msg)
        sys.stderr.write("  !  %s\n" % msg)

    token = read_token()

    if args.check_token:
        # Describe la FORMA del token, nunca su valor: alcanza para distinguir un
        # pegado truncado, uno con el prefijo "Token " adentro o uno con un salto
        # de linea en medio, que son las tres formas tipicas de romper un secret.
        raw = os.environ.get("CERES_TOKEN")
        print("origen:            %s" % ("variable de entorno CERES_TOKEN" if raw
                                         else "archivo .ceres_token"))
        print("largo:             %d caracteres" % len(token))
        print("espacios internos: %s" % ("SI - probablemente se pego mal"
                                         if any(c.isspace() for c in token) else "no"))
        print("empieza con Token: %s" % ("SI - sobra el prefijo, va solo el valor"
                                         if token.lower().startswith("token") else "no"))
        print("solo ASCII:        %s" % ("si" if all(ord(c) < 128 for c in token)
                                         else "NO - hay caracteres raros"))
        if raw is not None and raw != raw.strip():
            print("ADVERTENCIA:       venia con espacios al principio o al final "
                  "(se recortaron)")
        sys.stdout.flush()
        s = requests.Session()
        s.headers.update({"Authorization": "Token %s" % token,
                          "Accept": "application/json"})
        url = BASE_URL + "/admin_groups/weeks/%s/%s/" % (
            USER_ID, urllib.parse.quote(ADMIN_GROUP, safe=""))
        try:
            resp = s.get(url, timeout=TIMEOUT)
        except requests.RequestException as exc:
            print("resultado:         fallo de red (%s)" % type(exc).__name__)
            return 1
        print("resultado:         HTTP %d" % resp.status_code)
        if resp.status_code == 200:
            print("")
            print("El token es valido.")
            return 0
        if resp.status_code in (401, 403):
            print("")
            print("Ceres rechazo el token. Con el largo de arriba se distingue si")
            print("el pegado quedo incompleto o si el valor ya no sirve.")
        return 1

    session = requests.Session()
    session.headers.update({
        "Authorization": "Token %s" % token,
        "Accept": "application/json",
        "User-Agent": "san-gerardo-map/fetch_ceres",
    })

    if args.inspect_overlays:
        try:
            inspect_overlays(session)
        except CeresError as exc:
            sys.stderr.write("ERROR: no se pudo leer /overlays/ (%s).\n" % exc)
            return 1
        return 0

    existing = None if args.full else read_existing(args.out)
    known = {}
    if existing:
        for flight in existing.get("flights") or []:
            if flight.get("week_key"):
                known[flight["week_key"]] = flight
        print("Incremental: %d vuelos ya en %s."
              % (len(known), os.path.basename(args.out)))
    else:
        print("Refetch completo." if args.full else "Sin archivo previo: descarga completa.")

    print("Listando vuelos...")
    try:
        catalog = list_flights(session)
    except CeresError as exc:
        sys.stderr.write("ERROR: no se pudo listar los vuelos (%s).\n" % exc)
        return 1
    print("  %d vuelos en el historico (level 0)." % len(catalog))

    pending = [f for f in catalog if f["week_key"] not in known]
    print("  %d por descargar." % len(pending))

    # Los umbrales se piden antes que los vuelos, y de /overlays/: los overlays
    # de flight_summary vienen sin download_urls, asi que ahi no hay colorMap.
    print("Leyendo umbrales publicados...")
    colormaps = fetch_colormaps(session, warn)
    meta_sink = {"sectors": {}, "equipos": {}}
    fetched, failed = {}, []

    for i, flight in enumerate(pending, 1):
        print("[%d/%d] %s / %s" % (i, len(pending), flight["week_key"], flight["date"]))
        try:
            fetched[flight["week_key"]] = fetch_flight(
                session, flight, colormaps, meta_sink, warn)
        except CeresError as exc:
            warn("vuelo %s (%s) fallo tras los reintentos (%s); se omite y la "
                 "corrida continua." % (flight["week_key"], flight["date"], exc))
            failed.append(flight["week_key"])

    # Los vuelos que ya estaban en disco reusan su metadata de unidades.
    seed_meta_from_existing(existing, meta_sink)

    overrides = load_overrides()
    if overrides:
        print("  ceres_thresholds.json: bandas propias para %s."
              % ", ".join(sorted(overrides)))

    flights = []
    for entry in catalog:
        wk = entry["week_key"]
        if wk in fetched:
            values = fetched[wk]
        elif wk in known:
            values = {"sectors": known[wk].get("sectors") or OrderedDict(),
                      "equipos": known[wk].get("equipos") or OrderedDict()}
        else:
            continue
        flights.append(OrderedDict([
            ("week_key", wk),
            ("date", entry["date"]),
            ("season", season_of(entry["date"])),
            ("sectors", order_units(values["sectors"], sector_sort)),
            ("equipos", order_units(values["equipos"], equipo_num)),
        ]))

    if not flights:
        sys.stderr.write("ERROR: no quedo ningun vuelo con datos. No se escribe nada.\n")
        return 1

    sectors_meta, equipos_meta = build_unit_meta(meta_sink)

    # Los params se arman recien aca: un indicador sin bandas toma como rango de
    # eje el minimo y maximo reales de sus datos, y eso exige tener los vuelos.
    params = build_params(colormaps, overrides, warn, value_ranges(flights))

    compute_deltas(flights)
    # Las clases relativas se calculan antes del cumplimiento: este las necesita.
    compute_relative_bands(flights, params, warn)
    compute_compliance(flights, params, sectors_meta, equipos_meta, warn)

    payload = OrderedDict([
        ("generated_at", (existing or {}).get("generated_at") or now_iso()),
        ("source", "Ceres Imaging"),
        ("farm", OrderedDict([("name", FARM_NAME), ("customer", CUSTOMER),
                              ("admin_group", ADMIN_GROUP)])),
        ("params", params),
        ("sectors", sectors_meta),
        ("equipos", equipos_meta),
        ("flights", flights),
    ])

    body = dump(payload)
    previous = None
    if os.path.isfile(args.out):
        try:
            with open(args.out, "r", encoding="utf-8") as fh:
                previous = fh.read()
        except OSError:
            previous = None

    if previous == body:
        print("")
        print("Sin cambios: %s ya esta al dia." % os.path.basename(args.out))
        summarize(flights, params, sectors_meta, equipos_meta, failed, warnings)
        return 0

    payload["generated_at"] = now_iso()
    try:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(dump(payload))
    except OSError as exc:
        sys.stderr.write("ERROR: no se pudo escribir %s (%s).\n" % (args.out, exc))
        return 1
    print("")
    print("Escrito %s (%d vuelos)." % (os.path.basename(args.out), len(flights)))

    latest = flights[-1]["date"]
    if bump_data_version(latest):
        print('data-version.json: "ceres" -> %s.' % latest)

    summarize(flights, params, sectors_meta, equipos_meta, failed, warnings)
    return 0


def value_ranges(flights):
    """
    {overlay_type: (min, max)} sobre todos los vuelos y los dos niveles. Es el
    rango del eje para los indicadores que quedan sin clasificar: sin bandas que
    lo definan, el grafico igual necesita un eje que encuadre el dato.
    """
    acc = {}
    for flight in flights:
        for level in ("sectors", "equipos"):
            for values in flight[level].values():
                for pid, val in values.items():
                    lo, hi = acc.get(pid, (val, val))
                    acc[pid] = (min(lo, val), max(hi, val))
    return acc


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.stderr.write("\nInterrumpido.\n")
        sys.exit(130)
