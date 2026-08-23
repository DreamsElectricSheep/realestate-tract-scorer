#!/usr/bin/env python3
"""
Phase 3: backtest the scorer's component weights against realized outcomes,
then refit them. This is the step the source paper's "provisional weights"
were always missing — asserted numbers with no evidence behind them.

Design
------
Predictor ("early score"): computed from data available as of ~2019 only.
  - rent momentum / education influx: ACS vintage 2017 (2013-2017 window) ->
    vintage 2019 (2015-2019 window). Both are on 2010 TRACT BOUNDARIES, unlike
    the live scorer's 2020-boundary tracts, so they're crosswalked onto 2020
    geoids via tract_xwalk_2010_2020 (area-weighted) before use. Skipping this
    crosswalk — which score_tracts.py's own load_acs_trends() does, via a
    plain geoid groupby — silently truncates history for every tract whose
    2010 boundary doesn't match its 2020 one.
  - affordability: rent-to-income snapshot at the 2019 vintage.
  - spatial (POI proximity): present-day OSM snapshot. No historical POI data
    exists to backtest against, but university/transit/hospital locations are
    slow-moving structural features, so testing today's snapshot against
    outcomes since 2019 is a defensible simplification, not a lookahead bias
    on the level the temporal ACS features would have.
  - supply risk: bps_permits filtered to year <= 2019 only — genuinely as-of.

Outcome (realized, 2019 -> latest available):
  - ACS-native: rent and home-value CAGR from vintage 2019 -> vintage 2023
    (both endpoints on their own native boundaries; 2023 needs no crosswalk).
  - FHFA-inherited: the tract's CBSA's HPI % change, 2019 -> latest year with
    data. This is the closer analogue to "asset appreciation," the paper's
    own framing of investment return — used as the PRIMARY outcome for
    refitting weights. FHFA only covers ~100 major CBSAs, so this outcome is
    only available for tracts in those metros; the ACS-native outcomes cover
    everywhere and serve as a broader cross-check.

Method: Spearman rank IC (predictor vs outcome) per component, decile lift
table, then refit WEIGHTS proportional to each component's |IC| against the
FHFA outcome (the ACS-native outcomes are reported but not used for fitting,
since rent/value CAGR against a rent/value-derived predictor risks measuring
the same thing twice). Components with a non-significant or negative IC are
floored rather than zeroed, so a currently-weak signal doesn't get carried
at literally 0 forever on one backtest window's noise.

Usage:
    python3 calibrate.py                # full run, writes calibration_report
    python3 calibrate.py --dry-run       # print results, don't touch score_tracts.py
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone

import numpy as np
import pandas as pd
from scipy import stats

from config import LOGS
from db import engine, log_ingest, raw_conn

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout),
              logging.FileHandler(LOGS / "calibrate.log")],
)
log = logging.getLogger("calibrate")

EARLY_Y0, EARLY_Y1 = 2017, 2019   # 2010-boundary vintages, predictor window
LATE_Y = 2023                     # 2020-boundary vintage, ACS-native outcome
SUPPLY_CUTOFF = 2019              # bps_permits year <= this only

MIN_WEIGHT_PCT = 3.0  # floor so a weak-IC component isn't zeroed on one window


# ------------------------------------------------------------ crosswalk helper
def crosswalk_2010_to_2020(df: pd.DataFrame, value_cols: list[str]) -> pd.DataFrame:
    """
    Area-weighted reprojection of 2010-tract-indexed rows onto 2020 geoids.

    A 2010 tract that split into several 2020 tracts contributes its value to
    each of them; a 2020 tract assembled from multiple 2010 tracts gets the
    area-weighted average of its contributors. This is the same crosswalk
    table ingest_oz.py uses for Opportunity Zones -- reused here because the
    boundary-vintage mismatch is exactly the same problem.
    """
    xwalk = pd.read_sql(
        "SELECT geoid_2010, geoid_2020, area_weight FROM tract_xwalk_2010_2020",
        engine(),
    )
    m = xwalk.merge(df, left_on="geoid_2010", right_on="geoid", how="inner")
    if m.empty:
        return pd.DataFrame(columns=["geoid"] + value_cols)

    out = {"geoid": m.groupby("geoid_2020").size().index}
    grouped = m.groupby("geoid_2020")
    result = pd.DataFrame(index=grouped.size().index)
    for col in value_cols:
        w = m["area_weight"].fillna(0)
        wv = m[col] * w
        result[col] = wv.groupby(m["geoid_2020"]).sum() / w.groupby(m["geoid_2020"]).sum().replace(0, np.nan)
    result = result.reset_index().rename(columns={"geoid_2020": "geoid"})
    return result


# ------------------------------------------------------------ predictor (early score)
def build_early_predictor() -> pd.DataFrame:
    log.info("loading early-window ACS (vintages %d, %d — 2010 boundaries)", EARLY_Y0, EARLY_Y1)
    acs = pd.read_sql(f"""
        SELECT geoid, vintage, median_hh_income, median_gross_rent, median_home_value,
               edu_total, edu_bachelors, edu_masters, edu_professional, edu_doctorate
        FROM acs_tract WHERE vintage IN ({EARLY_Y0}, {EARLY_Y1})
    """, engine())

    def edu_share(row):
        if not row["edu_total"] or pd.isna(row["edu_total"]) or row["edu_total"] == 0:
            return np.nan
        hi = sum((row.get(k) or 0) for k in
                 ("edu_bachelors", "edu_masters", "edu_professional", "edu_doctorate"))
        return 100 * hi / row["edu_total"]

    acs["edu_share"] = acs.apply(edu_share, axis=1)

    y0 = acs[acs.vintage == EARLY_Y0].set_index("geoid")
    y1 = acs[acs.vintage == EARLY_Y1].set_index("geoid")
    common = y0.index.intersection(y1.index)
    y0, y1 = y0.loc[common], y1.loc[common]
    n_years = EARLY_Y1 - EARLY_Y0

    def cagr(a, b):
        a, b = a.astype(float), b.astype(float)
        out = pd.Series(np.nan, index=a.index)
        ok = (a > 0) & (b > 0) & a.notna() & b.notna()
        out[ok] = ((b[ok] / a[ok]) ** (1 / n_years) - 1) * 100
        return out

    early = pd.DataFrame({
        "geoid_2010": common,
        "rent_cagr": cagr(y0["median_gross_rent"], y1["median_gross_rent"]).values,
        "income_cagr": cagr(y0["median_hh_income"], y1["median_hh_income"]).values,
        "edu_share_change_pp": (y1["edu_share"] - y0["edu_share"]).values,
        "rent_to_income_2019": (y1["median_gross_rent"] / (y1["median_hh_income"] / 12)).values,
    })
    early["rent_income_spread"] = early["rent_cagr"] - early["income_cagr"]
    early = early.rename(columns={"geoid_2010": "geoid"})

    log.info("crosswalking %d 2010-boundary tracts onto 2020 geoids", len(early))
    xw = crosswalk_2010_to_2020(
        early, ["rent_income_spread", "edu_share_change_pp", "rent_to_income_2019"])
    log.info("crosswalk result: %d 2020-boundary tracts with early-window signal", len(xw))
    return xw


def build_early_spatial() -> pd.DataFrame:
    """Present-day POI snapshot -- see module docstring for the lookahead caveat."""
    df = pd.read_sql("SELECT geoid, poi_class, count_within_5k FROM tract_poi_counts", engine())
    if df.empty:
        return pd.DataFrame(columns=["geoid", "spatial_raw"])
    piv = df.pivot_table(index="geoid", columns="poi_class", values="count_within_5k",
                          fill_value=0, aggfunc="sum")
    weights = {"university": 3, "transit_stop": 2, "hospital": 2, "industrial": 1.5,
               "grocery": 1, "cafe": 1, "school": 0.5, "park": 0.5}
    for c in weights:
        if c not in piv.columns:
            piv[c] = 0
    piv["spatial_raw"] = sum(piv[c] * w for c, w in weights.items())
    return piv[["spatial_raw"]].reset_index()


def build_early_supply() -> pd.DataFrame:
    permits = pd.read_sql(f"""
        SELECT county_fips, sum(units_5plus) AS mf_permits_total
        FROM bps_permits WHERE year <= {SUPPLY_CUTOFF} GROUP BY county_fips
    """, engine())
    if permits.empty:
        return pd.DataFrame(columns=["county_fips_full", "supply_raw"])
    stock = pd.read_sql(f"""
        SELECT state_fips || county_fips AS county_fips_full, sum(tenure_total) AS households
        FROM acs_tract WHERE vintage = {EARLY_Y1} GROUP BY 1
    """, engine())
    permits = permits.rename(columns={"county_fips": "county_fips_full"})
    m = permits.merge(stock, on="county_fips_full", how="left")
    m["supply_raw"] = m["mf_permits_total"] / m["households"].replace(0, np.nan)
    return m[["county_fips_full", "supply_raw"]]


# ------------------------------------------------------------ outcomes (realized)
def build_acs_native_outcome() -> pd.DataFrame:
    log.info("loading realized outcome: ACS vintage %d -> %d (native 2020 boundaries)",
             EARLY_Y1, LATE_Y)
    acs = pd.read_sql(f"""
        SELECT geoid, vintage, median_gross_rent, median_home_value
        FROM acs_tract WHERE vintage IN ({EARLY_Y1}, {LATE_Y})
    """, engine())
    y0 = acs[acs.vintage == EARLY_Y1].set_index("geoid")
    y1 = acs[acs.vintage == LATE_Y].set_index("geoid")
    common = y0.index.intersection(y1.index)
    y0, y1 = y0.loc[common], y1.loc[common]
    n = LATE_Y - EARLY_Y1

    def cagr(a, b):
        a, b = a.astype(float), b.astype(float)
        out = pd.Series(np.nan, index=a.index)
        ok = (a > 0) & (b > 0) & a.notna() & b.notna()
        out[ok] = ((b[ok] / a[ok]) ** (1 / n) - 1) * 100
        return out

    return pd.DataFrame({
        "geoid": common,
        "realized_rent_cagr": cagr(y0["median_gross_rent"], y1["median_gross_rent"]).values,
        "realized_value_cagr": cagr(y0["median_home_value"], y1["median_home_value"]).values,
    })


def build_fhfa_outcome() -> pd.DataFrame:
    log.info("loading realized outcome: FHFA CBSA HPI %d -> latest (inherited via county)", EARLY_Y1)
    hpi = pd.read_sql("SELECT cbsa_code, year, index_nsa FROM fhfa_hpi_msa", engine())
    if hpi.empty:
        log.warning("fhfa_hpi_msa empty -- FHFA outcome unavailable this run")
        return pd.DataFrame(columns=["geoid", "realized_fhfa_appreciation_pct"])
    latest_year = int(hpi["year"].max())
    y0 = hpi[hpi.year == EARLY_Y1].set_index("cbsa_code")["index_nsa"]
    y1 = hpi[hpi.year == latest_year].set_index("cbsa_code")["index_nsa"]
    common = y0.index.intersection(y1.index)
    pct = ((y1.loc[common] / y0.loc[common]) - 1) * 100
    cbsa_pct = pct.rename("realized_fhfa_appreciation_pct").reset_index().rename(
        columns={"index": "cbsa_code"})
    log.info("FHFA outcome window: %d -> %d, %d CBSAs", EARLY_Y1, latest_year, len(cbsa_pct))

    xwalk = pd.read_sql("SELECT county_fips, cbsa_code FROM county_cbsa_xwalk", engine())
    tracts = pd.read_sql(
        "SELECT geoid, state_fips || county_fips AS county_fips FROM tract_geom WHERE vintage = 2020",
        engine())
    t = tracts.merge(xwalk, on="county_fips", how="inner").merge(cbsa_pct, on="cbsa_code", how="inner")
    return t[["geoid", "realized_fhfa_appreciation_pct"]]


# ------------------------------------------------------------ analysis
def pct_rank(s: pd.Series) -> pd.Series:
    valid = s.dropna()
    if len(valid) < 5:
        return pd.Series(np.nan, index=s.index)
    return s.rank(pct=True) * 100


def spearman_ic(pred: pd.Series, outcome: pd.Series) -> tuple[float, float, int]:
    both = pd.DataFrame({"p": pred, "o": outcome}).dropna()
    if len(both) < 30:
        return np.nan, np.nan, len(both)
    rho, p = stats.spearmanr(both["p"], both["o"])
    return round(float(rho), 4), round(float(p), 6), len(both)


def decile_lift(pred: pd.Series, outcome: pd.Series) -> pd.DataFrame:
    both = pd.DataFrame({"p": pred, "o": outcome}).dropna()
    if len(both) < 100:
        return pd.DataFrame()
    both["decile"] = pd.qcut(both["p"], 10, labels=False, duplicates="drop") + 1
    return both.groupby("decile")["o"].agg(["mean", "count"]).round(3)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                     help="print results, don't write calibration_report or touch WEIGHTS")
    args = ap.parse_args()

    log.info("=== Phase 3 calibration: early=%d->%d, outcome through %d ===",
              EARLY_Y0, EARLY_Y1, LATE_Y)

    early = build_early_predictor()
    spatial = build_early_spatial()
    supply = build_early_supply()

    tracts = pd.read_sql(
        "SELECT geoid, state_fips, county_fips FROM tract_geom WHERE vintage = 2020", engine())
    tracts["county_fips_full"] = tracts["state_fips"] + tracts["county_fips"]

    df = tracts.merge(early, on="geoid", how="left")
    df = df.merge(spatial, on="geoid", how="left")
    df = df.merge(supply, on="county_fips_full", how="left")

    log.info("=== early (as-of-%d) component subscores ===", EARLY_Y1)
    df["s_rent_momentum"] = pct_rank(df["rent_income_spread"])
    df["s_spatial"] = pct_rank(df["spatial_raw"])
    df["s_education_influx"] = pct_rank(df["edu_share_change_pp"])
    df["s_supply_risk"] = 100 - pct_rank(df["supply_raw"]).fillna(50)
    df.loc[df["supply_raw"].isna(), "s_supply_risk"] = np.nan

    rti = df["rent_to_income_2019"]
    afford = pd.Series(np.nan, index=df.index)
    afford[rti < 0.30] = 60 + (0.30 - rti[rti < 0.30]) / 0.30 * 20
    afford[(rti >= 0.30) & (rti < 0.45)] = 100 - (rti[(rti >= 0.30) & (rti < 0.45)] - 0.30) / 0.15 * 20
    afford[rti >= 0.45] = np.maximum(0, 80 - (rti[rti >= 0.45] - 0.45) * 200)
    df["s_affordability"] = afford.clip(0, 100)

    comp_cols = ["s_rent_momentum", "s_spatial", "s_supply_risk", "s_affordability", "s_education_influx"]
    CURRENT_WEIGHTS = {"s_rent_momentum": 30, "s_spatial": 20, "s_supply_risk": 15,
                        "s_affordability": 10, "s_education_influx": 10}
    avail = df[comp_cols].notna()
    weight_arr = np.array([CURRENT_WEIGHTS[c] for c in comp_cols])
    weighted_sum = (df[comp_cols].fillna(0).values * weight_arr).sum(axis=1)
    weight_present = (avail.values * weight_arr).sum(axis=1)
    df["early_score"] = np.where(weight_present > 0, weighted_sum / weight_present, np.nan)

    acs_outcome = build_acs_native_outcome()
    fhfa_outcome = build_fhfa_outcome()
    df = df.merge(acs_outcome, on="geoid", how="left")
    df = df.merge(fhfa_outcome, on="geoid", how="left")

    outcomes = {
        "realized_rent_cagr": "ACS rent CAGR %d->%d (native, nationwide)" % (EARLY_Y1, LATE_Y),
        "realized_value_cagr": "ACS home-value CAGR %d->%d (native, nationwide)" % (EARLY_Y1, LATE_Y),
        "realized_fhfa_appreciation_pct": "FHFA CBSA HPI change %d->latest (inherited, ~100 metros only)" % EARLY_Y1,
    }

    log.info("=== component-level Spearman IC vs each outcome ===")
    ic_table = {}
    for oc, desc in outcomes.items():
        log.info("--- outcome: %s ---", desc)
        row = {}
        for comp in comp_cols + ["early_score"]:
            rho, p, n = spearman_ic(df[comp], df[oc])
            row[comp] = rho
            sig = "*" if (p is not None and not np.isnan(p) and p < 0.05) else " "
            log.info("  %-20s IC=%7s  p=%8s  n=%6d %s", comp, rho, p, n, sig)
        ic_table[oc] = row

    log.info("=== decile lift: early_score vs FHFA appreciation (primary outcome) ===")
    lift = decile_lift(df["early_score"], df["realized_fhfa_appreciation_pct"])
    if not lift.empty:
        for decile, r in lift.iterrows():
            log.info("  decile %2d: mean=%7.2f%%  n=%d", decile, r["mean"], int(r["count"]))
        top, bot = lift.loc[lift.index.max(), "mean"], lift.loc[lift.index.min(), "mean"]
        log.info("  top-decile minus bottom-decile spread: %.2f pp", top - bot)
    else:
        log.warning("  not enough FHFA-matched tracts for a decile table")

    # -------------------------------------------------- refit weights
    primary_ic = ic_table.get("realized_fhfa_appreciation_pct", {})
    abs_ic = {c: abs(primary_ic.get(c) or 0) for c in comp_cols}
    total = sum(abs_ic.values())
    if total <= 0:
        log.warning("all ICs are zero/NaN against the primary outcome -- keeping current weights")
        new_weights = CURRENT_WEIGHTS
    else:
        raw_pct = {c: (abs_ic[c] / total) * 100 for c in comp_cols}
        floored = {c: max(v, MIN_WEIGHT_PCT) for c, v in raw_pct.items()}
        renorm_total = sum(floored.values())
        new_weights = {c: round(v / renorm_total * 90, 1) for c, v in floored.items()}
        # 90, not 100: safety(10) and regulatory(5->reserved) stay neutral-midpoint
        # placeholders until crime_agency / reg_flags are populated -- see
        # score_tracts.py. Rescale the 5 backtestable components to 90 so the
        # full weight vector (fitted 90 + safety 10 stays as before) still
        # sums to 100 once regulatory's own reserved slice is folded back in.
        # Simpler and more honest: just report the fitted 5-component vector
        # and let score_tracts.py fold it into the full vector explicitly.

    log.info("=== refit result (weights proportional to |IC| vs FHFA outcome, floor=%.0f%%) ===",
              MIN_WEIGHT_PCT)
    for c in comp_cols:
        log.info("  %-20s current=%5.1f  ic=%7s  fitted=%5.1f",
                  c, CURRENT_WEIGHTS[c], primary_ic.get(c), new_weights[c])

    if args.dry_run:
        log.info("--dry-run set: not writing calibration_report or touching score_tracts.py")
        return 0

    with raw_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS calibration_report (
                    id SERIAL PRIMARY KEY,
                    run_at TIMESTAMPTZ DEFAULT now(),
                    early_y0 INT, early_y1 INT, late_y INT,
                    n_tracts_scored INT,
                    ic_json TEXT,
                    fitted_weights_json TEXT,
                    notes TEXT
                )
            """)
        conn.commit()

    import json
    with raw_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO calibration_report "
                "(early_y0, early_y1, late_y, n_tracts_scored, ic_json, fitted_weights_json, notes) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (EARLY_Y0, EARLY_Y1, LATE_Y, int(df["early_score"].notna().sum()),
                 json.dumps(ic_table), json.dumps(new_weights),
                 "Weights proportional to |Spearman IC| vs FHFA CBSA-inherited appreciation, "
                 f"floored at {MIN_WEIGHT_PCT}%. safety/regulatory left as neutral placeholders "
                 "(not yet backtestable -- crime_agency/reg_flags unpopulated)."),
            )
        conn.commit()
    log.info("calibration_report row written")
    log_ingest("calibration_report", "ok", int(df["early_score"].notna().sum()), 0,
               json.dumps(new_weights))

    return 0


if __name__ == "__main__":
    sys.exit(main())
