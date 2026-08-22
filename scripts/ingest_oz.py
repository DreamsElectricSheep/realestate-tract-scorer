#!/usr/bin/env python3
"""
Ingest the Qualified Opportunity Zone designation list.

Deliberately FILE-based, not URL-based: the Treasury CDFI Fund does not publish
a single stable, permanent download link for the ~8,700-tract designated list
(it lives on cdfifund.gov, but the exact path has moved before and a guessed
URL here would either 404 silently or — worse — pull a stale mirror without
telling you). Point this at whatever file you have; it does the parsing,
2010-boundary validation, and DB load.

Get the file:
    https://www.cdfifund.gov/opportunity-zones  ("Designated QOZs" list,
    usually an .xlsx with a "2010 GEOID" or "Tract" column) — or any mirror
    you trust. Drop it anywhere and pass --file.

Usage:
    python3 ingest_oz.py --file /opt/realestate/data/raw/oz/designated_qoz.xlsx
    python3 ingest_oz.py --file oz.csv --geoid-col "2010 GEOID" --type-col "Tract Type"
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import pandas as pd

from config import LOGS
from db import log_ingest, upsert

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout),
              logging.FileHandler(LOGS / "ingest_oz.log")],
)
log = logging.getLogger("ingest_oz")

# Columns vary release to release; try these in order before giving up.
GEOID_CANDIDATES = ["2010 GEOID", "GEOID", "Census Tract Number", "Tract", "geoid", "GEOID10"]
TYPE_CANDIDATES = ["Tract Type", "Type", "tract_type"]


def find_col(df: pd.DataFrame, candidates: list[str]) -> str | None:
    lower = {c.lower().strip(): c for c in df.columns}
    for cand in candidates:
        if cand.lower() in lower:
            return lower[cand.lower()]
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", required=True, help="path to the OZ designation list (.csv/.xlsx)")
    ap.add_argument("--geoid-col", help="override auto-detected GEOID column")
    ap.add_argument("--type-col", help="override auto-detected tract-type column")
    args = ap.parse_args()

    path = Path(args.file)
    if not path.exists():
        log.error("file not found: %s", path)
        return 1

    df = pd.read_excel(path) if path.suffix.lower() in (".xlsx", ".xls") else pd.read_csv(path)
    log.info("loaded %d rows, columns: %s", len(df), list(df.columns))

    geoid_col = args.geoid_col or find_col(df, GEOID_CANDIDATES)
    type_col = args.type_col or find_col(df, TYPE_CANDIDATES)
    if not geoid_col:
        log.error("could not find a GEOID column. Columns present: %s\n"
                  "Pass --geoid-col explicitly.", list(df.columns))
        return 1

    df["_geoid"] = df[geoid_col].astype(str).str.replace(r"\D", "", regex=True).str.zfill(11)
    bad = df[df["_geoid"].str.len() != 11]
    if len(bad):
        log.warning("%d rows did not resolve to an 11-digit 2010 GEOID and will be skipped "
                    "(sample: %s)", len(bad), bad[geoid_col].head(3).tolist())
    df = df[df["_geoid"].str.len() == 11]

    rows = [
        (r["_geoid"], (r[type_col] if type_col else None), r["_geoid"][:2], r["_geoid"][2:5])
        for _, r in df.iterrows()
    ]
    upsert("opportunity_zones", ["geoid_2010", "tract_type", "state_fips", "county_fips"],
           rows, ["geoid_2010"])
    log.info("opportunity_zones loaded: %d tracts", len(rows))
    log_ingest("opportunity_zones", "ok", len(rows), 0, f"source={path.name}")

    n = pd_check = None
    from db import raw_conn
    with raw_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('public.tract_xwalk_2010_2020')")
            has_xwalk = cur.fetchone()[0] is not None
            if has_xwalk:
                cur.execute("SELECT count(*) FROM tract_xwalk_2010_2020")
                n = cur.fetchone()[0]
    if not n:
        log.warning(
            "tract_xwalk_2010_2020 is empty — OZ flags won't resolve onto current "
            "2020-boundary tracts until that crosswalk is loaded "
            "(Census relationship files: 2010->2020 tract relationship file, "
            "run build_xwalk.py once it exists)."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
