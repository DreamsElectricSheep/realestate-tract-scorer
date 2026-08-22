#!/usr/bin/env python3
"""
Build the POI layer from a local OpenStreetMap extract.

Why not Overpass (what the source paper does):
  - The public endpoint allows ~2 concurrent slots and a modest daily volume.
    85,000 tracts x 8 POI classes = 680,000 queries. You get banned, not data.
  - `out count;` on a union returns ONE aggregate number. The paper's code assigns
    that same number to universities, transit stops AND industrial nodes.
  - `node[building=industrial]` matches almost nothing: buildings are ways and
    relations, not nodes. Same for university campuses. We query nwr and take
    centroids, so polygons actually count.

Three resumable steps:
    python3 ingest_osm.py download   # ~14 GB, once
    python3 ingest_osm.py filter     # osmium tags-filter -> small pbf -> geojsonseq
    python3 ingest_osm.py load       # -> osm_poi, then tract_poi_counts via PostGIS
    python3 ingest_osm.py all
"""
from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
from pathlib import Path

from config import GEOFABRIK_US, LOGS, OSM_POI_FILTERS, PROXIMITY_RADIUS_M, RAW
from db import log_ingest, raw_conn
from http_cache import download_file

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout),
              logging.FileHandler(LOGS / "ingest_osm.log")],
)
log = logging.getLogger("ingest_osm")

OSM_DIR = RAW / "osm"
PBF_FULL = OSM_DIR / "us-latest.osm.pbf"
PBF_POI = OSM_DIR / "us-poi.osm.pbf"
GEOJSON = OSM_DIR / "us-poi.geojsonseq"

# tag -> poi_class, inverted from config for fast lookup during load
TAG_TO_CLASS: dict[tuple[str, str], str] = {}
for _cls, _tags in OSM_POI_FILTERS.items():
    for _t in _tags:
        _k, _v = _t.split("=", 1)
        TAG_TO_CLASS[(_k, _v)] = _cls


def run(cmd: list[str], desc: str) -> bool:
    log.info("%s: %s", desc, " ".join(cmd[:6]) + (" ..." if len(cmd) > 6 else ""))
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=14400)
    except subprocess.TimeoutExpired:
        log.error("%s TIMED OUT after 4h", desc)
        return False
    if p.returncode != 0:
        log.error("%s failed (rc=%d): %s", desc, p.returncode, (p.stderr or "")[-1500:])
        return False
    if p.stderr.strip():
        log.info("%s stderr: %s", desc, p.stderr.strip()[-400:])
    return True


def step_download() -> bool:
    OSM_DIR.mkdir(parents=True, exist_ok=True)
    log.info("downloading US OSM extract (~14 GB, one time, resumes are free "
             "because an existing file is never re-pulled)")
    return download_file(GEOFABRIK_US, PBF_FULL, min_bytes=1_000_000_000) is not None


def step_filter() -> bool:
    if not PBF_FULL.exists():
        log.error("%s missing — run `download` first", PBF_FULL)
        return False

    # nwr/ = match nodes, ways AND relations. This is the fix for polygon POIs.
    exprs = [f"nwr/{t}" for tags in OSM_POI_FILTERS.values() for t in tags]
    if not run(["osmium", "tags-filter", "--overwrite", "-o", str(PBF_POI),
                str(PBF_FULL), *exprs], "osmium tags-filter"):
        return False
    log.info("filtered pbf: %.1f MB (from %.1f GB)",
             PBF_POI.stat().st_size / 1e6, PBF_FULL.stat().st_size / 1e9)

    # export assembles areas and emits one GeoJSON feature per line
    if not run(["osmium", "export", "--overwrite", "-f", "geojsonseq",
                "--add-unique-id=type_id", "-o", str(GEOJSON), str(PBF_POI)],
               "osmium export"):
        return False
    log.info("geojsonseq: %.1f MB", GEOJSON.stat().st_size / 1e6)
    return True


def classify(props: dict) -> str | None:
    for k, v in props.items():
        cls = TAG_TO_CLASS.get((k, v))
        if cls:
            return cls
    return None


def centroid(geom: dict) -> tuple[float, float] | None:
    """Representative point. Polygons -> centroid, so campuses count as one POI."""
    t = geom.get("type")
    c = geom.get("coordinates")
    if not c:
        return None
    try:
        if t == "Point":
            return float(c[0]), float(c[1])
        if t == "LineString":
            pts = c
        elif t == "Polygon":
            pts = c[0]
        elif t == "MultiPolygon":
            pts = c[0][0]
        elif t == "MultiLineString":
            pts = c[0]
        else:
            return None
        xs = [float(p[0]) for p in pts]
        ys = [float(p[1]) for p in pts]
        return sum(xs) / len(xs), sum(ys) / len(ys)
    except (TypeError, ValueError, IndexError):
        return None


def step_load(batch: int = 20000) -> bool:
    if not GEOJSON.exists():
        log.error("%s missing — run `filter` first", GEOJSON)
        return False

    with raw_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("TRUNCATE osm_poi")
        conn.commit()

    from psycopg2.extras import execute_values

    total = 0
    skipped = 0
    rows: list[tuple] = []
    conn = raw_conn()
    cur = conn.cursor()
    sql = ("INSERT INTO osm_poi (osm_id, poi_class, name, geom) VALUES %s")
    tmpl = "(%s,%s,%s,ST_SetSRID(ST_MakePoint(%s,%s),4326))"

    with open(GEOJSON, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line or line[0] != "{":
                continue
            try:
                feat = json.loads(line)
            except json.JSONDecodeError:
                skipped += 1
                continue
            props = feat.get("properties") or {}
            cls = classify(props)
            if not cls:
                skipped += 1
                continue
            pt = centroid(feat.get("geometry") or {})
            if not pt:
                skipped += 1
                continue
            raw_id = str(feat.get("id", "0"))
            digits = "".join(ch for ch in raw_id if ch.isdigit())
            rows.append((int(digits or 0), cls, (props.get("name") or "")[:200],
                         pt[0], pt[1]))
            if len(rows) >= batch:
                execute_values(cur, sql, rows, template=tmpl, page_size=batch)
                conn.commit()
                total += len(rows)
                rows.clear()
                log.info("  loaded %d POIs", total)

    if rows:
        execute_values(cur, sql, rows, template=tmpl, page_size=batch)
        conn.commit()
        total += len(rows)
    cur.close()
    conn.close()

    log.info("osm_poi loaded: %d POIs (%d features skipped as unclassified)",
             total, skipped)
    log_ingest("osm_poi", "ok", total, 0, f"skipped={skipped}")
    return total > 0


def step_counts() -> bool:
    """
    Precompute per-tract POI counts once, in PostGIS. This is what replaces
    680,000 Overpass calls with a handful of local spatial joins.

    Geography casts give true metres without picking a projected CRS per region.
    """
    log.info("computing tract POI counts (in-tract, within %dm, nearest)",
             PROXIMITY_RADIUS_M)
    sql = f"""
    TRUNCATE tract_poi_counts;

    WITH classes AS (SELECT DISTINCT poi_class FROM osm_poi),
    grid AS (
        SELECT t.geoid, c.poi_class
        FROM tract_geom t CROSS JOIN classes c
        WHERE t.vintage = 2020
    ),
    in_tract AS (
        SELECT t.geoid, p.poi_class, count(*)::int AS n
        FROM tract_geom t
        JOIN osm_poi p ON ST_Intersects(t.geom, p.geom)
        WHERE t.vintage = 2020
        GROUP BY 1,2
    ),
    near AS (
        -- The `p.geom && ST_Expand(..., 0.15)` clause is redundant with
        -- ST_DWithin in principle (0.15deg ~ 16km at Alaska's northernmost
        -- tracts, well past the 5km radius) but NOT redundant in practice:
        -- PostGIS's planner does not reliably push ST_DWithin(geography,...)
        -- down to the osm_poi GiST index on its own here, and silently falls
        -- back to a per-tract sequential scan of all 2M POIs (84,415 tracts x
        -- 2,059,874 POIs — the actual cause of three straight 4h timeouts).
        -- The explicit `&&` on bare geometry columns matches the index's
        -- native operator class and forces the bounding-box prefilter;
        -- ST_DWithin still does the exact geodesic check afterward.
        SELECT t.geoid, p.poi_class, count(*)::int AS n
        FROM tract_geom t
        JOIN osm_poi p
          ON p.geom && ST_Expand(
               ST_SetSRID(ST_MakePoint(t.intptlon, t.intptlat),4326), 0.15)
         AND ST_DWithin(
               ST_SetSRID(ST_MakePoint(t.intptlon, t.intptlat),4326)::geography,
               p.geom::geography,
               {PROXIMITY_RADIUS_M})
        WHERE t.vintage = 2020
        GROUP BY 1,2
    )
    INSERT INTO tract_poi_counts (geoid, poi_class, count_in_tract, count_within_5k)
    SELECT g.geoid, g.poi_class,
           COALESCE(i.n,0), COALESCE(nr.n,0)
    FROM grid g
    LEFT JOIN in_tract i ON i.geoid = g.geoid AND i.poi_class = g.poi_class
    LEFT JOIN near    nr ON nr.geoid = g.geoid AND nr.poi_class = g.poi_class;
    """
    with raw_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SET statement_timeout = '4h'")
            cur.execute(sql)
            cur.execute("SELECT count(*) FROM tract_poi_counts")
            n = cur.fetchone()[0]
        conn.commit()
    log.info("tract_poi_counts: %d rows", n)
    log_ingest("tract_poi_counts", "ok", n, 0, "")
    return n > 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("step", choices=["download", "filter", "load", "counts", "all"])
    args = ap.parse_args()

    steps = {
        "download": [step_download],
        "filter": [step_filter],
        "load": [step_load],
        "counts": [step_counts],
        "all": [step_download, step_filter, step_load, step_counts],
    }[args.step]

    for fn in steps:
        if not fn():
            log.error("step %s failed — stopping", fn.__name__)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
