#!/usr/bin/env python3
"""
Compute the composite investment score for every tract with sufficient data.

Design principles that differ from the source paper:
  - Rent momentum scores the RENT-MINUS-INCOME GROWTH SPREAD (multi-vintage),
    not a single-year rent-to-income snapshot. The snapshot rewards places that
    are currently cheap; the spread rewards places where rent growth is
    outrunning the income growth of people already there — the actual
    gentrification-ascent signal the paper's own prose argues for.
  - Supply risk is a genuine PENALTY (BPS multifamily permits relative to
    existing stock), not absent. A market can have great rent growth and still
    be a bad buy because of what's under construction.
  - Weights below are PROVISIONAL — asserted, not fitted. Phase 3 backtests
    them against realized FHFA/Zillow appreciation and refits. Every run logs
    itself as provisional until that calibration has happened at least once.
  - A tract's score is only ever computed from the components actually
    available for it, reweighted proportionally — and `data_completeness`
    records what fraction of the full component set that was. Downstream
    consumers (dashboard, county rollup) filter on completeness so a
    data-starved tract cannot masquerade as a confidently cold one.

Usage:
    python3 score_tracts.py                 # all tracts, provisional weights
    python3 score_tracts.py --skip-moran     # faster iteration while testing
"""
from __future__ import annotations

import argparse
import logging
import sys
import time

import geopandas as gpd
import numpy as np
import pandas as pd

from config import LOGS
from db import engine, log_ingest, raw_conn

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout),
              logging.FileHandler(LOGS / "score_tracts.log")],
)
log = logging.getLogger("score_tracts")

# Provisional weights (sum to 100). Phase 3 replaces these with fitted values.
WEIGHTS = {
    "s_rent_momentum": 30,     # rent CAGR minus income CAGR spread
    "s_spatial": 20,           # POI proximity (universities/transit/industrial/etc)
    "s_supply_risk": 15,       # inverted: heavy multifamily permitting = penalty
    "s_affordability": 10,     # moderate burden = room to grow; extreme = risk
    "s_education_influx": 10,  # change in bachelor's-or-higher share
    "s_safety": 10,            # neutral (50) until crime_agency is populated
    "s_regulatory": 5,         # neutral (50) until reg_flags is populated
}
MIN_COMPLETENESS = 0.6  # below this, dashboard/rollups exclude the tract


def pct_rank(s: pd.Series) -> pd.Series:
    """0-100 percentile rank, NaN-safe, robust to small samples."""
    valid = s.dropna()
    if len(valid) < 5:
        return pd.Series(np.nan, index=s.index)
    ranks = s.rank(pct=True) * 100
    return ranks


def load_acs_trends() -> pd.DataFrame:
    df = pd.read_sql("""
        SELECT geoid, vintage, population, median_hh_income, median_gross_rent,
               median_home_value, edu_total, edu_bachelors, edu_masters,
               edu_professional, edu_doctorate
        FROM acs_tract ORDER BY geoid, vintage
    """, engine())
    if df.empty:
        log.warning("acs_tract is empty — rent/income/education subscores will "
                    "be null for every tract until the Census API key is added "
                    "and ingest_acs.py has run")
        return pd.DataFrame(columns=["geoid"])

    out = []
    for geoid, g in df.groupby("geoid"):
        g = g.sort_values("vintage")
        if len(g) < 2:
            continue
        y0, y1 = g.iloc[0], g.iloc[-1]
        n = int(y1.vintage - y0.vintage)
        if n <= 0:
            continue

        def cagr(a, b):
            if pd.isna(a) or pd.isna(b) or a <= 0 or b <= 0:
                return np.nan
            return ((b / a) ** (1 / n) - 1) * 100

        rent_cagr = cagr(y0.median_gross_rent, y1.median_gross_rent)
        inc_cagr = cagr(y0.median_hh_income, y1.median_hh_income)

        def edu_share(row):
            if not row.edu_total or pd.isna(row.edu_total) or row.edu_total == 0:
                return np.nan
            hi = sum((row.get(k) or 0) for k in
                     ("edu_bachelors", "edu_masters", "edu_professional", "edu_doctorate"))
            return 100 * hi / row.edu_total

        e0, e1 = edu_share(y0), edu_share(y1)
        rti = (y1.median_gross_rent / (y1.median_hh_income / 12)
               if y1.median_hh_income and y1.median_hh_income > 0 and y1.median_gross_rent
               else np.nan)

        out.append({
            "geoid": geoid,
            "rent_cagr_5y": rent_cagr,
            "income_cagr_5y": inc_cagr,
            "rent_income_spread": (rent_cagr - inc_cagr
                                   if not (pd.isna(rent_cagr) or pd.isna(inc_cagr)) else np.nan),
            "edu_share_change_pp": (e1 - e0 if not (pd.isna(e0) or pd.isna(e1)) else np.nan),
            "rent_to_income": rti,
        })
    return pd.DataFrame(out)


def load_spatial() -> pd.DataFrame:
    df = pd.read_sql("""
        SELECT geoid, poi_class, count_within_5k FROM tract_poi_counts
    """, engine())
    if df.empty:
        return pd.DataFrame(columns=["geoid"])
    piv = df.pivot_table(index="geoid", columns="poi_class", values="count_within_5k",
                          fill_value=0, aggfunc="sum")
    # Weighted raw index: university/transit/hospital carry more signal than
    # a park count. Percentile-ranked afterward, so raw units don't matter.
    weights = {"university": 3, "transit_stop": 2, "hospital": 2, "industrial": 1.5,
               "grocery": 1, "cafe": 1, "school": 0.5, "park": 0.5}
    for c in weights:
        if c not in piv.columns:
            piv[c] = 0
    piv["spatial_raw"] = sum(piv[c] * w for c, w in weights.items())
    return piv[["spatial_raw"]].reset_index()


def load_supply() -> pd.DataFrame:
    """
    County-level supply risk, joined onto every tract in that county.
    Multifamily permits (5+ units) over the last available years, normalized
    by county household count via ACS tenure_total as a stock proxy.
    """
    permits = pd.read_sql("""
        SELECT county_fips, sum(units_5plus) AS mf_permits_total,
               count(distinct year) AS years_covered
        FROM bps_permits GROUP BY county_fips
    """, engine())
    if permits.empty:
        return pd.DataFrame(columns=["county_fips"])

    stock = pd.read_sql("""
        SELECT state_fips || county_fips AS county_fips,
               sum(tenure_total) AS households
        FROM acs_tract WHERE vintage = (SELECT max(vintage) FROM acs_tract)
        GROUP BY 1
    """, engine())
    if stock.empty:
        # ACS not loaded yet — supply risk falls back to raw permit volume
        # percentile rather than a normalized rate. Still directionally useful.
        permits["supply_raw"] = permits["mf_permits_total"]
        return permits[["county_fips", "supply_raw"]]

    m = permits.merge(stock, on="county_fips", how="left")
    m["supply_raw"] = m["mf_permits_total"] / m["households"].replace(0, np.nan)
    return m[["county_fips", "supply_raw"]]


def compute_moran(scored: pd.DataFrame) -> pd.DataFrame:
    """Local Moran's I (LISA) on the composite score, Queen contiguity."""
    import libpysal
    from esda.moran import Moran_Local

    log.info("loading tract geometry for Moran's I (this is the slow step)...")
    t0 = time.time()
    geo = gpd.read_postgis(
        "SELECT geoid, geom FROM tract_geom WHERE vintage = 2020",
        engine(), geom_col="geom",
    )
    merged = geo.merge(scored[["geoid", "score"]], on="geoid", how="inner").dropna(subset=["score"])
    if len(merged) < 20:
        log.warning("too few scored tracts (%d) for a meaningful Moran's I — skipping", len(merged))
        return pd.DataFrame(columns=["geoid", "moran_cluster", "moran_p"])

    merged = merged.reset_index(drop=True)
    w = libpysal.weights.Queen.from_dataframe(merged, use_index=False, silence_warnings=True)
    w.transform = "r"
    log.info("contiguity weights built for %d tracts in %.1fs", len(merged), time.time() - t0)

    lisa = Moran_Local(merged["score"].values, w, permutations=199, seed=42)

    # esda quadrant codes: 1=HH, 2=LH, 3=LL, 4=HL
    quad_map = {1: "HH", 2: "LH", 3: "LL", 4: "HL"}
    sig = lisa.p_sim < 0.05
    cluster = np.where(sig, [quad_map[q] for q in lisa.q], "NS")

    log.info("Moran's I clusters: %s",
             pd.Series(cluster).value_counts().to_dict())
    return pd.DataFrame({
        "geoid": merged["geoid"],
        "moran_cluster": cluster,
        "moran_p": lisa.p_sim.round(5),
    })


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-moran", action="store_true")
    args = ap.parse_args()

    log.info("=== loading component data ===")
    acs = load_acs_trends()
    spatial = load_spatial()
    supply = load_supply()

    # Base frame: every tract with geometry, so a fully-data-starved tract still
    # gets a row (score NULL, completeness 0) rather than silently vanishing.
    base = pd.read_sql(
        "SELECT geoid, state_fips, county_fips FROM tract_geom WHERE vintage = 2020",
        engine(),
    )
    df = base.merge(acs, on="geoid", how="left")
    df = df.merge(spatial, on="geoid", how="left")
    df["county_fips_full"] = df["state_fips"] + df["county_fips"]
    if not supply.empty:
        supply_col = supply.rename(columns={"county_fips": "county_fips_full"})
        df = df.merge(supply_col, on="county_fips_full", how="left")
    else:
        df["supply_raw"] = np.nan

    # OZ flag via the 2010->2020 crosswalk, if loaded.
    with raw_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('public.opportunity_zones')")
            oz_exists = cur.fetchone()[0] is not None
    if oz_exists:
        oz = pd.read_sql("""
            SELECT DISTINCT x.geoid_2020 AS geoid, true AS is_oz
            FROM tract_xwalk_2010_2020 x
            JOIN opportunity_zones o ON o.geoid_2010 = x.geoid_2010
        """, engine())
        df = df.merge(oz, on="geoid", how="left")
        df["is_opportunity_zone"] = df["is_oz"].fillna(False)
    else:
        df["is_opportunity_zone"] = None

    log.info("=== component subscores (percentile-ranked, 0-100) ===")
    df["s_rent_momentum"] = pct_rank(df["rent_income_spread"])
    df["s_spatial"] = pct_rank(df["spatial_raw"])
    df["s_education_influx"] = pct_rank(df["edu_share_change_pp"])

    # Affordability: inverted-U. Some burden = room for rents to keep climbing
    # without breaking tenants; extreme burden (paper's own >50% tipping point)
    # is a risk flag, not a bonus.
    rti = df["rent_to_income"]
    afford = pd.Series(np.nan, index=df.index)
    afford[rti < 0.30] = 60 + (0.30 - rti[rti < 0.30]) / 0.30 * 20   # cheap: ok, not exciting
    afford[(rti >= 0.30) & (rti < 0.45)] = 100 - (rti[(rti >= 0.30) & (rti < 0.45)] - 0.30) / 0.15 * 20  # sweet spot
    afford[rti >= 0.45] = np.maximum(0, 80 - (rti[rti >= 0.45] - 0.45) * 200)  # severe burden: penalize
    df["s_affordability"] = afford.clip(0, 100)

    # Supply risk is inverted: MORE permitting relative to stock = LOWER score.
    df["s_supply_risk"] = 100 - pct_rank(df["supply_raw"]).fillna(50)
    df.loc[df["supply_raw"].isna(), "s_supply_risk"] = np.nan

    # Not yet ingested — neutral midpoint rather than penalizing every tract
    # for a layer that simply hasn't been built yet.
    df["s_safety"] = 50.0
    df["s_regulatory"] = 50.0

    log.info("=== weighted composite ===")
    comp_cols = list(WEIGHTS.keys())
    avail = df[comp_cols].notna()
    weight_arr = np.array([WEIGHTS[c] for c in comp_cols])
    # Reweight over only the components present for this tract, so a missing
    # layer (e.g. safety, not yet ingested) doesn't drag the score toward zero —
    # it's excluded from both numerator and denominator.
    weighted_sum = (df[comp_cols].fillna(0).values * weight_arr).sum(axis=1)
    weight_present = (avail.values * weight_arr).sum(axis=1)
    score = np.where(weight_present > 0, weighted_sum / weight_present, np.nan)
    df["score"] = pd.Series(score, index=df.index).round(2)

    # data_completeness: true core signal availability, not counting the two
    # not-yet-built neutral-midpoint layers (safety, regulatory) as "present".
    core_cols = ["s_rent_momentum", "s_spatial", "s_supply_risk",
                 "s_affordability", "s_education_influx"]
    df["data_completeness"] = df[core_cols].notna().mean(axis=1).round(3)

    scored_mask = df["data_completeness"] >= MIN_COMPLETENESS
    log.info("tracts with geometry: %d | with any score: %d | meeting completeness>=%.0f%%: %d",
             len(df), df["score"].notna().sum(), MIN_COMPLETENESS * 100, scored_mask.sum())

    if not args.skip_moran and scored_mask.sum() >= 20:
        moran = compute_moran(df[scored_mask][["geoid", "score"]])
        df = df.merge(moran, on="geoid", how="left")
    else:
        df["moran_cluster"] = None
        df["moran_p"] = None
        if args.skip_moran:
            log.info("--skip-moran set, leaving cluster fields null")

    log.info("=== writing tract_scores ===")
    cols = ["geoid", "score", "s_rent_momentum", "s_income_growth", "s_education_influx",
            "s_affordability", "s_spatial", "s_supply_risk", "s_safety", "s_regulatory",
            "rent_cagr_5y", "income_cagr_5y", "rent_income_spread", "rent_to_income",
            "is_opportunity_zone", "moran_cluster", "moran_p", "data_completeness"]
    df["s_income_growth"] = pct_rank(df["income_cagr_5y"])  # separate diagnostic subscore
    for c in cols:
        if c not in df.columns:
            df[c] = None

    def clean(v):
        if v is None or (isinstance(v, float) and np.isnan(v)):
            return None
        if isinstance(v, (np.floating, np.integer)):
            return float(v)
        if isinstance(v, np.bool_):
            return bool(v)
        return v

    rows = [tuple(clean(row[c]) for c in cols) for _, row in df.iterrows()]
    from db import upsert
    upsert("tract_scores", cols, rows, ["geoid"])

    log.info("tract_scores written: %d rows (%d scored, %d meet completeness threshold)",
             len(rows), df["score"].notna().sum(), scored_mask.sum())
    log_ingest("tract_scores", "provisional_weights", len(rows), 0,
               f"weights={WEIGHTS} min_completeness={MIN_COMPLETENESS}")

    log.info("=== refreshing county rollup ===")
    import subprocess
    subprocess.run([sys.executable, str((__file__).replace("score_tracts.py", "build_counties.py")),
                    "--refresh"], check=False)

    return 0


if __name__ == "__main__":
    sys.exit(main())
