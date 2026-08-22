#!/usr/bin/env python3
"""
Ingest Census Building Permits Survey (BPS) — county-level, annual.

This is the single biggest omission in the source paper: a tract can have
strong rent growth AND a supply wave big enough to crush it (Austin/Nashville/
Phoenix 2023-2025). Column 16 ("5+ units, Bldgs") is multifamily permits
specifically, which is what actually matters for a multifamily thesis.

No API key. Static annual CSVs, one small file per year.
"""
from __future__ import annotations

import csv
import io
import logging
import sys

from config import LOGS
from db import log_ingest, upsert
from http_cache import get_json

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout),
              logging.FileHandler(LOGS / "ingest_bps.log")],
)
log = logging.getLogger("ingest_bps")

BPS_URL = "https://www2.census.gov/econ/bps/County/co{year}a.txt"
YEARS = list(range(2015, 2024))  # aligned with ACS_VINTAGES span


def fetch_year(year: int) -> list[tuple] | None:
    # get_json expects JSON; BPS is CSV, so fetch raw via the same cache/throttle
    # machinery by calling requests directly but routed through get_json's cache
    # would require JSON. Simpler: reuse requests with the same politeness here.
    import requests
    from config import REQUEST_TIMEOUT, USER_AGENT
    from http_cache import _cache_path  # reuse cache key/path logic

    url = BPS_URL.format(year=year)
    cp = _cache_path(url, None)
    if cp.exists():
        text = cp.read_text(encoding="utf-8", errors="replace")
    else:
        r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=REQUEST_TIMEOUT)
        if r.status_code != 200:
            log.warning("year %d: HTTP %d", year, r.status_code)
            return None
        text = r.text
        cp.write_text(text, encoding="utf-8")

    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    if len(rows) < 4:
        return None

    out = []
    for r in rows[3:]:  # header spans first 2 rows, then a blank row
        if len(r) < 17 or not r[1].strip().isdigit():
            continue
        state, county = r[1].strip().zfill(2), r[2].strip().zfill(3)
        try:
            units_total = int(r[7] or 0) + int(r[10] or 0) + int(r[13] or 0) + int(r[16] or 0)
            units_5plus = int(r[16] or 0)  # "5+ units" bucket = multifamily
        except ValueError:
            continue
        out.append((f"{state}{county}", year, units_total, units_5plus))
    return out


def main() -> int:
    total = 0
    failed = []
    for year in YEARS:
        rows = fetch_year(year)
        if not rows:
            failed.append(year)
            continue
        upsert("bps_permits", ["county_fips", "year", "units_total", "units_5plus"],
               rows, ["county_fips", "year"])
        total += len(rows)
        log.info("year %d: %d counties", year, len(rows))

    log.info("BPS ingest done: %d rows, %d years", total, len(YEARS) - len(failed))
    if failed:
        log.warning("years failed: %s", failed)
    log_ingest("bps_permits", "ok" if not failed else "partial", total, len(YEARS),
               f"failed={failed}")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
