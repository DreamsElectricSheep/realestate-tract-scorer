#!/usr/bin/env python3
"""
Ingest the Census 2010->2020 tract relationship file.

This is the piece the source paper's design is missing entirely: it compares
OZ designations (published on 2010 tract boundaries) against ACS/geocoder
results (2020 tract boundaries) with no crosswalk, so on a redrawn tract the
OZ match silently fails. This file makes that comparison correct.

Weight = share of the 2010 tract's land area that falls inside the 2020 tract
— used downstream to pick the dominant 2010 parent for a given 2020 tract
when a boundary redraw split it across more than one.

No API key. One national file, ~9 MB, one request.
"""
from __future__ import annotations

import logging
import sys

import pandas as pd

from config import LOGS, RAW
from db import log_ingest, upsert
from http_cache import download_file

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout),
              logging.FileHandler(LOGS / "ingest_xwalk.log")],
)
log = logging.getLogger("ingest_xwalk")

URL = ("https://www2.census.gov/geo/docs/maps-data/data/rel2020/tract/"
       "tab20_tract20_tract10_natl.txt")


def main() -> int:
    dest = RAW / "census" / "tab20_tract20_tract10_natl.txt"
    path = download_file(URL, dest, min_bytes=1_000_000)
    if not path:
        log.error("download failed")
        return 1

    df = pd.read_csv(path, sep="|", encoding="utf-8-sig", dtype=str)
    df["AREALAND_TRACT_10"] = pd.to_numeric(df["AREALAND_TRACT_10"], errors="coerce")
    df["AREALAND_PART"] = pd.to_numeric(df["AREALAND_PART"], errors="coerce")

    # Weight = what share of the 2010 tract's land ends up inside this 2020
    # tract. Water-only slivers (AREALAND_TRACT_10 == 0) get weight 0 rather
    # than a division error.
    denom = df["AREALAND_TRACT_10"].replace(0, pd.NA)
    df["weight"] = (df["AREALAND_PART"] / denom).fillna(0).clip(0, 1)

    rows = [
        (r.GEOID_TRACT_10, r.GEOID_TRACT_20, round(float(r.weight), 6))
        for r in df.itertuples()
        if len(str(r.GEOID_TRACT_10)) == 11 and len(str(r.GEOID_TRACT_20)) == 11
    ]
    upsert("tract_xwalk_2010_2020", ["geoid_2010", "geoid_2020", "area_weight"],
           rows, ["geoid_2010", "geoid_2020"])
    log.info("crosswalk loaded: %d relationship rows (%d distinct 2010 tracts, "
             "%d distinct 2020 tracts)",
             len(rows), df["GEOID_TRACT_10"].nunique(), df["GEOID_TRACT_20"].nunique())
    log_ingest("tract_xwalk_2010_2020", "ok", len(rows), 1, "")
    return 0


if __name__ == "__main__":
    sys.exit(main())
