#!/usr/bin/env python3
"""
Ingest TIGER/Line census tract boundaries for all 50 states + DC.

No API key required — these are static files on www2.census.gov.
51 downloads total (~250 MB), cached on disk, never re-pulled.

TIGER ships EPSG:4269 (NAD83); we reproject to 4326 so everything downstream
(PostGIS distance work, Leaflet map) shares one CRS.

Usage:
    python3 ingest_tiger.py               # 2020-boundary tracts (TIGER 2023)
    python3 ingest_tiger.py --state 44    # single state
    python3 ingest_tiger.py --vintage 2010 --tiger-year 2019   # 2010 tracts, for OZ matching
"""
from __future__ import annotations

import argparse
import logging
import sys

import geopandas as gpd
from shapely.geometry import MultiPolygon
from sqlalchemy import text

from config import LOGS, RAW, TIGER_YEAR_2020
from db import engine, log_ingest, raw_conn
from http_cache import download_file
from ingest_acs import STATE_FIPS

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout),
              logging.FileHandler(LOGS / "ingest_tiger.log")],
)
log = logging.getLogger("ingest_tiger")

TIGER_URL = ("https://www2.census.gov/geo/tiger/TIGER{ty}/TRACT/"
             "tl_{ty}_{state}_tract.zip")


def as_multipolygon(geom):
    """Schema column is MultiPolygon; TIGER mixes Polygon and MultiPolygon."""
    if geom is None or geom.is_empty:
        return None
    if geom.geom_type == "Polygon":
        return MultiPolygon([geom])
    if geom.geom_type == "MultiPolygon":
        return geom
    return None


def load_state(state: str, tiger_year: int, boundary_vintage: int) -> int:
    url = TIGER_URL.format(ty=tiger_year, state=state)
    dest = RAW / "tiger" / f"tl_{tiger_year}_{state}_tract.zip"
    path = download_file(url, dest, min_bytes=10_000)
    if not path:
        log.warning("state %s: download failed", state)
        return 0

    try:
        gdf = gpd.read_file(f"zip://{path}")
    except Exception as e:
        log.error("state %s: could not read shapefile: %s", state, e)
        return 0

    gdf = gdf.to_crs("EPSG:4326")
    # Uppercase attribute columns only — renaming the active geometry column
    # detaches it from the GeoDataFrame.
    geom_name = gdf.geometry.name
    gdf.columns = [c if c == geom_name else c.upper() for c in gdf.columns]

    # TIGER column names are stable across these vintages, but be defensive.
    geoid_col = "GEOID" if "GEOID" in gdf.columns else "GEOID10"
    aland_col = "ALAND" if "ALAND" in gdf.columns else "ALAND10"
    awater_col = "AWATER" if "AWATER" in gdf.columns else "AWATER10"
    lat_col = "INTPTLAT" if "INTPTLAT" in gdf.columns else "INTPTLAT10"
    lon_col = "INTPTLON" if "INTPTLON" in gdf.columns else "INTPTLON10"
    name_col = "NAMELSAD" if "NAMELSAD" in gdf.columns else "NAMELSAD10"

    out = gpd.GeoDataFrame({
        "geoid": gdf[geoid_col].astype(str),
        "vintage": boundary_vintage,
        "state_fips": gdf[geoid_col].astype(str).str[:2],
        "county_fips": gdf[geoid_col].astype(str).str[2:5],
        "name": gdf[name_col].astype(str) if name_col in gdf.columns else None,
        "aland": gdf[aland_col].astype("int64") if aland_col in gdf.columns else None,
        "awater": gdf[awater_col].astype("int64") if awater_col in gdf.columns else None,
        "intptlat": gdf[lat_col].astype(str).str.replace("+", "", regex=False).astype(float)
                    if lat_col in gdf.columns else None,
        "intptlon": gdf[lon_col].astype(str).str.replace("+", "", regex=False).astype(float)
                    if lon_col in gdf.columns else None,
    }, geometry=gdf.geometry.apply(as_multipolygon), crs="EPSG:4326")

    out = out[out.geometry.notna()]
    out = out.rename_geometry("geom")

    # Delete-then-insert per state keeps the run idempotent without needing an
    # upsert path for geometry.
    with raw_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM tract_geom WHERE state_fips = %s AND vintage = %s",
                (state, boundary_vintage),
            )
        conn.commit()

    out.to_postgis("tract_geom", engine(), if_exists="append", index=False)
    return len(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", action="append", help="state FIPS; default = all")
    ap.add_argument("--tiger-year", type=int, default=TIGER_YEAR_2020)
    ap.add_argument("--vintage", type=int, default=2020,
                    help="boundary vintage label (2020 or 2010)")
    args = ap.parse_args()

    states = args.state or STATE_FIPS
    log.info("TIGER ingest: %d states, TIGER%d, boundary vintage %d",
             len(states), args.tiger_year, args.vintage)

    total = 0
    failed = []
    for i, st in enumerate(states, 1):
        n = load_state(st, args.tiger_year, args.vintage)
        total += n
        if n == 0:
            failed.append(st)
        log.info("[%2d/%d] state %s: %5d tracts (running total %d)",
                 i, len(states), st, n, total)

    with engine().connect() as c:
        db_total = c.execute(
            text("SELECT count(*) FROM tract_geom WHERE vintage = :v"),
            {"v": args.vintage},
        ).scalar()

    log.info("TIGER ingest complete: %d tracts loaded, %d in table", total, db_total)
    if failed:
        log.warning("failed states: %s", failed)
    log_ingest("tract_geom", "ok" if not failed else "partial", total, len(states),
               f"tiger{args.tiger_year} vintage{args.vintage} failed={failed}")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
