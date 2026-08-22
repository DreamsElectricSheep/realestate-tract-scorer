#!/usr/bin/env python3
"""
Ingest Zillow ZHVI (home values) and ZORI (rents) — ZIP-code level, monthly.

No API key. Static CSVs (~10-15 MB each), wide format (one column per month).
We melt to long format and downsample to December-of-year (annual), which is
plenty of resolution for tract-scoring cadence and keeps the table small.

ZIP is finer than tract for values but coarser for administrative boundaries —
useful as a cross-check on the ACS-derived rent/value trend, not a replacement.
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
              logging.FileHandler(LOGS / "ingest_zillow.log")],
)
log = logging.getLogger("ingest_zillow")

SERIES = {
    "zhvi": "https://files.zillowstatic.com/research/public_csvs/zhvi/"
            "Zip_zhvi_uc_sfrcondo_tier_0.33_0.67_sm_sa_month.csv",
    "zori": "https://files.zillowstatic.com/research/public_csvs/zori/"
            "Zip_zori_uc_sfrcondomfr_sm_sa_month.csv",
}


def load_metric(metric: str, url: str) -> int:
    dest = RAW / "zillow" / f"{metric}.csv"
    path = download_file(url, dest, min_bytes=100_000)
    if not path:
        log.error("%s: download failed", metric)
        return 0

    df = pd.read_csv(path, dtype={"RegionName": str})
    date_cols = [c for c in df.columns if c[:4].isdigit() and "-" in c]

    # December value = year-end snapshot. Keeps this aligned to annual ACS
    # cadence instead of carrying 300+ near-duplicate monthly rows per zip.
    dec_cols = [c for c in date_cols if c[5:7] == "12"] + \
               ([date_cols[-1]] if date_cols and date_cols[-1] not in
                [c for c in date_cols if c[5:7] == "12"] else [])

    rows = []
    for _, r in df.iterrows():
        zip5 = str(r["RegionName"]).zfill(5)
        for col in dec_cols:
            v = r.get(col)
            if pd.isna(v):
                continue
            rows.append((zip5, metric, "zip", zip5, col, float(v)))

    if not rows:
        return 0
    upsert("zillow_series", ["region_id", "metric", "geo_level", "zip", "obs_date", "value"],
           rows, ["region_id", "metric", "obs_date"])
    log.info("%s: %d zip-year observations from %d zips", metric, len(rows), df["RegionName"].nunique())
    return len(rows)


def main() -> int:
    total = 0
    failed = []
    for metric, url in SERIES.items():
        n = load_metric(metric, url)
        if not n:
            failed.append(metric)
        total += n
    log.info("Zillow ingest done: %d rows", total)
    log_ingest("zillow_series", "ok" if not failed else "partial", total, len(SERIES),
               f"failed={failed}")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
