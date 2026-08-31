#!/usr/bin/env python3
"""
Real Estate Scorer dashboard.

  http://192.168.1.252:5011

Four things it does:
  1. Address lookup  — paste an address, get everything we know about its tract.
  2. Hot zone map    — national choropleth (county), drilling to tract on zoom.
  3. Deal calculator — underwrite one property from listing numbers.
  4. Deal scanner    — rank every tract in the country by cash-on-cash return
                       for a given down payment and mortgage size.

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

from config import LOGS, MIN_COMPLETENESS, SCORE_COMPONENTS
from db import raw_conn, engine
from http_cache import get_json

# Only pulled in for this one job: reconciling the 2010/2020 tract boundary
# redesign so a single tract's ACS trend actually spans 2017-2023 instead of
# silently collapsing to whatever years happen to share its exact 2020 geoid.
# Same bug class the audit fixed in score_tracts.py, different code path --
# this endpoint queried acs_tract raw with no crosswalk at all.
import pandas as pd
from acs_boundaries import unify_acs_boundaries

ACS_TREND_VALUE_COLS = [
    "population", "median_hh_income", "median_gross_rent", "median_home_value",
    "median_re_taxes", "tenure_total", "tenure_renter", "edu_total",
    "edu_bachelors", "edu_masters", "edu_professional", "edu_doctorate",
    "below_poverty", "median_rent_pct_income",
]


def load_acs_series_unified(geoid: str) -> list[dict]:
    """
    Full 2017-2023 ACS series for a 2020-boundary tract, with pre-2020
    vintages reprojected from their 2010-boundary source(s). Without this,
    any tract touched by the 2020 redesign only shows whichever native
    vintages happen to share its exact geoid -- for a newly-split tract,
    sometimes just one or two years, silently mislabeled as "the trend."
    """
    cols_sql = ", ".join(ACS_TREND_VALUE_COLS)
    native = pd.read_sql(
        f"SELECT geoid, vintage, {cols_sql} FROM acs_tract "
        f"WHERE geoid = %(g)s AND vintage >= 2020",
        engine(), params={"g": geoid},
    )
    contributors = pd.read_sql(
        f"SELECT geoid, vintage, {cols_sql} FROM acs_tract "
        f"WHERE vintage < 2020 AND geoid IN "
        f"(SELECT geoid_2010 FROM tract_xwalk_2010_2020 WHERE geoid_2020 = %(g)s)",
        engine(), params={"g": geoid},
    )
    if contributors.empty:
        combined = native
    else:
        crosswalked = unify_acs_boundaries(
            pd.concat([native, contributors], ignore_index=True), ACS_TREND_VALUE_COLS
        )
        combined = crosswalked[crosswalked["geoid"] == geoid] if not crosswalked.empty else native

    if combined.empty:
        return []
    combined = combined.sort_values("vintage")
    # NaN isn't JSON-serializable; the rest of the app already treats missing
    # ACS fields as None, not 0, so match that rather than coercing to zero.
    return combined.where(pd.notna(combined), None).to_dict("records")


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout),
              logging.FileHandler(LOGS / "dashboard.log")],
)
log = logging.getLogger("dashboard")

app = Flask(__name__, template_folder=str(Path(__file__).parent / "templates"))
# debug=False (below) disables Jinja's auto-reload by default, which silently
# serves a stale compiled template after an index.html edit until the service
# is restarted -- bit us once already. This is a single-user LAN dashboard,
# so the tiny per-request recompile cost is irrelevant; correctness on edit
# is worth more than it.
app.config["TEMPLATES_AUTO_RELOAD"] = True

GEOCODER = "https://geocoding.geo.census.gov/geocoder/geographies/onelineaddress"

# Owner's home base (Hamden, CT) -- not a target market (property taxes too
# high there), used only as the anchor point for the "how far would I have to
# drive" filter. Town-center coordinates, precise enough for a mileage filter.
HOME_BASE = (41.3959, -72.8968)


def distance_filter_sql(alias: str = "t") -> tuple[str, list]:
    """
    Returns (sql_fragment, params) for an optional max-miles-from-home-base
    filter, or ("", []) if not requested. Uses geography distance (meters),
    converted to miles, against the tract's Census-published internal point.
    """
    max_miles = request.args.get("max_miles", type=float)
    if not max_miles or max_miles <= 0:
        return "", []
    return (
        f" AND ST_Distance("
        f"ST_SetSRID(ST_MakePoint({alias}.intptlon,{alias}.intptlat),4326)::geography, "
        f"ST_SetSRID(ST_MakePoint(%s,%s),4326)::geography"
        f") / 1609.34 <= %s",
        [HOME_BASE[1], HOME_BASE[0], max_miles],
    )


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
    # Weights come from config so the "Why this score" panel can never disagree
    # with what the scorer actually computed.
    return render_template("index.html", status=layer_status(),
                           score_components=SCORE_COMPONENTS,
                           min_completeness=MIN_COMPLETENESS)


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

    geo = q("""SELECT t.geoid, t.name, t.state_fips, t.county_fips, t.aland, t.awater,
                      t.intptlat, t.intptlon, c.name AS county_name,
                      ST_XMin(t.geom) AS bbox_west, ST_XMax(t.geom) AS bbox_east,
                      ST_YMin(t.geom) AS bbox_south, ST_YMax(t.geom) AS bbox_north
               FROM tract_geom t
               LEFT JOIN county_geom c ON c.county_fips = t.state_fips || t.county_fips
               WHERE t.geoid = %s AND t.vintage = 2020""", (geoid,))
    out["geography"] = geo[0] if geo else None

    # ACS across every vintage we hold, boundary-unified -- this is the
    # temporal spine. A raw per-geoid query here used to silently return only
    # whichever vintages happen to share this tract's exact 2020 geoid; for a
    # tract split or reshaped in the 2020 redesign, that could be a single
    # year mislabeled as "the trend."
    acs = load_acs_series_unified(geoid)
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
    if not table_populated("county_geom"):
        return jsonify({"type": "FeatureCollection", "features": [],
                        "note": "county_geom not built yet"})
    max_miles = request.args.get("max_miles", type=float)
    state = request.args.get("state")
    clauses, params = [], []
    if state:
        # county_fips is the 5-digit national FIPS (2-digit state + 3-digit
        # county); no separate state column on this table.
        clauses.append("left(county_fips,2) = %s")
        params.append(state)
    if max_miles and max_miles > 0:
        clauses.append("ST_Distance(ST_Centroid(geom_simple)::geography, "
                        "ST_SetSRID(ST_MakePoint(%s,%s),4326)::geography) / 1609.34 <= %s")
        params.extend([HOME_BASE[1], HOME_BASE[0], max_miles])
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = q(f"""
        SELECT county_fips, name, ST_AsGeoJSON(geom_simple) AS gj,
               score, tract_count, scored_tracts
        FROM county_geom{where} ORDER BY county_fips
    """, tuple(params))
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
    return jsonify({"type": "FeatureCollection", "features": feats})


def apply_common_filters(where: str, params: list, alias: str = "t") -> tuple[str, list]:
    """Shared filter set for /api/tracts.geojson and /api/top: state, score
    range, Moran cluster, and distance from home base. Kept in one place so
    the map and the leaderboard can never silently drift apart on what
    "filtered" means -- state was previously only wired into /api/top, so
    picking a state correctly filtered the leaderboard but left the map
    showing neighboring-state tracts inside the same viewport (e.g. Long
    Island tracts still rendering with a Connecticut filter applied)."""
    state = request.args.get("state")
    if state:
        where += f" AND {alias}.state_fips = %s"
        params.append(state)
    max_home_value = request.args.get("max_home_value", type=float)
    if max_home_value:
        # Requires the caller's query to LEFT JOIN LATERAL the latest-vintage
        # ACS row as `latest_acs` (see tracts_geojson/top_tracts) -- tract-level
        # median home value, the only home-price signal this free tier has.
        where += " AND latest_acs.median_home_value IS NOT NULL AND latest_acs.median_home_value <= %s"
        params.append(max_home_value)
    min_score = request.args.get("min_score", type=float)
    if min_score is not None:
        where += " AND s.score >= %s"
        params.append(min_score)
    max_score = request.args.get("max_score", type=float)
    if max_score is not None:
        where += " AND s.score <= %s"
        params.append(max_score)
    clusters = request.args.get("cluster")  # comma-separated: HH,LH,...
    if clusters:
        vals = [c.strip().upper() for c in clusters.split(",") if c.strip()]
        if vals:
            where += f" AND s.moran_cluster = ANY(%s)"
            params.append(vals)
    dist_sql, dist_params = distance_filter_sql(alias)
    where += dist_sql
    params.extend(dist_params)
    return where, params


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

    where = "WHERE t.vintage = 2020 AND t.geom && ST_MakeEnvelope(%s,%s,%s,%s,4326)"
    params: list = [w, s, e, n]
    where, params = apply_common_filters(where, params)

    rows = q(f"""
        SELECT t.geoid, t.name, c.name AS county_name,
               ST_AsGeoJSON(ST_SimplifyPreserveTopology(t.geom, 0.0002)) AS gj,
               s.score, s.moran_cluster, s.is_opportunity_zone,
               s.rent_income_spread, s.data_completeness,
               latest_acs.median_home_value, latest_acs.median_gross_rent,
               ST_Distance(
                 ST_SetSRID(ST_MakePoint(t.intptlon,t.intptlat),4326)::geography,
                 ST_SetSRID(ST_MakePoint(%s,%s),4326)::geography) / 1609.34 AS distance_mi
        FROM tract_geom t
        LEFT JOIN tract_scores s ON s.geoid = t.geoid
        LEFT JOIN county_geom c ON c.county_fips = t.state_fips || t.county_fips
        LEFT JOIN LATERAL (
            SELECT median_home_value, median_gross_rent FROM acs_tract a
            WHERE a.geoid = t.geoid ORDER BY a.vintage DESC LIMIT 1
        ) latest_acs ON true
        {where}
        LIMIT 4000
    """, tuple([HOME_BASE[1], HOME_BASE[0]] + params))

    import json as _json
    feats = [{
        "type": "Feature",
        "geometry": _json.loads(r["gj"]),
        "properties": {
            "geoid": r["geoid"], "name": r["name"], "county_name": r["county_name"],
            "score": float(r["score"]) if r["score"] is not None else None,
            "moran_cluster": r["moran_cluster"],
            "is_oz": r["is_opportunity_zone"],
            "spread": float(r["rent_income_spread"]) if r["rent_income_spread"] is not None else None,
            "completeness": float(r["data_completeness"]) if r["data_completeness"] is not None else None,
            "distance_mi": round(float(r["distance_mi"]), 1) if r["distance_mi"] is not None else None,
            "median_home_value": r["median_home_value"],
            "median_gross_rent": r["median_gross_rent"],
        },
    } for r in rows]
    return jsonify({"type": "FeatureCollection", "features": feats})


@app.route("/api/top")
def top_tracts():
    """Ranked leaderboard — the actionable output."""
    limit = min(int(request.args.get("limit", 100)), 500)
    where = "WHERE s.data_completeness >= 0.6"
    params: list = []
    where, params = apply_common_filters(where, params)
    params.append(limit)
    rows = q(f"""
        SELECT s.geoid, t.name, t.state_fips, t.county_fips, c.name AS county_name, s.score,
               s.moran_cluster, s.is_opportunity_zone, s.rent_income_spread,
               t.intptlat, t.intptlon,
               latest_acs.median_home_value, latest_acs.median_gross_rent,
               ST_Distance(
                 ST_SetSRID(ST_MakePoint(t.intptlon,t.intptlat),4326)::geography,
                 ST_SetSRID(ST_MakePoint(%s,%s),4326)::geography) / 1609.34 AS distance_mi
        FROM tract_scores s
        JOIN tract_geom t ON t.geoid = s.geoid AND t.vintage = 2020
        LEFT JOIN county_geom c ON c.county_fips = t.state_fips || t.county_fips
        LEFT JOIN LATERAL (
            SELECT median_home_value, median_gross_rent FROM acs_tract a
            WHERE a.geoid = t.geoid ORDER BY a.vintage DESC LIMIT 1
        ) latest_acs ON true
        {where}
        ORDER BY s.score DESC NULLS LAST
        LIMIT %s
    """, tuple([HOME_BASE[1], HOME_BASE[0]] + params))
    for r in rows:
        if r.get("distance_mi") is not None:
            r["distance_mi"] = round(float(r["distance_mi"]), 1)
    return jsonify({"count": len(rows), "tracts": rows})


@app.route("/api/scan")
def deal_scan():
    """
    Nationwide inversion of the single-property deal calculator: instead of
    typing in one listing's numbers, run the same cap-rate/cash-flow math
    against every tract's own ACS median home value and median gross rent,
    using the caller's actual financing plan (fixed down payment $ + max
    loan $), and rank by cash-on-cash return.

    Same approximation the single-property calculator uses, applied at
    scale: "price" and "rent" are the tract's median SINGLE-unit values,
    scaled by an assumed unit count -- a nationwide screen to prioritize
    where to look, not a substitute for real listing data. Property tax
    is the tract's own actual median tax bill (exact, since price ==
    that tract's own median_home_value by construction -- no ratio
    scaling needed, unlike the single-calculator's arbitrary-price case).
    Insurance has no free per-tract data source, so it's a flat estimate
    applied uniformly everywhere -- real insurance cost varies enormously
    by geography (coastal/flood-zone markets run far higher), which is
    exactly the kind of thing that would flip a "profitable" result at
    this screening stage. Treat this as a first pass, not a final answer.
    """
    down_payment = request.args.get("down_payment", type=float)
    max_loan = request.args.get("max_loan", type=float)
    if not down_payment or down_payment <= 0 or max_loan is None or max_loan < 0:
        return jsonify({"error": "down_payment (>0) and max_loan (>=0) are required"}), 400

    rate = request.args.get("rate", default=7.0, type=float)
    term_years = request.args.get("term", default=30, type=int)
    # NOT configurable, deliberately. This used to accept a `units` multiplier
    # that scaled rent but NOT price -- since `price` here is the tract's
    # median value for ONE home, setting units=3 tripled income against an
    # unchanged purchase price and produced impossible cap rates (measured:
    # 24.4% at units=3 vs 10.3% at units=1 on the same scan). There is no
    # honest way to scale a single-home median to a multi-unit building
    # without real listing data, so the screen is strictly one-unit and the
    # per-property Deal Calculator handles real multi-unit deals instead.
    units = 1.0
    vacancy = request.args.get("vacancy", default=7.0, type=float)
    opex = request.args.get("opex", default=40.0, type=float)
    insurance = request.args.get("insurance", default=1500.0, type=float)
    state = request.args.get("state")
    limit = min(int(request.args.get("limit", 100)), 300)
    max_miles = request.args.get("max_miles", type=float)

    max_price = down_payment + max_loan

    extra_where = ""
    params: dict = {
        "max_price": max_price, "units": units, "vacancy": vacancy, "opex": opex,
        "insurance": insurance, "down_payment": down_payment, "rate": rate,
        "term_months": term_years * 12, "limit": limit,
    }
    if state:
        extra_where += " AND t.state_fips = %(state)s"
        params["state"] = state
    min_score = request.args.get("min_score", type=float)
    if min_score is not None:
        extra_where += " AND sc.score >= %(min_score)s"
        params["min_score"] = min_score
    if max_miles and max_miles > 0:
        extra_where += (" AND ST_Distance(ST_SetSRID(ST_MakePoint(t.intptlon,t.intptlat),4326)::geography,"
                         " ST_SetSRID(ST_MakePoint(%(home_lon)s,%(home_lat)s),4326)::geography) / 1609.34"
                         " <= %(max_miles)s")
        params["home_lon"], params["home_lat"], params["max_miles"] = HOME_BASE[1], HOME_BASE[0], max_miles

    sql = f"""
    WITH base AS (
        SELECT t.geoid, t.name, t.state_fips, c.name AS county_name,
               t.intptlat, t.intptlon,
               a.median_home_value AS price, a.median_gross_rent AS rent_per_unit,
               COALESCE(a.median_re_taxes, 0) AS tax,
               -- The neighborhood score rides along with the money math. Without
               -- this join the scanner ranked purely on yield and could put a
               -- bottom-decile tract at the top with no hint of it -- the two
               -- halves of the product never spoke to each other.
               sc.score, sc.moran_cluster, sc.data_completeness
        FROM tract_geom t
        JOIN LATERAL (
            SELECT median_home_value, median_gross_rent, median_re_taxes
            FROM acs_tract x WHERE x.geoid = t.geoid ORDER BY x.vintage DESC LIMIT 1
        ) a ON true
        LEFT JOIN county_geom c ON c.county_fips = t.state_fips || t.county_fips
        LEFT JOIN tract_scores sc ON sc.geoid = t.geoid
        WHERE t.vintage = 2020
          -- Floor, not just > 0: tracts with too few owner-occupied units for
          -- a real median (Census suppression/small-sample artifacts) report
          -- absurd values like $9,999 in Manhattan. Those aren't real deals --
          -- unfiltered, their mechanically enormous cash-on-cash ratios would
          -- dominate the top of every ranking.
          AND a.median_home_value >= 50000 AND a.median_home_value <= %(max_price)s
          -- 3501 is ACS's top-code sentinel for this field ("$3,500 or more"),
          -- not a real value -- confirmed 2026-08-22, 1,130 tracts report
          -- exactly 3501 vs. 17 for the next most common nearby value. Paired
          -- with a low home value it produces impossible rent-to-price ratios.
          AND a.median_gross_rent > 0 AND a.median_gross_rent < 3501
          -- Direct gross-yield ceiling, not just the downstream cap-rate one
          -- below: real markets essentially never sustain an annual gross
          -- rent-to-price ratio above roughly a fifth of the price (the
          -- classic "monthly rent equals one point of the price" heuristic
          -- is already considered an aggressive cash-flow market, and that
          -- is well under this ceiling). Above it is the owner/renter
          -- housing-stock mismatch described in deal_scan()'s docstring, not
          -- a real buyable combination -- confirmed against a real,
          -- well-sampled tract (563 owner-occupied units) whose ratio was
          -- roughly double this ceiling before the filter existed.
          AND a.median_gross_rent * 12 <= a.median_home_value * 0.21
          {extra_where}
    ),
    econ AS (
        SELECT *,
               (rent_per_unit * %(units)s * 12)::numeric AS gpr,
               (rent_per_unit * %(units)s * 12 * (1 - %(vacancy)s/100.0))::numeric AS egi,
               LEAST(price, %(down_payment)s) AS down_here,
               GREATEST(price - %(down_payment)s, 0) AS loan
        FROM base
    ),
    noi_calc AS (
        SELECT *, (egi - egi*%(opex)s/100.0 - tax - %(insurance)s) AS noi
        FROM econ
    ),
    finance AS (
        SELECT *,
               CASE WHEN loan <= 0 THEN 0
                    WHEN %(rate)s = 0 THEN loan / %(term_months)s
                    ELSE loan * ((%(rate)s/1200.0) * power(1+%(rate)s/1200.0, %(term_months)s))
                              / (power(1+%(rate)s/1200.0, %(term_months)s) - 1)
               END AS monthly_pi
        FROM noi_calc
    ),
    final AS (
        SELECT geoid, name, state_fips, county_name, intptlat, intptlon,
               score, moran_cluster, data_completeness,
               price, rent_per_unit, tax, gpr, egi, noi,
               (noi/price)*100 AS cap_rate,
               down_here, loan, monthly_pi, monthly_pi*12 AS annual_debt_service,
               (noi - monthly_pi*12) AS annual_cash_flow,
               CASE WHEN down_here > 0 THEN ((noi - monthly_pi*12)/down_here)*100 END AS cash_on_cash,
               CASE WHEN monthly_pi*12 > 0 THEN noi/(monthly_pi*12) END AS dscr
        FROM finance
    )
    -- Defense-in-depth: real-world multifamily cap rates run roughly 4 to 8
    -- points (mid-7 in the riskiest sub-sectors per current market data);
    -- anything above 25 almost certainly means a data artifact slipped past
    -- the home-value/rent floors above, not a genuine once-in-a-lifetime deal.
    SELECT * FROM final
    WHERE cap_rate <= 25
    ORDER BY cash_on_cash DESC NULLS LAST
    LIMIT %(limit)s
    """
    rows = q(sql, params)
    for r in rows:
        for k, v in list(r.items()):
            if isinstance(v, float):
                r[k] = round(v, 2)
    return jsonify({
        "count": len(rows), "max_price": max_price,
        "assumptions": {"rate": rate, "term_years": term_years, "units": units,
                         "vacancy_pct": vacancy, "opex_pct": opex, "insurance": insurance},
        "tracts": rows,
    })


if __name__ == "__main__":
    # 5005/5006/5007/5008 already owned by the trading dashboard, crypto
    # validator, biotech fda_dashboard.py, and (until 2026-08-31) this app's own
    # old port -- moved off 5008 after a WSL2 networking fault left it in a
    # perpetual EADDRINUSE state after a host reboot (nothing in this WSL
    # instance's /proc/net/tcp held it, no Windows-side listener or portproxy
    # rule either -- a phantom kernel-level reservation, only clearable with a
    # full `wsl --shutdown`, which was not done here since it would also kill
    # the trading fleet sharing this VM). See CLAUDE_MEMORY.md.
    app.run(host="0.0.0.0", port=5011, debug=False)
