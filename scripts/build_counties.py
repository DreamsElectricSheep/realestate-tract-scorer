#!/usr/bin/env python3
"""
Build the county layer that backs the national hot-zone view.

84,415 tract polygons is far too many for a browser to render at once, so the
national view is county-level (3,143 polygons, simplified) and the map swaps to
bbox-limited tracts once you zoom past county scale.

County scores are the mean of their scored tracts, and we keep scored_tracts /
tract_count alongside so a county coloured off two scored tracts is visibly
distinguishable from one coloured off two hundred.

Usage:
    python3 build_counties.py            # download geometry + refresh scores
    python3 build_counties.py --refresh  # scores only (fast, no download)
"""
from __future__ import annotations

import argparse
import logging
import sys

import geopandas as gpd
from shapely.geometry import MultiPolygon

from config import LOGS, RAW, TIGER_YEAR_2020
from db import engine, log_ingest, raw_conn
from http_cache import download_file

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout),
              logging.FileHandler(LOGS / "build_counties.log")],
)
log = logging.getLogger("build_counties")

COUNTY_URL = ("https://www2.census.gov/geo/tiger/TIGER{ty}/COUNTY/"
              "tl_{ty}_us_county.zip")

DDL = """
CREATE TABLE IF NOT EXISTS county_geom (
    county_fips   TEXT PRIMARY KEY,     -- 5-digit state+county
    state_fips    TEXT,
    name          TEXT,
    geom          GEOMETRY(MultiPolygon, 4326),
    geom_simple   GEOMETRY(MultiPolygon, 4326),
    score         NUMERIC(6,2),
    tract_count   INT,
    scored_tracts INT
);
CREATE INDEX IF NOT EXISTS idx_county_gist ON county_geom USING GIST (geom);
"""


def as_multipolygon(g):
    if g is None or g.is_empty:
        return None
    if g.geom_type == "Polygon":
        return MultiPolygon([g])
    return g if g.geom_type == "MultiPolygon" else None


def load_geometry() -> int:
    url = COUNTY_URL.format(ty=TIGER_YEAR_2020)
    dest = RAW / "tiger" / f"tl_{TIGER_YEAR_2020}_us_county.zip"
    path = download_file(url, dest, min_bytes=100_000)
    if not path:
        log.error("county shapefile download failed")
        return 0

    gdf = gpd.read_file(f"zip://{path}").to_crs("EPSG:4326")
    geom_name = gdf.geometry.name
    gdf.columns = [c if c == geom_name else c.upper() for c in gdf.columns]

    # Keep the 50 states + DC, matching the tract layer exactly.
    gdf = gdf[gdf["STATEFP"].astype(str).str.zfill(2) <= "56"]
    gdf = gdf[~gdf["STATEFP"].isin(["60", "66", "69", "72", "78"])]

    out = gpd.GeoDataFrame({
        "county_fips": gdf["GEOID"].astype(str),
        "state_fips": gdf["STATEFP"].astype(str),
        "name": gdf["NAMELSAD"].astype(str),
    }, geometry=gdf.geometry.apply(as_multipolygon), crs="EPSG:4326")
    out = out[out.geometry.notna()].rename_geometry("geom")

    with raw_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(DDL)
            cur.execute("TRUNCATE county_geom")
        conn.commit()

    out.to_postgis("county_geom_tmp", engine(), if_exists="replace", index=False)

    with raw_conn() as conn:
        with conn.cursor() as cur:
            # 0.01 deg ~ 1 km: plenty for a national overview, ~10x lighter payload.
            cur.execute("""
                INSERT INTO county_geom (county_fips, state_fips, name, geom, geom_simple)
                SELECT county_fips, state_fips, name, geom,
                       ST_Multi(ST_SimplifyPreserveTopology(geom, 0.01))
                FROM county_geom_tmp
                ON CONFLICT (county_fips) DO NOTHING
            """)
            cur.execute("DROP TABLE county_geom_tmp")
        conn.commit()

    log.info("county geometry loaded: %d counties", len(out))
    return len(out)


def refresh_scores() -> int:
    """Roll tract scores up to county. Safe to run before any scores exist."""
    with raw_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(DDL)
            cur.execute("""
                WITH agg AS (
                    SELECT t.state_fips || t.county_fips AS cf,
                           count(*)                                        AS tract_count,
                           count(s.score)                                  AS scored_tracts,
                           avg(s.score) FILTER (WHERE s.data_completeness >= 0.6) AS score
                    FROM tract_geom t
                    LEFT JOIN tract_scores s ON s.geoid = t.geoid
                    WHERE t.vintage = 2020
                    GROUP BY 1
                )
                UPDATE county_geom c
                SET score = round(agg.score::numeric, 2),
                    tract_count = agg.tract_count,
                    scored_tracts = agg.scored_tracts
                FROM agg WHERE agg.cf = c.county_fips
            """)
            cur.execute("SELECT count(*) FROM county_geom WHERE tract_count IS NOT NULL")
            n = cur.fetchone()[0]
        conn.commit()
    log.info("county scores refreshed: %d counties have tract counts", n)
    return n


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true", help="scores only, skip download")
    args = ap.parse_args()

    n = 0
    if not args.refresh:
        n = load_geometry()
        if not n:
            return 1
    refresh_scores()
    log_ingest("county_geom", "ok", n, 1, "refresh" if args.refresh else "full")
    return 0


if __name__ == "__main__":
    sys.exit(main())
