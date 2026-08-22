#!/usr/bin/env python3
"""
Real Estate Scorer dashboard.

  http://192.168.1.252:5007

Two things it does:
  1. Address lookup  — paste an address, get everything we know about its tract.
  2. Hot zone map    — national choropleth (county), drilling to tract on zoom.

Every panel degrades gracefully. Layers that haven't been ingested yet render as
"not loaded" rather than as zeros — a missing input must never look like a bad
score.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, "/opt/realestate/scripts")

from flask import Flask, jsonify, render_template, request

from config import LOGS
from db import raw_conn
from http_cache import get_json

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout),
              logging.FileHandler(LOGS / "dashboard.log")],
)
log = logging.getLogger("dashboard")

app = Flask(__name__, template_folder=str(Path(__file__).parent / "templates"))

GEOCODER = "https://geocoding.geo.census.gov/geocoder/geographies/onelineaddress"


# ------------------------------------------------------------------ helpers
def q(sql: str, params: tuple = ()) -> list[dict]:
    with raw_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            if cur.description is None:
                return []
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]


def table_populated(name: str) -> bool:
    try:
        return bool(q(f"SELECT 1 FROM {name} LIMIT 1"))
    except Exception:
        return False


def layer_status() -> dict:
    """What's actually loaded. Drives the honesty banner on the UI."""
    return {
        "geometry":     table_populated("tract_geom"),
        "demographics": table_populated("acs_tract"),
        "pois":         table_populated("osm_poi"),
        "scores":       table_populated("tract_scores"),
        "opportunity_zones": table_populated("opportunity_zones"),
    }


# ------------------------------------------------------------------ routes
@app.route("/")
def index():
    return render_template("index.html", status=layer_status())


@app.route("/api/health")
def health():
    counts = {}
    for t in ("tract_geom", "acs_tract", "osm_poi", "tract_poi_counts",
              "tract_scores", "opportunity_zones"):
        try:
            counts[t] = q(f"SELECT count(*) AS n FROM {t}")[0]["n"]
        except Exception:
            counts[t] = None
    return jsonify({"ok": True, "row_counts": counts, "layers": layer_status()})


@app.route("/api/lookup")
def lookup():
    """Address -> tract -> everything we know."""
    address = (request.args.get("address") or "").strip()
    if not address:
        return jsonify({"error": "no address supplied"}), 400

    params = {
        "address": address,
        "benchmark": "Public_AR_Current",
        "vintage": "Current_Current",
        "format": "json",
        "layers": "all",
    }
    # Cached + throttled. Repeated lookups of the same address cost 0 requests.
    data = get_json(GEOCODER, params, ttl_days=180)
    if not data:
        return jsonify({"error": "geocoder unavailable or rate limited"}), 503

    matches = (data.get("result") or {}).get("addressMatches") or []
    if not matches:
        return jsonify({"error": "address not found", "address": address}), 404

    m = matches[0]
    tracts = (m.get("geographies") or {}).get("Census Tracts") or [{}]
    geoid = tracts[0].get("GEOID")
    if not geoid:
        return jsonify({"error": "no census tract for that address"}), 404

    return jsonify({
        "query": address,
        "matched_address": m.get("matchedAddress"),
        "lat": m["coordinates"]["y"],
        "lon": m["coordinates"]["x"],
        "geoid": geoid,
        "tract": tract_detail(geoid),
    })


def tract_detail(geoid: str) -> dict:
    out: dict = {"geoid": geoid}

    geo = q("""SELECT geoid, name, state_fips, county_fips, aland, awater,
                      intptlat, intptlon
               FROM tract_geom WHERE geoid = %s AND vintage = 2020""", (geoid,))
    out["geography"] = geo[0] if geo else None

    # ACS across every vintage we hold — this is the temporal spine.
    acs = q("""SELECT vintage, population, median_hh_income, median_gross_rent,
                      median_home_value, median_re_taxes, tenure_total,
                      tenure_renter, edu_total, edu_bachelors, edu_masters,
                      edu_professional, edu_doctorate, below_poverty,
                      median_rent_pct_income
               FROM acs_tract WHERE geoid = %s ORDER BY vintage""", (geoid,))
    out["acs_series"] = acs
    out["acs_latest"] = acs[-1] if acs else None

    if len(acs) >= 2:
        out["trends"] = compute_trends(acs)
    else:
        out["trends"] = None

    pois = q("""SELECT poi_class, count_in_tract, count_within_5k
                FROM tract_poi_counts WHERE geoid = %s
                ORDER BY count_within_5k DESC""", (geoid,))
    out["pois"] = pois or None

    sc = q("SELECT * FROM tract_scores WHERE geoid = %s", (geoid,))
    out["score"] = sc[0] if sc else None

    oz = q("""SELECT x.geoid_2010, x.area_weight
              FROM tract_xwalk_2010_2020 x
              JOIN opportunity_zones o ON o.geoid_2010 = x.geoid_2010
              WHERE x.geoid_2020 = %s
              ORDER BY x.area_weight DESC""", (geoid,))
    out["opportunity_zone"] = {
        "in_oz": bool(oz),
        "matched_2010_tracts": oz,
    } if table_populated("opportunity_zones") else None

    return out


def compute_trends(acs: list[dict]) -> dict:
    """
    CAGRs plus the rent-minus-income spread.

    That spread is the actual early-gentrification signal the source paper argues
    for and then doesn't implement — its code scores a single-year snapshot, which
    just rewards places that are currently cheap.
    """
    def cagr(series: list[tuple[int, float]]) -> float | None:
        pts = [(y, v) for y, v in series if v not in (None, 0)]
        if len(pts) < 2:
            return None
        (y0, v0), (y1, v1) = pts[0], pts[-1]
        n = y1 - y0
        if n <= 0 or v0 <= 0:
            return None
        return round(((v1 / v0) ** (1 / n) - 1) * 100, 2)

    rent = cagr([(r["vintage"], r["median_gross_rent"]) for r in acs])
    inc = cagr([(r["vintage"], r["median_hh_income"]) for r in acs])
    val = cagr([(r["vintage"], r["median_home_value"]) for r in acs])

    latest = acs[-1]
    rti = None
    if latest["median_gross_rent"] and latest["median_hh_income"]:
        rti = round(latest["median_gross_rent"] / (latest["median_hh_income"] / 12), 4)

    def edu_share(r: dict) -> float | None:
        # bachelor's OR HIGHER = 022+023+024+025. B15003_022E alone is
        # bachelor's only -- the source paper mislabels it.
        if not r.get("edu_total"):
            return None
        hi = sum(r.get(k) or 0 for k in
                 ("edu_bachelors", "edu_masters", "edu_professional", "edu_doctorate"))
        return round(100 * hi / r["edu_total"], 2)

    e0, e1 = edu_share(acs[0]), edu_share(acs[-1])

    return {
        "span": f"{acs[0]['vintage']}-{acs[-1]['vintage']}",
        "rent_cagr_pct": rent,
        "income_cagr_pct": inc,
        "home_value_cagr_pct": val,
        "rent_minus_income_spread": (round(rent - inc, 2)
                                     if rent is not None and inc is not None else None),
        "rent_to_income": rti,
        "rent_burden_flag": ("severe" if rti and rti > 0.5
                             else "burdened" if rti and rti > 0.3 else "ok") if rti else None,
        "edu_share_start_pct": e0,
        "edu_share_end_pct": e1,
        "edu_share_change_pp": (round(e1 - e0, 2)
                                if e0 is not None and e1 is not None else None),
    }


@app.route("/api/tract/<geoid>")
def api_tract(geoid: str):
    if not geoid.isdigit() or len(geoid) != 11:
        return jsonify({"error": "geoid must be 11 digits"}), 400
    return jsonify(tract_detail(geoid))


@app.route("/api/counties.geojson")
def counties_geojson():
    """National view. 3,143 simplified county polygons — light enough for a browser."""
    metric = request.args.get("metric", "score")
    if not table_populated("county_geom"):
        return jsonify({"type": "FeatureCollection", "features": [],
                        "note": "county_geom not built yet"})
    rows = q("""
        SELECT county_fips, name, ST_AsGeoJSON(geom_simple) AS gj,
               score, tract_count, scored_tracts
        FROM county_geom ORDER BY county_fips
    """)
    feats = []
    for r in rows:
        feats.append({
            "type": "Feature",
            "geometry": __import__("json").loads(r["gj"]),
            "properties": {
                "county_fips": r["county_fips"], "name": r["name"],
                "score": float(r["score"]) if r["score"] is not None else None,
                "tract_count": r["tract_count"],
                "scored_tracts": r["scored_tracts"],
            },
        })
    return jsonify({"type": "FeatureCollection", "features": feats, "metric": metric})


@app.route("/api/tracts.geojson")
def tracts_geojson():
    """Zoomed view. Strictly bbox-limited and capped — never ship 84k polygons."""
    try:
        w, s, e, n = (float(x) for x in (request.args.get("bbox") or "").split(","))
    except ValueError:
        return jsonify({"error": "bbox=west,south,east,north required"}), 400

    if (e - w) * (n - s) > 25:
        return jsonify({"type": "FeatureCollection", "features": [],
                        "note": "zoom in further"})

    rows = q("""
        SELECT t.geoid, t.name,
               ST_AsGeoJSON(ST_SimplifyPreserveTopology(t.geom, 0.0002)) AS gj,
               s.score, s.moran_cluster, s.is_opportunity_zone,
               s.rent_income_spread, s.data_completeness
        FROM tract_geom t
        LEFT JOIN tract_scores s ON s.geoid = t.geoid
        WHERE t.vintage = 2020
          AND t.geom && ST_MakeEnvelope(%s,%s,%s,%s,4326)
        LIMIT 4000
    """, (w, s, e, n))

    import json as _json
    feats = [{
        "type": "Feature",
        "geometry": _json.loads(r["gj"]),
        "properties": {
            "geoid": r["geoid"], "name": r["name"],
            "score": float(r["score"]) if r["score"] is not None else None,
            "moran_cluster": r["moran_cluster"],
            "is_oz": r["is_opportunity_zone"],
            "spread": float(r["rent_income_spread"]) if r["rent_income_spread"] is not None else None,
            "completeness": float(r["data_completeness"]) if r["data_completeness"] is not None else None,
        },
    } for r in rows]
    return jsonify({"type": "FeatureCollection", "features": feats})


@app.route("/api/top")
def top_tracts():
    """Ranked leaderboard — the actionable output."""
    limit = min(int(request.args.get("limit", 100)), 500)
    state = request.args.get("state")
    where = "WHERE s.data_completeness >= 0.6"
    params: list = []
    if state:
        where += " AND t.state_fips = %s"
        params.append(state)
    params.append(limit)
    rows = q(f"""
        SELECT s.geoid, t.name, t.state_fips, t.county_fips, s.score,
               s.moran_cluster, s.is_opportunity_zone, s.rent_income_spread,
               t.intptlat, t.intptlon
        FROM tract_scores s
        JOIN tract_geom t ON t.geoid = s.geoid AND t.vintage = 2020
        {where}
        ORDER BY s.score DESC NULLS LAST
        LIMIT %s
    """, tuple(params))
    return jsonify({"count": len(rows), "tracts": rows})


if __name__ == "__main__":
    # 5005/5006/5007 already owned by the trading dashboard, crypto validator,
    # and the biotech fda_dashboard.py respectively — confirmed live on the
    # Beelink before picking this port.
    app.run(host="0.0.0.0", port=5008, debug=False)
