#!/usr/bin/env python3
"""
Ingest ACS 5-year estimates for every census tract in the US, across multiple vintages.

Request math — this is why we don't get rate limited:
    51 states+DC  x  7 vintages  =  357 requests TOTAL for ~85,000 tracts x 7 years.
    The naive per-tract approach the source paper implies would be ~595,000 requests.

Every response is cached to disk, so re-running costs zero requests.

Usage:
    python3 ingest_acs.py                # all vintages
    python3 ingest_acs.py --vintage 2023 # one vintage
    python3 ingest_acs.py --dry-run      # show plan, fetch nothing
"""
from __future__ import annotations

import argparse
import logging
import sys

from config import ACS_BASE, ACS_NULL_SENTINELS, ACS_VARS, ACS_VINTAGES, CENSUS_KEY, LOGS
from db import log_ingest, upsert
from http_cache import BudgetExceeded, budget_report, get_json

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout),
              logging.FileHandler(LOGS / "ingest_acs.log")],
)
log = logging.getLogger("ingest_acs")

# 50 states + DC. Territories excluded: FHFA/Zillow/BPS coverage is absent or
# non-comparable, so they'd score on partial data.
STATE_FIPS = [
    "01","02","04","05","06","08","09","10","11","12","13","15","16","17","18",
    "19","20","21","22","23","24","25","26","27","28","29","30","31","32","33",
    "34","35","36","37","38","39","40","41","42","44","45","46","47","48","49",
    "50","51","53","54","55","56",
]

VAR_CODES = list(ACS_VARS.keys())
DB_COLS = [ACS_VARS[v] for v in VAR_CODES]


def clean(raw: str | None, numeric: bool = True):
    """
    Census sends -666666666 and friends for suppressed/unavailable values.
    Casting those to int is how a model quietly starts scoring garbage.
    Returns None so the column is NULL, not 0.
    """
    if raw is None or raw == "":
        return None
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return None
    if int(val) in ACS_NULL_SENTINELS or val <= -666666:
        return None
    return val if not numeric else val


def fetch_state_vintage(state: str, vintage: int) -> list[tuple] | None:
    """One request -> every tract in one state for one ACS vintage."""
    params = {
        "get": "NAME," + ",".join(VAR_CODES),
        "for": "tract:*",
        "in": f"state:{state} county:*",
    }
    if CENSUS_KEY:
        params["key"] = CENSUS_KEY

    data = get_json(ACS_BASE.format(year=vintage), params, ttl_days=365)
    if not data or len(data) < 2:
        return None

    header = data[0]
    idx = {name: i for i, name in enumerate(header)}
    try:
        i_state, i_county, i_tract = idx["state"], idx["county"], idx["tract"]
        i_name = idx["NAME"]
    except KeyError:
        log.warning("unexpected header for state %s vintage %s: %s", state, vintage, header[:8])
        return None

    rows = []
    for rec in data[1:]:
        st, cty, tr = rec[i_state], rec[i_county], rec[i_tract]
        geoid = f"{st}{cty}{tr}"          # 11-digit. The paper compared bare 6-digit
        if len(geoid) != 11:              # tract codes against 11-digit GEOIDs and
            continue                      # could therefore never match an OZ.
        vals = []
        for code in VAR_CODES:
            v = clean(rec[idx[code]]) if code in idx else None
            vals.append(v)

        # cast the integer-typed columns
        out = []
        for col, v in zip(DB_COLS, vals):
            if v is None:
                out.append(None)
            elif col == "median_rent_pct_income":
                out.append(round(v, 2))
            else:
                out.append(int(v))

        rows.append((geoid, vintage, st, cty, rec[i_name], *out))
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--vintage", type=int, action="append",
                    help="specific vintage(s); default = all configured")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    vintages = args.vintage or ACS_VINTAGES
    planned = len(STATE_FIPS) * len(vintages)

    log.info("ACS ingest plan: %d states x %d vintages = %d requests max "
             "(cached responses cost 0)", len(STATE_FIPS), len(vintages), planned)

    if not CENSUS_KEY:
        # Verified 2026-08-08: the Census API no longer serves keyless requests —
        # it answers HTTP 200 with an HTML "Missing Key" page. Fail loudly here
        # rather than issue 357 requests that all silently return nothing.
        log.error(
            "NO CENSUS API KEY — aborting before any request.\n"
            "  The Census API rejects keyless requests (HTTP 200 + HTML, not an error).\n"
            "  Fix: get a free key (instant) at\n"
            "       https://api.census.gov/data/key_signup.html\n"
            "  then: echo '{\"census\":\"YOUR_KEY\"}' > /opt/realestate/.keys.json\n"
            "        chmod 600 /opt/realestate/.keys.json"
        )
        log_ingest("acs_tract", "no_key", 0, 0, "Census API key absent")
        return 3

    log.info("Census API key: present")
    if args.dry_run:
        log.info("dry run — exiting before any network call")
        return 0

    cols = ["geoid", "vintage", "state_fips", "county_fips", "name"] + DB_COLS
    total_rows = 0
    requests_made = 0
    failures: list[str] = []

    for vintage in vintages:
        v_rows = 0
        for state in STATE_FIPS:
            try:
                rows = fetch_state_vintage(state, vintage)
            except BudgetExceeded as e:
                log.error("STOPPING CLEANLY: %s", e)
                log_ingest("acs_tract", "budget_stop", total_rows, requests_made, str(e))
                log.info("budget: %s", budget_report())
                return 2
            requests_made += 1
            if not rows:
                failures.append(f"{state}/{vintage}")
                continue
            upsert("acs_tract", cols, rows, ["geoid", "vintage"])
            v_rows += len(rows)
        total_rows += v_rows
        log.info("vintage %s complete: %d tract-rows", vintage, v_rows)

    log.info("ACS ingest done: %d rows, %d requests issued", total_rows, requests_made)
    if failures:
        log.warning("%d state/vintage combos returned nothing: %s",
                    len(failures), ", ".join(failures[:20]))
    log.info("budget usage: %s", budget_report())
    log_ingest("acs_tract", "ok" if not failures else "partial",
               total_rows, requests_made, f"failures={failures[:50]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
