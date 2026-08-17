"""Genera disease_data.json desde la base fitopatologica en Excel.

Uso:  python tools/build_disease_data.py [ruta_al_xlsx]

El laboratorio cambio el esquema de unidad entre informes: 2024 otono reporta
"Codigo R" (R1/R2/R3) sin equivalencia declarada, 2024 primavera y 2025 otono
reportan por zona, y desde 2025 primavera por equipo de riego. El JSON conserva
ese esquema por informe en vez de forzar una sola grilla: los informes por zona
se pintan sobre SOIL_GEOJSON, los de equipo sobre los cuarteles agrupados, y los
de codigo R quedan marcados como no mapeables en vez de inventarles ubicacion.
"""
import json
import re
import sys
import unicodedata
import warnings
from datetime import datetime, date
from pathlib import Path

warnings.filterwarnings("ignore")
import openpyxl  # noqa: E402

DEFAULT_XLSX = Path.home() / "Downloads" / "Base_Fitopatologica_San_Gerardo_2024_2026.xlsx"
OUT = Path(__file__).resolve().parent.parent / "disease_data.json"

# Umbral agronomico entregado por el cliente: sobre 10^7 ufc/g es alta incidencia.
THRESHOLD_UFC = 1e7

# Las zonas del laboratorio no se escriben igual que en SOIL_GEOJSON.
ZONE_GEO = {
    "Oficina Norte": "Norte Oficinas",
    "Oficina Sur": "Oficinas Sur",
    "Las Torres": "Las Torres",
}
# Un "equipo" del informe puede cubrir mas de un equipo de riego del campo.
EQUIPO_GEO = {
    "Equipo 1-2": ["E1", "E2"],
    "Equipo 3": ["E3"],
    "Equipo 4-5": ["E4", "E5"],
}
SCHEME = {"Codigo R": "codigoR", "Zona": "zona", "Equipo de riego": "equipo"}
SEASON_ORDER = {"Verano": 1, "Otono": 2, "Invierno": 3, "Primavera": 4}


def strip_accents(text):
    nfd = unicodedata.normalize("NFD", str(text or ""))
    return "".join(c for c in nfd if unicodedata.category(c) != "Mn")


def slug(text):
    return re.sub(r"[^a-z0-9]+", "-", strip_accents(text).lower()).strip("-")


def iso(value):
    if isinstance(value, (datetime, date)):
        return value.strftime("%Y-%m-%d")
    return None


def sheet_records(ws):
    rows = list(ws.iter_rows(min_row=2, values_only=True))
    header = [str(h) if h else "" for h in rows[0]]
    out = []
    for row in rows[1:]:
        if not row[0]:
            continue
        out.append({header[i]: row[i] for i in range(len(header)) if header[i]})
    return out


def pathogen_key(name):
    # "Xanthomonas arboricola pv. juglandis (XAJ)" -> xaj, para no arrastrar el
    # nombre completo como clave en cada lectura.
    if "Xanthomonas" in name:
        return "xaj"
    return slug(name.replace(" sp.", ""))


def main():
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_XLSX
    if not src.exists():
        sys.exit("No se encontro el Excel: %s" % src)

    wb = openpyxl.load_workbook(src, data_only=True)
    results = sheet_records(wb["Base resultados"])
    reports_raw = sheet_records(wb["Informes"])

    # ── patogenos ───────────────────────────────────────────────────────────
    alias = {"xaj": "Peste negra"}
    short = {"xaj": "XAJ"}
    pathogens, seen = [], set()
    for r in results:
        name = str(r["Patogeno"] if "Patogeno" in r else r["Patógeno"]).strip()
        key = pathogen_key(name)
        if key in seen:
            continue
        seen.add(key)
        pathogens.append({
            "key": key,
            "name": name,
            "alias": alias.get(key),
            "short": short.get(key, name.replace(" sp.", "")),
        })

    # ── informes ────────────────────────────────────────────────────────────
    by_report_pathogens = {}
    for r in results:
        rid = r["Informe_ID"]
        key = pathogen_key(str(r.get("Patogeno") or r.get("Patógeno")))
        by_report_pathogens.setdefault(rid, [])
        if key not in by_report_pathogens[rid]:
            by_report_pathogens[rid].append(key)

    reports = []
    for r in reports_raw:
        # La estacion se muestra tal cual viene del informe; la version sin
        # tildes solo se usa para ordenar.
        season = str(r["Estacion"] if "Estacion" in r else r["Estación"]).strip()
        scheme = SCHEME[strip_accents(r["Esquema_unidad"])]
        year = int(r["Ano"] if "Ano" in r else r["Año"])
        sample = str(r["Tipo_muestra"])
        reports.append({
            "id": r["Informe_ID"],
            "year": year,
            "season": season,
            "sample": sample,
            # Dos informes comparten ano y estacion y solo se distinguen por la
            # muestra, asi que la etiqueta la incluye siempre.
            "label": "%s %d · %s" % (season, year, sample.split(" de ")[0]),
            "scheme": scheme,
            "mappable": scheme != "codigoR",
            "sampling": iso(r.get("Fecha_muestreo")),
            "issued": iso(r.get("Fecha_emision") or r.get("Fecha_emisión")),
            "service": r.get("Codigo_servicio") or r.get("Código_servicio"),
            "n": r.get("N° muestras"),
            "locality": r.get("Localidad"),
            "pathogens": by_report_pathogens.get(r["Informe_ID"], []),
            "note": r.get("Observaciones_calidad") or None,
            "sort": year * 10 + SEASON_ORDER.get(strip_accents(season), 0),
        })
    reports.sort(key=lambda x: (x["sort"], x["sample"]))
    for rep in reports:
        del rep["sort"]

    # ── unidades por esquema ────────────────────────────────────────────────
    units = {"zona": [], "equipo": [], "codigoR": []}
    for r in results:
        scheme = SCHEME[strip_accents(r["Unidad_tipo"])]
        if scheme == "zona":
            key = str(r["Zona_estandarizada"]).strip()
            entry = {"key": key, "label": key, "geo": ZONE_GEO.get(key)}
        elif scheme == "equipo":
            key = str(r["Equipo_riego_estandarizado"]).strip()
            entry = {"key": key, "label": key.replace("Equipo ", "Equipos ") if "-" in key else key,
                     "equipos": EQUIPO_GEO.get(key, [])}
        else:
            key = str(r["Codigo_original"] if "Codigo_original" in r else r["Código_original"]).strip()
            entry = {"key": key, "label": key}
        if not any(u["key"] == key for u in units[scheme]):
            units[scheme].append(entry)
    for lst in units.values():
        lst.sort(key=lambda u: u["key"])

    # ── lecturas ────────────────────────────────────────────────────────────
    readings = []
    for r in results:
        scheme = SCHEME[strip_accents(r["Unidad_tipo"])]
        if scheme == "zona":
            unit = str(r["Zona_estandarizada"]).strip()
        elif scheme == "equipo":
            unit = str(r["Equipo_riego_estandarizado"]).strip()
        else:
            unit = str(r.get("Codigo_original") or r.get("Código_original")).strip()
        ufc = r.get("Copias_por_gramo")
        ufc = float(ufc) if ufc is not None else None
        readings.append({
            "id": r["Registro_ID"],
            "report": r["Informe_ID"],
            "pathogen": pathogen_key(str(r.get("Patogeno") or r.get("Patógeno"))),
            "scheme": scheme,
            "unit": unit,
            "ufc": ufc,
            "raw": str(r.get("Resultado_original") or "").strip(),
            "high": bool(ufc is not None and ufc >= THRESHOLD_UFC),
        })

    payload = {
        "meta": {
            "farm": "Fundo San Gerardo",
            "crop": "Nogales",
            "technique": "qPCR",
            "unit": "ufc/g",
            "source": src.name,
            "reports": len(reports),
            "readings": len(readings),
            "thresholdUfc": THRESHOLD_UFC,
            "thresholdLabel": "10⁷ ufc/g",
            "thresholdNote": (
                "Sobre 10⁷ ufc/g se considera alta incidencia; "
                "por debajo el nivel se informa como magnitud, no como alarma."
            ),
        },
        "pathogens": pathogens,
        "reports": reports,
        "units": units,
        "readings": readings,
    }

    OUT.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    mapped = sum(1 for x in readings if x["scheme"] != "codigoR")
    high = sum(1 for x in readings if x["high"])
    print("OK -> %s" % OUT.name)
    print("   informes:  %d (%d mapeables)" % (len(reports), sum(1 for r in reports if r["mappable"])))
    print("   lecturas:  %d (%d mapeables, %d sin equivalencia espacial)"
          % (len(readings), mapped, len(readings) - mapped))
    print("   patogenos: %s" % ", ".join(p["short"] for p in pathogens))
    print("   sobre umbral 10^7: %d" % high)
    if readings:
        mx = max(x for x in (r["ufc"] for r in readings) if x is not None)
        print("   maximo observado: %.3g ufc/g" % mx)


if __name__ == "__main__":
    main()
