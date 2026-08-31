#!/usr/bin/env python3
"""
Bridge Connecticut's 2022 county-to-Planning-Region relabeling into the
existing 2010->2020 tract crosswalk.

THE PROBLEM
-----------
Connecticut replaced its 8 counties with 9 Planning Regions as the county
equivalent for statistical geography, effective with TIGER/ACS vintage 2022.
This changed every CT tract's GEOID (the county-code digits, positions 3-5),
even though the underlying census tract itself did not move:

    old (county-coded, ACS 2017-2021, TIGER <=2021): 09001010101
    new (region-coded, ACS 2022-2023, TIGER 2022+):  09110010101

tract_geom holds only the NEW-coded 2020-boundary geometry (from TIGER 2023).
The official Census 2010->2020 tract relationship file -- already loaded in
tract_xwalk_2010_2020 -- predates the relabeling and uses OLD codes on both
sides, so it cannot bridge to tract_geom's NEW codes either. Net effect,
confirmed 2026-08-23: 884 of 884 Connecticut tracts (100%) had zero crosswalk
coverage, and Connecticut was the only affected state in the country.

THE FIX
-------
Connecticut's tract boundaries were essentially unchanged 2010->2020 in the
OLD coding scheme (confirmed: the national relationship file maps every CT
GEOID_TRACT_10 to an identical GEOID_TRACT_20). So for Connecticut, "2010
boundary" and "old-coded 2020 boundary" are the same geometry -- meaning a
single area-weighted crosswalk from OLD-coded geometry to NEW-coded geometry
serves as a complete substitute for the relabeling step, and can be inserted
directly into tract_xwalk_2010_2020 using its existing geoid_2010/geoid_2020
columns. Nothing downstream (acs_boundaries.py, score_tracts.py,
calibrate.py, the dashboard) needs to change; they all consume that table
generically.

Requires OLD-coded CT tract geometry, which tract_geom does not hold (it only
has the current/NEW-coded vintage). Run ingest_tiger.py first to pull one:
    python3 ingest_tiger.py --state 09 --tiger-year 2021 --vintage 2021
TIGER 2021 is the last vintage published before the relabeling (verified:
its Connecticut county codes are the old 001-015, not 110-190).

CAVEAT: weighted by raw geometry area (land + water), not AREALAND alone like
the official national file -- tract_geom does not carry a separate land-only
geometry, only the aland/awater area TALLIES. For Connecticut's coastal
tracts this slightly understates the land-area weight versus the Census
methodology. Better than no crosswalk; not bit-identical to how Census would
compute it.

Usage:
    python3 ingest_ct_relabel.py                 # build and load
    python3 ingest_ct_relabel.py --dry-run        # report only, write nothing
"""
from __future__ import annotations

import argparse
import logging
import sys

from config import LOGS
from db import log_ingest, raw_conn, upsert

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout),
              logging.FileHandler(LOGS / "ingest_ct_relabel.log")],
)
log = logging.getLogger("ingest_ct_relabel")

OLD_VINTAGE = 2021  # TIGER vintage with old (001-015) CT county codes
NEW_VINTAGE = 2020  # the vintage tract_geom's live geometry carries

# Below this share of the OLD tract's area, treat an intersection as a
# boundary-line sliver (shared edge/coastline rounding), not a real overlap.
MIN_WEIGHT = 0.01


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    with raw_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM tract_geom WHERE state_fips='09' AND vintage=%s",
                        (OLD_VINTAGE,))
            n_old = cur.fetchone()[0]
    if not n_old:
        log.error("no vintage=%d CT geometry loaded -- run: "
                   "python3 ingest_tiger.py --state 09 --tiger-year %d --vintage %d",
                   OLD_VINTAGE, OLD_VINTAGE, OLD_VINTAGE)
        return 1
    log.info("old-coded CT geometry present: %d tracts (vintage %d)", n_old, OLD_VINTAGE)

    sql = """
    SELECT o.geoid AS geoid_2010, n.geoid AS geoid_2020,
           ST_Area(ST_Intersection(o.geom, n.geom)::geography)
             / NULLIF(ST_Area(o.geom::geography), 0) AS weight
    FROM tract_geom o
    JOIN tract_geom n
      ON n.state_fips = '09' AND n.vintage = %s
     AND ST_Intersects(o.geom, n.geom)
    WHERE o.state_fips = '09' AND o.vintage = %s
    """
    with raw_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (NEW_VINTAGE, OLD_VINTAGE))
            rows = cur.fetchall()

    kept = [(g10, g20, round(float(w), 6)) for g10, g20, w in rows if w and w >= MIN_WEIGHT]
    dropped = len(rows) - len(kept)
    log.info("candidate pairs: %d, kept (weight >= %.0f%%): %d, dropped as slivers: %d",
             len(rows), MIN_WEIGHT * 100, len(kept), dropped)

    covered = len({g10 for g10, _, _ in kept})
    log.info("distinct old CT tracts with at least one real match: %d / %d", covered, n_old)

    if args.dry_run:
        log.info("--dry-run set: not writing")
        return 0

    n = upsert("tract_xwalk_2010_2020", ["geoid_2010", "geoid_2020", "area_weight"],
               kept, ["geoid_2010", "geoid_2020"])
    log.info("tract_xwalk_2010_2020: %d CT relabel rows upserted", n)
    log_ingest("tract_xwalk_2010_2020", "ok", n, 0, f"ct_relabel old={OLD_VINTAGE} new={NEW_VINTAGE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
