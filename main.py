#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
API:
- GET /healthz      -> Überprüft den Zustand des Dienstes
- GET /api          -> Führt eine Baumsuche durch und gibt die Ergebnisse als JSON zurück
- GET /api/geojson  -> Führt eine Baumsuche durch und gibt die Ergebnisse als GeoJSON-Datei zurück
"""

import json
import os
import re
import time
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode

import requests
from flask import Flask, Response, jsonify, render_template_string, request

# -----------------------------------------------------------------------------
# Service Meta / Navigation (no placeholder links)
# -----------------------------------------------------------------------------
LANDING_URL = "https://data-tales.dev/"
COOKBOOK_URL = "https://data-tales.dev/cookbook/"
PLZ_URL = "https://plz.data-tales.dev/"

SERVICE_META = {
    "service_name_slug": "tree",
    "page_title": "Tree Locator – data-tales.dev",
    "page_h1": "Tree Locator",
    "page_subtitle": "Zähle OpenStreetMap-Bäume für einen Ort und liefere eine schnelle Übersicht.",
}



# -----------------------------------------------------------------------------
# External endpoints & policy-friendly defaults
# -----------------------------------------------------------------------------
USER_AGENT = os.getenv(
    "USER_AGENT",
    "data-tales-tree-locator/1.0 (+https://data-tales.dev/; info@data-tales.dev)",
)
NOMINATIM_ENDPOINT = os.getenv("NOMINATIM_ENDPOINT", "https://nominatim.openstreetmap.org")
OVERPASS_ENDPOINT = os.getenv("OVERPASS_ENDPOINT", "https://overpass-api.de/api/interpreter")

# Requests timeouts (connect, read)
HTTP_TIMEOUT = (5, 25)

# Limits
Q_MAX_LEN = 120
RADIUS_MIN_KM = 0.1
RADIUS_MAX_KM = 50.0
SAMPLE_MAX = 2000
SAMPLE_DEFAULT = 500

# -----------------------------------------------------------------------------
# Flask app
# -----------------------------------------------------------------------------
app = Flask(__name__)

# -----------------------------------------------------------------------------
# Utilities
# -----------------------------------------------------------------------------


class UserFacingError(Exception):
    def __init__(self, message: str, hint: Optional[str] = None):
        super().__init__(message)
        self.message = message
        self.hint = hint


def _now_ms() -> int:
    return int(time.time() * 1000)


def _norm_q(q: str) -> str:
    return re.sub(r"\s+", " ", q.strip())


def validate_q(q: str) -> str:
    q = _norm_q(q)
    if not (2 <= len(q) <= Q_MAX_LEN):
        raise UserFacingError(
            f"Bitte gib einen Ort mit 2–{Q_MAX_LEN} Zeichen an.",
            hint='Beispiel: "Darmstadt, Hessen, DE"',
        )
    # allow letters (incl. umlauts), digits, whitespace, and a small set of punctuation
    if not re.match(r"^[\w\säöüÄÖÜß.,\-'/()]+$", q, flags=re.UNICODE):
        raise UserFacingError(
            "Der Ort enthält nicht erlaubte Zeichen.",
            hint="Erlaubt sind Buchstaben/Zahlen, Leerzeichen sowie . , - ' / ( )",
        )
    return q


def parse_radius_km(raw: str) -> float:
    try:
        val = float(raw)
    except Exception:
        raise UserFacingError("Radius muss eine Zahl sein.", hint="Beispiel: 2 oder 5.5")

    if not (RADIUS_MIN_KM <= val <= RADIUS_MAX_KM):
        raise UserFacingError(
            f"Radius muss zwischen {RADIUS_MIN_KM:g} und {RADIUS_MAX_KM:g} km liegen."
        )
    return val


def parse_limit(raw: str, default: int = SAMPLE_DEFAULT) -> int:
    if raw is None or str(raw).strip() == "":
        return default
    try:
        val = int(raw)
    except Exception:
        raise UserFacingError("Limit muss eine ganze Zahl sein.", hint=f"1–{SAMPLE_MAX}")

    if not (1 <= val <= SAMPLE_MAX):
        raise UserFacingError(f"Limit muss zwischen 1 und {SAMPLE_MAX} liegen.")
    return val


def parse_mode(raw: str) -> str:
    raw = (raw or "").strip().lower()
    if raw in ("", "count"):
        return "count"
    if raw in ("sample",):
        return "sample"
    raise UserFacingError("Ungültiger Modus.", hint="Erlaubt: count, sample")


def parse_query_mode(raw: str) -> str:
    raw = (raw or "boundary").strip().lower()
    if raw in ("boundary", "radius"):
        return raw
    raise UserFacingError("Ungültiger Suchmodus.", hint="Erlaubt: boundary, radius")


def _requests_headers() -> Dict[str, str]:
    # OSM endpoints expect a descriptive UA
    return {"User-Agent": USER_AGENT, "Accept": "application/json"}


@dataclass(frozen=True)
class GeocodeResult:
    q: str
    display_name: str
    lat: float
    lon: float
    osm_type: str
    osm_id: int
    geojson: Optional[Dict[str, Any]]


def _area_id_from_osm(osm_type: str, osm_id: int) -> Optional[int]:
    # Overpass area IDs:
    # relation -> 3600000000 + id
    # way      -> 2400000000 + id
    # node     -> not directly supported as area
    osm_type = (osm_type or "").lower()
    if osm_type == "relation":
        return 3600000000 + osm_id
    if osm_type == "way":
        return 2400000000 + osm_id
    return None


@lru_cache(maxsize=512)
def geocode_nominatim(q: str) -> GeocodeResult:
    params = {
        "q": q,
        "format": "jsonv2",
        "limit": 1,
        "polygon_geojson": 1,
        "addressdetails": 0,
    }
    url = f"{NOMINATIM_ENDPOINT.rstrip('/')}/search?{urlencode(params)}"

    try:
        r = requests.get(url, headers=_requests_headers(), timeout=HTTP_TIMEOUT)
    except requests.RequestException:
        raise UserFacingError(
            "Geocoding ist aktuell nicht erreichbar.",
            hint="Bitte später erneut versuchen.",
        )

    if r.status_code == 429:
        raise UserFacingError(
            "Geocoding-Limit erreicht (Rate Limit).",
            hint="Bitte warte kurz und versuche es erneut.",
        )
    if r.status_code >= 500:
        raise UserFacingError("Geocoding-Serverfehler.", hint="Bitte später erneut versuchen.")
    if r.status_code != 200:
        raise UserFacingError("Geocoding fehlgeschlagen.", hint=f"HTTP {r.status_code}")

    data = r.json()
    if not data:
        raise UserFacingError("Ort nicht gefunden.", hint="Bitte präzisieren (z. B. Stadt, Bundesland, Land).")

    item = data[0]
    try:
        lat = float(item["lat"])
        lon = float(item["lon"])
        osm_type = str(item.get("osm_type", ""))
        osm_id = int(item.get("osm_id"))
        display_name = str(item.get("display_name", q))
        geojson = item.get("geojson")
    except Exception:
        raise UserFacingError("Geocoding-Antwort konnte nicht verarbeitet werden.", hint="Bitte später erneut versuchen.")

    return GeocodeResult(
        q=q,
        display_name=display_name,
        lat=lat,
        lon=lon,
        osm_type=osm_type,
        osm_id=osm_id,
        geojson=geojson if isinstance(geojson, dict) else None,
    )


def _overpass_post(query: str) -> Dict[str, Any]:
    try:
        r = requests.post(
            OVERPASS_ENDPOINT,
            data=query.encode("utf-8"),
            headers={**_requests_headers(), "Content-Type": "text/plain; charset=utf-8"},
            timeout=HTTP_TIMEOUT,
        )
    except requests.RequestException:
        raise UserFacingError(
            "Overpass API ist aktuell nicht erreichbar.",
            hint="Bitte später erneut versuchen.",
        )

    if r.status_code == 429:
        raise UserFacingError(
            "Overpass API ist überlastet (Rate Limit).",
            hint="Bitte warte 30–60 Sekunden und versuche es erneut.",
        )
    if r.status_code in (502, 503, 504):
        raise UserFacingError(
            "Overpass API ist aktuell überlastet.",
            hint="Bitte später erneut versuchen.",
        )
    if r.status_code != 200:
        raise UserFacingError("Overpass Anfrage fehlgeschlagen.", hint=f"HTTP {r.status_code}")

    try:
        return r.json()
    except Exception:
        raise UserFacingError("Overpass Antwort ist ungültig.", hint="Bitte später erneut versuchen.")


@lru_cache(maxsize=1024)
def overpass_count_by_area(area_id: int) -> int:
    q = f"""
[out:json][timeout:25];
area({area_id})->.a;
node["natural"="tree"](area.a);
out count;
""".strip()
    data = _overpass_post(q)
    elements = data.get("elements", [])
    if not elements:
        return 0
    return int(elements[0].get("tags", {}).get("total", 0))


@lru_cache(maxsize=1024)
def overpass_count_by_radius(lat: float, lon: float, radius_m: int) -> int:
    q = f"""
[out:json][timeout:25];
node(around:{radius_m},{lat},{lon})["natural"="tree"];
out count;
""".strip()
    data = _overpass_post(q)
    elements = data.get("elements", [])
    if not elements:
        return 0
    return int(elements[0].get("tags", {}).get("total", 0))


def overpass_sample_by_area(area_id: int, limit: int) -> List[Dict[str, Any]]:
    q = f"""
[out:json][timeout:25];
area({area_id})->.a;
node["natural"="tree"](area.a);
out body {limit};
""".strip()
    data = _overpass_post(q)
    out = []
    for el in data.get("elements", []):
        if el.get("type") != "node":
            continue
        lat = el.get("lat")
        lon = el.get("lon")
        if lat is None or lon is None:
            continue
        out.append({"lat": float(lat), "lon": float(lon), "id": el.get("id")})
    return out


def overpass_sample_by_radius(lat: float, lon: float, radius_m: int, limit: int) -> List[Dict[str, Any]]:
    q = f"""
[out:json][timeout:25];
node(around:{radius_m},{lat},{lon})["natural"="tree"];
out body {limit};
""".strip()
    data = _overpass_post(q)
    out = []
    for el in data.get("elements", []):
        if el.get("type") != "node":
            continue
        rlat = el.get("lat")
        rlon = el.get("lon")
        if rlat is None or rlon is None:
            continue
        out.append({"lat": float(rlat), "lon": float(rlon), "id": el.get("id")})
    return out


def geojson_featurecollection(
    points: List[Dict[str, Any]],
    boundary_geojson: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    feats = []

    if boundary_geojson:
        feats.append(
            {
                "type": "Feature",
                "properties": {"kind": "boundary"},
                "geometry": boundary_geojson,
            }
        )

    for p in points:
        feats.append(
            {
                "type": "Feature",
                "properties": {"kind": "tree", "id": p.get("id")},
                "geometry": {"type": "Point", "coordinates": [p["lon"], p["lat"]]},
            }
        )

    return {
        "type": "FeatureCollection",
        "features": feats,
        "properties": {
            "attribution": "© OpenStreetMap contributors (ODbL)",
        },
    }


def run_tree_locator(
    q: str,
    query_mode: str,
    radius_km: Optional[float],
    mode: str,
    limit: int,
) -> Dict[str, Any]:
    t0 = _now_ms()
    geo = geocode_nominatim(q)

    area_id = _area_id_from_osm(geo.osm_type, geo.osm_id) if query_mode == "boundary" else None
    used_mode = query_mode

    radius_km_val = None
    radius_m = None

    if used_mode == "boundary" and area_id is None:
        # fallback: boundary not supported for this osm_type (often node)
        used_mode = "radius"

    if used_mode == "radius":
        radius_km_val = radius_km if radius_km is not None else 2.0
        radius_m = int(round(radius_km_val * 1000))

    if mode == "count":
        if used_mode == "boundary":
            tree_count = overpass_count_by_area(int(area_id))  # type: ignore[arg-type]
            sample = []
        else:
            tree_count = overpass_count_by_radius(geo.lat, geo.lon, int(radius_m))  # type: ignore[arg-type]
            sample = []
    else:
        if limit > SAMPLE_MAX:
            raise UserFacingError(f"Limit zu hoch. Maximal {SAMPLE_MAX}.")
        if used_mode == "boundary":
            tree_count = overpass_count_by_area(int(area_id))  # cheap cached count
            # safeguard: only sample if count is not absurdly high; still limit payload
            if tree_count > 200000:
                sample = []
            else:
                sample = overpass_sample_by_area(int(area_id), limit)
        else:
            tree_count = overpass_count_by_radius(geo.lat, geo.lon, int(radius_m))  # cached
            if tree_count > 200000:
                sample = []
            else:
                sample = overpass_sample_by_radius(geo.lat, geo.lon, int(radius_m), limit)

    dt_ms = _now_ms() - t0

    return {
        "ok": True,
        "query": {
            "q": geo.q,
            "display_name": geo.display_name,
            "lat": geo.lat,
            "lon": geo.lon,
            "query_mode": used_mode,
            "mode": mode,
            "radius_km": radius_km_val,
            "limit": limit if mode == "sample" else None,
        },
        "tree_count": int(tree_count),
        "sample": sample,
        "boundary_geojson": geo.geojson,
        "attribution": {
            "text": "© OpenStreetMap contributors (ODbL)",
            "license_url": "https://opendatacommons.org/licenses/odbl/",
        },
        "timing_ms": dt_ms,
    }


# -----------------------------------------------------------------------------
# Routes
# -----------------------------------------------------------------------------


@app.get("/healthz")
def healthz() -> Response:
    return Response("ok", mimetype="text/plain")


@app.get("/api")
def api() -> Response:
    try:
        q = validate_q(request.args.get("q", ""))
        query_mode = parse_query_mode(request.args.get("query_mode"))
        mode = parse_mode(request.args.get("mode"))
        radius_km = None
        if query_mode == "radius":
            radius_km = parse_radius_km(request.args.get("radius_km", "2"))
        limit = parse_limit(request.args.get("limit"), default=SAMPLE_DEFAULT)

        result = run_tree_locator(
            q=q,
            query_mode=query_mode,
            radius_km=radius_km,
            mode=mode,
            limit=limit,
        )
        return jsonify(result)
    except UserFacingError as e:
        return jsonify({"ok": False, "error": e.message, "hint": e.hint}), 400
    except Exception:
        return jsonify({"ok": False, "error": "Unerwarteter Fehler.", "hint": "Bitte später erneut versuchen."}), 500


@app.get("/api/geojson")
def api_geojson() -> Response:
    try:
        q = validate_q(request.args.get("q", ""))
        query_mode = parse_query_mode(request.args.get("query_mode"))
        mode = parse_mode(request.args.get("mode") or "sample")
        if mode != "sample":
            raise UserFacingError("GeoJSON ist nur im sample-Modus verfügbar.", hint="mode=sample setzen.")
        radius_km = None
        if query_mode == "radius":
            radius_km = parse_radius_km(request.args.get("radius_km", "2"))
        limit = parse_limit(request.args.get("limit"), default=min(SAMPLE_DEFAULT, 500))

        result = run_tree_locator(
            q=q,
            query_mode=query_mode,
            radius_km=radius_km,
            mode="sample",
            limit=limit,
        )

        fc = geojson_featurecollection(result.get("sample", []), boundary_geojson=result.get("boundary_geojson"))
        pretty = json.dumps(fc, ensure_ascii=False)
        filename = f"trees_{re.sub(r'[^a-zA-Z0-9_-]+', '_', q)[:60]}.geojson"
        return Response(
            pretty,
            mimetype="application/geo+json; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    except UserFacingError as e:
        return jsonify({"ok": False, "error": e.message, "hint": e.hint}), 400
    except Exception:
        return jsonify({"ok": False, "error": "Unerwarteter Fehler.", "hint": "Bitte später erneut versuchen."}), 500


HTML = r"""
<!doctype html>
<html lang="de">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <meta name="description" content="{{ meta.page_subtitle }}" />
  <meta name="theme-color" content="#0b0f19" />
  <title>{{ meta.page_title }}</title>

  <!-- Leaflet (CDN) for optional Mini-Map -->
  <link
    rel="stylesheet"
    href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"
    crossorigin=""
  />

  <style>
    :root{
      --bg: #0b0f19;
      --bg2:#0f172a;
      --card:#111a2e;
      --text:#e6eaf2;
      --muted:#a8b3cf;
      --border: rgba(255,255,255,.10);
      --shadow: 0 18px 60px rgba(0,0,0,.35);
      --primary:#6ea8fe;
      --primary2:#8bd4ff;
      --focus: rgba(110,168,254,.45);

      --radius: 18px;
      --container: 1100px;
      --gap: 18px;

      --font: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial, "Apple Color Emoji","Segoe UI Emoji";
    }

    [data-theme="light"]{
      --bg:#f6f7fb;
      --bg2:#ffffff;
      --card:#ffffff;
      --text:#111827;
      --muted:#4b5563;
      --border: rgba(17,24,39,.12);
      --shadow: 0 18px 60px rgba(17,24,39,.10);
      --primary:#2563eb;
      --primary2:#0ea5e9;
      --focus: rgba(37,99,235,.25);
    }

    *{box-sizing:border-box}
    html,body{height:100%}
    body{
      margin:0;
      font-family:var(--font);
      background: radial-gradient(1200px 800px at 20% -10%, rgba(110,168,254,.25), transparent 55%),
                  radial-gradient(1000px 700px at 110% 10%, rgba(139,212,255,.20), transparent 55%),
                  linear-gradient(180deg, var(--bg), var(--bg2));
      color:var(--text);
    }

    .container{
      max-width:var(--container);
      margin:0 auto;
      padding:0 18px;
    }

    .skip-link{
      position:absolute; left:-999px; top:10px;
      background:var(--card); color:var(--text);
      padding:10px 12px; border-radius:10px;
      border:1px solid var(--border);
    }
    .skip-link:focus{left:10px; outline:2px solid var(--focus)}

    .site-header{
      position:sticky; top:0; z-index:20;
      backdrop-filter: blur(10px);
      background: rgba(10, 14, 24, .55);
      border-bottom:1px solid var(--border);
    }
    [data-theme="light"] .site-header{ background: rgba(246,247,251,.75); }

    .header-inner{
      display:flex; align-items:center; justify-content:space-between;
      padding:14px 0;
      gap:14px;
    }
    .brand{display:flex; align-items:center; gap:10px; text-decoration:none; color:var(--text); font-weight:700}
    .brand-mark{
      width:14px; height:14px; border-radius:6px;
      background: linear-gradient(135deg, var(--primary), var(--primary2));
      box-shadow: 0 10px 25px rgba(110,168,254,.25);
    }
    .nav{display:flex; gap:16px; flex-wrap:wrap}
    .nav a{color:var(--muted); text-decoration:none; font-weight:600}
    .nav a:hover{color:var(--text)}
    .header-actions{display:flex; gap:10px; align-items:center}
    .header-note{
      display:flex;
      align-items:center;
      gap:8px;
      padding:8px 10px;
      border-radius:12px;
      border:1px solid var(--border);
      background: rgba(255,255,255,.04);
      color: var(--muted);
      font-weight: 750;
      font-size: 12px;
      line-height: 1;
      white-space: nowrap;
    }

    [data-theme="light"] .header-note{
      background: rgba(17,24,39,.03);
    }

    .header-note__label{
      letter-spacing: .06em;
      text-transform: uppercase;
      font-weight: 900;
      color: var(--muted);
    }

    .header-note__mail{
      color: var(--text);
      text-decoration: none;
      font-weight: 850;
    }

    .header-note__mail:hover{
      text-decoration: underline;
    }

    /* Mobile: Label ausblenden, nur Mail zeigen */
    @media (max-width: 720px){
      .header-note__label{ display:none; }
    }
    .btn{
      display:inline-flex; align-items:center; justify-content:center;
      gap:8px;
      padding:10px 14px;
      border-radius:12px;
      border:1px solid var(--border);
      text-decoration:none;
      font-weight:700;
      color:var(--text);
      background: transparent;
      cursor:pointer;
    }
    .btn:focus{outline:2px solid var(--focus); outline-offset:2px}
    .btn-primary{
      border-color: transparent;
      background: linear-gradient(135deg, var(--primary), var(--primary2));
      color: #0b0f19;
    }
    [data-theme="light"] .btn-primary{ color:#ffffff; }
    .btn-secondary{ background: rgba(255,255,255,.06); }
    [data-theme="light"] .btn-secondary{ background: rgba(17,24,39,.04); }
    .btn-ghost{ background: transparent; }
    .btn:hover{transform: translateY(-1px)}
    .btn:active{transform:none}

    .nav-dropdown{ position: relative; display: inline-flex; align-items: center; }
    .nav-dropbtn{ padding: 10px 12px; gap: 8px; }
    .nav-caret{ font-size: .9em; opacity: .85; }
    .nav-menu{
      position: absolute;
      top: calc(100% + 10px);
      left: 0;
      min-width: 240px;
      padding: 10px;
      z-index: 60;
      background: rgba(17, 26, 46, .92);
      backdrop-filter: blur(10px);
    }
    [data-theme="light"] .nav-menu{ background: rgba(255, 255, 255, .96); }
    .nav-menu a{
      display: block;
      padding: 10px 10px;
      border-radius: 12px;
      text-decoration: none;
      color: var(--text);
      font-weight: 650;
    }
    .nav-menu a:hover{ background: rgba(110,168,254,.12); }

    .hero{padding:54px 0 22px}
    .kicker{
      margin:0 0 10px;
      display:inline-block;
      font-weight:800;
      letter-spacing:.08em;
      text-transform:uppercase;
      color:var(--muted);
      font-size:12px;
    }
    h1{margin:0 0 12px; font-size:42px; line-height:1.1}
    @media (max-width: 520px){ h1{font-size:34px} }
    .lead{margin:0 0 18px; color:var(--muted); font-size:16px; line-height:1.6}

    .section{padding:18px 0 42px}
    .card{
      border:1px solid var(--border);
      border-radius: var(--radius);
      background: rgba(255,255,255,.04);
      padding:16px;
      box-shadow: var(--shadow);
    }
    [data-theme="light"] .card{ background: rgba(255,255,255,.92); }

    .form-grid{
      display:grid;
      grid-template-columns: 1.2fr .8fr;
      gap: var(--gap);
      align-items: start;
    }
    @media (max-width: 900px){ .form-grid{ grid-template-columns: 1fr; } }

    label{display:block; font-weight:800; margin:0 0 8px}
    .field{margin:0 0 14px}
    input, select{
      width:100%;
      padding:12px 14px;
      border-radius:12px;
      border:1px solid var(--border);
      background: rgba(255,255,255,.04);
      color: var(--text);
      font-weight:650;
    }
    [data-theme="light"] input, [data-theme="light"] select{ background: rgba(17,24,39,.03); }
    input:focus, select:focus{ outline:2px solid var(--focus); outline-offset:2px }

    .row{display:flex; gap:12px; flex-wrap:wrap; margin-top:10px}
    .muted{color:var(--muted); line-height:1.6; margin:0}
    .hint{font-size:12px; color:var(--muted); margin-top:8px}

    .badge{
      display:inline-flex;
      align-items:center;
      gap:8px;
      padding:6px 10px;
      border-radius:999px;
      border:1px solid var(--border);
      background: rgba(255,255,255,.03);
      color: var(--muted);
      font-weight:800;
      font-size:12px;
    }
    [data-theme="light"] .badge{ background: rgba(17,24,39,.02); }

    .map{
      height: 360px;
      border-radius: var(--radius);
      border:1px solid var(--border);
      overflow:hidden;
      margin-top:12px;
    }

    .site-footer{
      border-top:1px solid var(--border);
      padding:18px 0;
    }
    .footer-inner{display:flex; justify-content:space-between; gap:12px; flex-wrap:wrap}
    .sr-only{
      position:absolute; width:1px; height:1px; padding:0; margin:-1px;
      overflow:hidden; clip:rect(0,0,0,0); border:0;
    }
  </style>
</head>

<body>
  <a class="skip-link" href="#main">Zum Inhalt springen</a>

  <header class="site-header">
    <div class="container header-inner">
      <a class="brand" href="{{ landing_url }}" aria-label="Zur Landing Page">
        <span class="brand-mark" aria-hidden="true"></span>
        <span class="brand-text">data-tales.dev</span>
      </a>

      <div class="nav-dropdown" data-dropdown>
          <button class="btn btn-ghost nav-dropbtn"
                  type="button"
                  aria-haspopup="true"
                  aria-expanded="false"
                  aria-controls="servicesMenu">
            Dienste <span class="nav-caret" aria-hidden="true">▾</span>
          </button>

          <div id="servicesMenu" class="card nav-menu" role="menu" hidden>
            <a role="menuitem" href="https://flybi-demo.data-tales.dev/">Flybi Dashboard Demo</a>
            <a role="menuitem" href="https://wms-wfs-sources.data-tales.dev/">WMS/WFS Server Viewer</a>
            <a role="menuitem" href="https://tree-locator.data-tales.dev/">Tree Locator</a>
            <a role="menuitem" href="https://plz.data-tales.dev/">PLZ → Koordinaten</a>
            <a role="menuitem" href="https://paw-wiki.data-tales.dev/">Paw Patrole Wiki</a>
            <a role="menuitem" href="https://paw-quiz.data-tales.dev/">Paw Patrole Quiz</a>
            <a role="menuitem" href="https://hp-quiz.data-tales.dev/">Harry Potter Quiz</a>
            <a role="menuitem" href="https://worm-attack-3000.data-tales.dev/">Wurm Attacke 3000</a>
          </div>
      </div>

      <div class="header-actions">
        <div class="header-note" aria-label="Feedback Kontakt">
          <span class="header-note__label">Änderung / Kritik:</span>
          <a class="header-note__mail" href="mailto:info@data-tales.dev">info@data-tales.dev</a>
        </div>

        
        <button class="btn btn-ghost" id="themeToggle" type="button" aria-label="Theme umschalten">
          <span aria-hidden="true" id="themeIcon">☾</span>
          <span class="sr-only">Theme umschalten</span>
        </button>
      </div>
    </div>
  </header>

  <main id="main">
    <section class="hero">
      <div class="container">
        <p class="kicker">{{ meta.service_name_slug }}</p>
        <h1>{{ meta.page_h1 }}</h1>
        <p class="lead">{{ meta.page_subtitle }}</p>
      </div>
    </section>

    <section class="section">
      <div class="container form-grid">

        <div class="card">
          <form method="post" action="/">
            <div class="field">
              <label for="q">Ort</label>
              <input id="q" name="q" required maxlength="120"
                     value="{{ form.q }}"
                     placeholder='z. B. "Darmstadt, Hessen, DE"' />
              <div class="hint">Eingabe wird über Nominatim (OSM) aufgelöst.</div>
            </div>

            <div class="field">
              <label for="query_mode">Suchmodus</label>
              <select id="query_mode" name="query_mode">
                <option value="boundary" {% if form.query_mode == "boundary" %}selected{% endif %}>Boundary (Area)</option>
                <option value="radius" {% if form.query_mode == "radius" %}selected{% endif %}>Radius (um Zentrum)</option>
              </select>
              <div class="hint">Boundary ist präziser, Radius ist robuster (Fallback bei manchen Treffern).</div>
            </div>

            <div class="field" id="radiusField">
              <label for="radius_km">Radius (km)</label>
              <input id="radius_km" name="radius_km" inputmode="decimal"
                     value="{{ form.radius_km }}"
                     placeholder="z. B. 2" />
              <div class="hint">Gültig: {{ radius_min }}–{{ radius_max }} km.</div>
            </div>

            <div class="field">
              <label for="mode">Output</label>
              <select id="mode" name="mode">
                <option value="count" {% if form.mode == "count" %}selected{% endif %}>Count (schnell)</option>
                <option value="sample" {% if form.mode == "sample" %}selected{% endif %}>Sample + Mini-Map</option>
              </select>
            </div>

            <div class="field" id="limitField">
              <label for="limit">Sample-Limit</label>
              <input id="limit" name="limit" inputmode="numeric"
                     value="{{ form.limit }}"
                     placeholder="z. B. 500" />
              <div class="hint">Maximal {{ sample_max }} Punkte. Bei sehr großen Treffermengen wird ggf. nur der Count gezeigt.</div>
            </div>

            <div class="row">
              <button class="btn btn-primary" type="submit">Suchen</button>
              {% if api_url %}
                <a class="btn btn-secondary" href="{{ api_url }}" target="_blank" rel="noreferrer">JSON</a>
              {% endif %}
              {% if geojson_url %}
                <a class="btn btn-ghost" href="{{ geojson_url }}">GeoJSON</a>
              {% endif %}
            </div>
          </form>
        </div>

        <div>
          {% if error %}
            <div class="card">
              <p class="badge">Fehler</p>
              <h3 style="margin:10px 0 8px;">{{ error }}</h3>
              {% if hint %}
                <p class="muted">{{ hint }}</p>
              {% endif %}
            </div>
          {% endif %}

          {% if result %}
            <div class="card">
              <p class="badge">Ergebnis</p>
              <h3 style="margin:10px 0 8px;">{{ result.tree_count }} Bäume</h3>
              <p class="muted" style="margin:0 0 10px;">
                <strong>Ort:</strong> {{ result.query.display_name }}
              </p>
              <p class="muted" style="margin:0 0 10px;">
                <strong>Modus:</strong> {{ result.query.query_mode }} / {{ result.query.mode }}
                {% if result.query.radius_km %}
                  (Radius: {{ "%.1f"|format(result.query.radius_km) }} km)
                {% endif %}
                {% if result.query.limit %}
                  (Limit: {{ result.query.limit }})
                {% endif %}
              </p>
              <p class="muted" style="margin:0 0 10px;">
                <strong>Timing:</strong> {{ result.timing_ms }} ms
              </p>

              <p class="muted" style="margin:0;">
                {{ result.attribution.text }} –
                <a href="{{ result.attribution.license_url }}" target="_blank" rel="noreferrer" style="color:var(--text); font-weight:800; text-decoration:none; border-bottom:1px solid transparent;">
                  Lizenz
                </a>
              </p>

              {% if result.sample and result.sample|length > 0 %}
                <div class="map" id="map"></div>
                <div class="hint">Mini-Map zeigt bis zu {{ result.sample|length }} Punkte (Sample), nicht den vollständigen Datensatz.</div>
              {% endif %}
            </div>
          {% endif %}

          {% if not result and not error %}
            <div class="card">
              <p class="badge">Hinweis</p>
              <p class="muted" style="margin:10px 0 0;">
                Tippe einen Ort ein und wähle optional „Sample + Mini-Map“. Standardmäßig wird nur die Anzahl (Count) berechnet.
              </p>
            </div>
          {% endif %}
        </div>

      </div>
    </section>
  </main>

  <footer class="site-footer">
    <div class="container footer-inner">
      <span class="muted">© <span id="year"></span> data-tales.dev</span>
      <span class="muted">Tree Locator (Cloud Run)</span>
    </div>
  </footer>

  <script
    src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"
    crossorigin=""
  ></script>

  <script>
    (function(){
    const dd = document.querySelector('[data-dropdown]');
    if(!dd) return;

    const btn = dd.querySelector('.nav-dropbtn');
    const menu = dd.querySelector('.nav-menu');

    function setOpen(isOpen){
      btn.setAttribute('aria-expanded', String(isOpen));
      if(isOpen){
        menu.hidden = false;
        dd.classList.add('open');
      }else{
        menu.hidden = true;
        dd.classList.remove('open');
      }
    }

    btn.addEventListener('click', (e) => {
      e.preventDefault();
      e.stopPropagation();
      const isOpen = btn.getAttribute('aria-expanded') === 'true';
      setOpen(!isOpen);
    });

    document.addEventListener('click', (e) => {
      if(!dd.contains(e.target)) setOpen(false);
    });

    document.addEventListener('keydown', (e) => {
      if(e.key === 'Escape') setOpen(false);
    });

    // Wenn per Tab aus dem Dropdown rausnavigiert wird: schließen
    dd.addEventListener('focusout', () => {
      requestAnimationFrame(() => {
        if(!dd.contains(document.activeElement)) setOpen(false);
      });
    });

    // Initial geschlossen
    setOpen(false);
  })();
    // Theme toggle (Landing-like)
    (function(){
      const key = "theme";
      const root = document.documentElement;
      const btn = document.getElementById("themeToggle");
      const icon = btn ? btn.querySelector("[aria-hidden='true']") : null;

      function apply(theme){
        if(theme === "light"){
          root.setAttribute("data-theme","light");
          if(icon) icon.textContent = "☀";
        } else {
          root.removeAttribute("data-theme");
          if(icon) icon.textContent = "☾";
        }
      }

      const stored = localStorage.getItem(key);
      apply(stored);

      if(btn){
        btn.addEventListener("click", () => {
          const isLight = root.getAttribute("data-theme") === "light";
          const next = isLight ? "" : "light";
          if(next) localStorage.setItem(key, next);
          else localStorage.removeItem(key);
          apply(next);
        });
      }
    })();

    // Form conditional fields
    (function(){
      const qm = document.getElementById("query_mode");
      const mode = document.getElementById("mode");
      const radiusField = document.getElementById("radiusField");
      const limitField = document.getElementById("limitField");

      function update(){
        const qmVal = qm ? qm.value : "boundary";
        if(radiusField) radiusField.style.display = (qmVal === "radius") ? "block" : "none";

        const mVal = mode ? mode.value : "count";
        if(limitField) limitField.style.display = (mVal === "sample") ? "block" : "none";
      }
      if(qm) qm.addEventListener("change", update);
      if(mode) mode.addEventListener("change", update);
      update();
    })();

    // Mini-Map (only if sample data exists)
    (function(){
      const mapEl = document.getElementById("map");
      if(!mapEl) return;

      const sample = {{ (result.sample if result and result.sample else []) | tojson }};
      const boundary = {{ (result.boundary_geojson if result and result.boundary_geojson else None) | tojson }};
      const center = {{ ([result.query.lat, result.query.lon] if result else [49.8728, 8.6512]) | tojson }};

      const map = L.map("map", { zoomControl: true }).setView(center, 12);
      L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
        maxZoom: 19,
        attribution: '© OpenStreetMap contributors'
      }).addTo(map);

      if(boundary){
        try{
          const layer = L.geoJSON(boundary, { style: { weight: 2, fillOpacity: 0.05 } }).addTo(map);
          map.fitBounds(layer.getBounds(), { padding: [10, 10] });
        }catch(e){}
      }

      const pts = [];
      for(const p of sample){
        if(!p || typeof p.lat !== "number" || typeof p.lon !== "number") continue;
        pts.push([p.lat, p.lon]);
        L.circleMarker([p.lat, p.lon], { radius: 3, weight: 1, fillOpacity: 0.6 }).addTo(map);
      }
      if(pts.length && !boundary){
        const bounds = L.latLngBounds(pts);
        map.fitBounds(bounds, { padding: [10, 10] });
      }
    })();

    // Footer year
    document.getElementById("year").textContent = new Date().getFullYear();
  </script>
</body>
</html>
"""


@app.route("/", methods=["GET", "POST"])
def index() -> Response:
    # Defaults for form
    form = {
        "q": "Darmstadt, Hessen, DE",
        "query_mode": "boundary",
        "radius_km": "2",
        "mode": "count",
        "limit": str(SAMPLE_DEFAULT),
    }

    result = None
    error = None
    hint = None
    api_url = None
    geojson_url = None

    def _build_urls(f: Dict[str, str]) -> Tuple[str, Optional[str]]:
        qs = {
            "q": f["q"],
            "query_mode": f["query_mode"],
            "mode": f["mode"],
        }
        if f["query_mode"] == "radius":
            qs["radius_km"] = f["radius_km"] or "2"
        if f["mode"] == "sample":
            qs["limit"] = f["limit"] or str(SAMPLE_DEFAULT)

        api = "/api?" + urlencode(qs)
        gj = None
        if f["mode"] == "sample":
            gj = "/api/geojson?" + urlencode(qs)
        return api, gj

    if request.method == "POST":
        try:
            q = validate_q(request.form.get("q", ""))
            query_mode = parse_query_mode(request.form.get("query_mode"))
            mode = parse_mode(request.form.get("mode"))
            radius_km = None
            if query_mode == "radius":
                radius_km = parse_radius_km(request.form.get("radius_km", "2"))
            limit = parse_limit(request.form.get("limit"), default=SAMPLE_DEFAULT)

            form.update(
                {
                    "q": q,
                    "query_mode": query_mode,
                    "radius_km": f"{radius_km:g}" if radius_km is not None else (request.form.get("radius_km", "2") or "2"),
                    "mode": mode,
                    "limit": str(limit),
                }
            )

            result = run_tree_locator(
                q=q,
                query_mode=query_mode,
                radius_km=radius_km,
                mode=mode,
                limit=limit,
            )

        except UserFacingError as e:
            error = e.message
            hint = e.hint
            # keep user's last input in form
            form.update(
                {
                    "q": _norm_q(request.form.get("q", "")),
                    "query_mode": (request.form.get("query_mode") or "boundary"),
                    "radius_km": (request.form.get("radius_km") or "2"),
                    "mode": (request.form.get("mode") or "count"),
                    "limit": (request.form.get("limit") or str(SAMPLE_DEFAULT)),
                }
            )
        except Exception:
            error = "Unerwarteter Fehler."
            hint = "Bitte später erneut versuchen."

    # Always compute API/GeoJSON URLs based on current form values
    try:
        api_url, geojson_url = _build_urls(form)
    except Exception:
        api_url, geojson_url = None, None

    return render_template_string(
        HTML,
        meta=SERVICE_META,
        landing_url=LANDING_URL,
        cookbook_url=COOKBOOK_URL,
        form=form,
        result=result,
        error=error,
        hint=hint,
        api_url=api_url,
        geojson_url=geojson_url,
        radius_min=RADIUS_MIN_KM,
        radius_max=RADIUS_MAX_KM,
        sample_max=SAMPLE_MAX,
    )


if __name__ == "__main__":
    # Local dev only (Cloud Run uses gunicorn)
    port = int(os.environ.get("PORT", "8080"))
    app.run(host="0.0.0.0", port=port, debug=False)
