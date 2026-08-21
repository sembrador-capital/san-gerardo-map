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

Los cortes de cada indicador salen del parametro colorMap que Ceres publica en
las URLs de download_urls, por overlay_type. No hay rangos inventados aca.

Para ajustar los cortes al nogal en Chile sin tocar codigo ni mapa, crea
ceres_thresholds.json en la raiz del repo:

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

Sus bandas pisan a las de Ceres para ese indicador y bands_source pasa a
"custom". Las etiquetas (Optimo/Adecuado/Alerta/Critico) y los codigos de estado
se derivan de la cantidad de bandas y de la direccion del indicador; no hace
falta escribirlas.

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

# Los 5 indicadores utiles. colorized_ndvi queda fuera a proposito: duplica
# absolute_ndvi y ocuparia un lugar en el selector sin aportar nada.
PARAMS = OrderedDict([
    ("water_stress", {
        "es": "Estrés hídrico", "en": "Water stress",
        "higher_is_better": False,
    }),
    ("absolute_ndvi", {
        "es": "NDVI", "en": "NDVI",
        "higher_is_better": True,
    }),
    ("season_average_ndvi", {
        "es": "NDVI promedio temporada", "en": "Season avg NDVI",
        "higher_is_better": True,
    }),
    ("chlorophyll_class", {
        "es": "Clorofila", "en": "Chlorophyll",
        "higher_is_better": True,
    }),
    ("cumulative_thermal_stress", {
        "es": "Estrés térmico acumulado", "en": "Cumulative thermal stress",
        "higher_is_better": False,
    }),
])
IGNORED_OVERLAYS = {"colorized_ndvi"}

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

def label_bands(raw_bands, higher_is_better):
    """
    Ordena las bandas por valor ascendente y les cuelga severidad, codigo de
    estado y etiqueta es/en. severity 0 = mejor: si el indicador es "alto =
    peor", la severidad crece con el valor; si es "alto = mejor", decrece. El
    mapa pinta por severidad, no por el orden del arreglo.
    """
    bands = sorted(raw_bands, key=lambda b: b["min"])
    n = len(bands)
    ladder = STATUS_LADDER.get(n)
    labels = BAND_LABELS.get(n)

    out = []
    for idx, band in enumerate(bands):
        severity = (n - 1 - idx) if higher_is_better else idx
        if ladder and labels:
            status = ladder[severity]
            es, en = labels[severity]
        else:
            # Mas de 5 bandas: no hay escalera de estados que alcance. Se cae a
            # codigos numericos y el mapa colorea por severidad igual.
            status = "b%d" % severity
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


def build_params(colormaps, overrides, warn):
    params = []
    for pid, meta in PARAMS.items():
        if pid in overrides:
            raw, source = overrides[pid], "custom"
        elif pid in colormaps:
            raw, source = colormaps[pid], "ceres"
        else:
            warn("no se pudo extraer el colorMap de `%s`: queda sin bandas y el "
                 "mapa lo va a mostrar sin clasificar." % pid)
            raw, source = [], "none"
        bands = label_bands(raw, meta["higher_is_better"]) if raw else []
        lo = min((b["min"] for b in bands), default=0.0)
        hi = max((b["max"] for b in bands), default=1.0)
        params.append(OrderedDict([
            ("id", pid),
            ("es", meta["es"]),
            ("en", meta["en"]),
            ("min", lo),
            ("max", hi),
            ("higher_is_better", meta["higher_is_better"]),
            ("bands", bands),
            ("bands_source", source),
        ]))
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


def compute_compliance(flights, params, sectors_meta, warn):
    """
    Cuantos sectores, cuanta superficie y cuantas plantas caen en cada banda.
    Precalculado aca: el navegador no debe contar nada.
    """
    for flight in flights:
        compliance = OrderedDict()
        for param in params:
            if not param.get("bands"):
                continue
            statuses = [b["status"] for b in param["bands"]]
            by_sector = OrderedDict((s, 0) for s in statuses)
            by_area = OrderedDict((s, 0) for s in statuses)
            by_plants = OrderedDict((s, 0) for s in statuses)
            classified = 0
            for unit, values in flight["sectors"].items():
                band = band_of(param, values.get(param["id"]))
                if band is None:
                    continue
                meta = sectors_meta.get(unit) or {}
                by_sector[band["status"]] += 1
                by_area[band["status"]] += int(meta.get("area_m2") or 0)
                by_plants[band["status"]] += int(meta.get("plants") or 0)
                classified += 1
            if classified and classified != N_SECTORES:
                warn("vuelo %s / %s: %d sectores clasificados, se esperaban %d."
                     % (flight["week_key"], param["id"], classified, N_SECTORES))
            compliance[param["id"]] = OrderedDict([
                ("by_sector", by_sector),
                ("by_area_m2", by_area),
                ("by_plants", by_plants),
            ])
        flight["compliance"] = compliance


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
    banded = [p for p in params if p.get("bands")]
    print("Indicadores: %d con bandas" % len(banded))
    for p in params:
        n = len(p.get("bands") or [])
        print("               %-27s %d bandas  [%s]" % (p["id"], n, p.get("bands_source")))
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
    args = ap.parse_args()

    warnings = []

    def warn(msg):
        warnings.append(msg)
        sys.stderr.write("  !  %s\n" % msg)

    token = read_token()
    session = requests.Session()
    session.headers.update({
        "Authorization": "Token %s" % token,
        "Accept": "application/json",
        "User-Agent": "san-gerardo-map/fetch_ceres",
    })

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

    colormaps = {}
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
    if not colormaps and existing and existing.get("params"):
        # Incremental sin vuelos nuevos: las bandas ya estan calculadas. Aun asi
        # se reaplican los overrides, para que editar ceres_thresholds.json y
        # correr sin --full alcance para reclasificar.
        params = (build_params(bands_from_params(existing["params"]), overrides, warn)
                  if overrides else existing["params"])
    else:
        params = build_params(colormaps, overrides, warn)
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

    compute_deltas(flights)
    compute_compliance(flights, params, sectors_meta, warn)

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


def bands_from_params(params):
    """params[] -> {overlay_type: [{min,max}]}, para reaplicar overrides sin refetch."""
    out = {}
    for p in params:
        bands = p.get("bands") or []
        if bands:
            out[p["id"]] = [{"min": b["min"], "max": b["max"]} for b in bands]
    return out


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.stderr.write("\nInterrumpido.\n")
        sys.exit(130)
