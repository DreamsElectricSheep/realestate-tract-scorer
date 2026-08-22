#!/usr/bin/env python3
"""
Ingest FHFA House Price Index — MSA level — as the Phase 3 calibration ground truth.

Note on granularity: FHFA's public HPI product does not publish a tract- or
county-level series (verified 2026-08-09 — every county/tract-named filename
pattern 404s; the master file's finest "level" column value is MSA). MSA is
what's actually available, so that's the ground truth this project uses.
A tract inherits its MSA's HPI trajectory via the county->CBSA crosswalk below;
that's coarser than a true tract-level truth series would be, but it's real
data rather than an invented one.

No API key. Two static files.
"""
from __future__ import annotations

import logging
import sys

import pandas as pd

from config import LOGS, RAW
from db import log_ingest, raw_conn, upsert
from http_cache import download_file

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout),
              logging.FileHandler(LOGS / "ingest_fhfa.log")],
)
log = logging.getLogger("ingest_fhfa")

HPI_MASTER_URL = "https://www.fhfa.gov/hpi/download/monthly/hpi_master.csv"
CBSA_DELIN_URL = ("https://www2.census.gov/programs-surveys/metro-micro/geographies/"
                   "reference-files/2020/delineation-files/list1_2020.xls")

DDL = """
CREATE TABLE IF NOT EXISTS fhfa_hpi_msa (
    cbsa_code   TEXT NOT NULL,
    place_name  TEXT,
    year        INT  NOT NULL,
    index_nsa   NUMERIC(12,4),
    annual_pct  NUMERIC(8,4),
    PRIMARY KEY (cbsa_code, year)
);
CREATE TABLE IF NOT EXISTS county_cbsa_xwalk (
    county_fips TEXT PRIMARY KEY,
    cbsa_code   TEXT,
    cbsa_title  TEXT,
    metro_micro TEXT
);
"""


def load_hpi() -> int:
    dest = RAW / "fhfa" / "hpi_master.csv"
    path = download_file(HPI_MASTER_URL, dest, min_bytes=1_000_000)
    if not path:
        return 0

    df = pd.read_csv(path)
    m = df[
        (df["level"] == "MSA")
        & (df["hpi_type"] == "traditional")
        & (df["frequency"] == "quarterly")
        & (df["hpi_flavor"] == "purchase-only")
    ].copy()
    if m.empty:
        log.error("no MSA/traditional/quarterly/purchase-only rows found — "
                   "FHFA file structure may have changed, check columns: %s",
                   list(df.columns))
        return 0

    # Q4 of each year = year-end snapshot, one row per CBSA per year.
    yearly = (m[m["period"] == 4]
              .groupby(["place_id", "place_name", "yr"], as_index=False)["index_nsa"]
              .mean())
    yearly = yearly.sort_values(["place_id", "yr"])
    yearly["annual_pct"] = yearly.groupby("place_id")["index_nsa"].pct_change() * 100

    rows = [
        (str(r.place_id), r.place_name, int(r.yr), float(r.index_nsa),
         None if pd.isna(r.annual_pct) else round(float(r.annual_pct), 4))
        for r in yearly.itertuples()
    ]
    upsert("fhfa_hpi_msa", ["cbsa_code", "place_name", "year", "index_nsa", "annual_pct"],
           rows, ["cbsa_code", "year"])
    log.info("FHFA MSA HPI: %d cbsa-year rows, %d distinct CBSAs",
             len(rows), yearly["place_id"].nunique())
    return len(rows)


def load_xwalk() -> int:
    dest = RAW / "fhfa" / "cbsa_delineation_2020.xls"
    path = download_file(CBSA_DELIN_URL, dest, min_bytes=50_000)
    if not path:
        return 0

    # Census delineation files carry a title block before the real header row.
    raw = pd.read_excel(path, header=None)
    header_row = None
    for i in range(min(10, len(raw))):
        if raw.iloc[i].astype(str).str.contains("CBSA Code", case=False, na=False).any():
            header_row = i
            break
    if header_row is None:
        log.error("could not locate header row in CBSA delineation file")
        return 0

    df = pd.read_excel(path, header=header_row)
    df.columns = [str(c).strip() for c in df.columns]
    need = {"CBSA Code", "CBSA Title", "Metropolitan/Micropolitan Statistical Area",
            "FIPS State Code", "FIPS County Code"}
    if not need.issubset(df.columns):
        log.error("expected columns missing, got: %s", list(df.columns))
        return 0

    df = df.dropna(subset=["FIPS State Code", "FIPS County Code"])
    rows = []
    # iterrows(), not itertuples(): several column names contain spaces/slashes,
    # which are not valid Python identifiers — itertuples() silently falls back
    # to positional field names (_1, _2, ...) for EVERY column when that happens,
    # not just the offending one, breaking any by-name access.
    for _, r in df.iterrows():
        st = str(r["FIPS State Code"]).split(".")[0].zfill(2)
        cty = str(r["FIPS County Code"]).split(".")[0].zfill(3)
        rows.append((
            f"{st}{cty}",
            str(r["CBSA Code"]).split(".")[0],
            r["CBSA Title"],
            r["Metropolitan/Micropolitan Statistical Area"],
        ))

    with raw_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(DDL)
        conn.commit()
    upsert("county_cbsa_xwalk", ["county_fips", "cbsa_code", "cbsa_title", "metro_micro"],
           rows, ["county_fips"])
    log.info("county->CBSA crosswalk: %d counties", len(rows))
    return len(rows)


def main() -> int:
    with raw_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(DDL)
        conn.commit()

    n1 = load_xwalk()
    n2 = load_hpi()
    ok = n1 > 0 and n2 > 0
    log_ingest("fhfa_hpi_msa", "ok" if ok else "partial", n2, 2, f"xwalk_counties={n1}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
