"""
Census tract boundary reconciliation, shared by the scorer and the calibrator.

THE PROBLEM THIS SOLVES
-----------------------
ACS vintages straddle a boundary redesign:

    2017, 2018, 2019   -> 2010 tract boundaries   (73,056 tracts)
    2020 ... 2023      -> 2020 tract boundaries   (84,4xx tracts)

A tract GEOID can persist across that redesign while the actual polygon it
names changes shape. Computing a 2017 -> 2023 trend with a plain
`groupby("geoid")` therefore compares two different pieces of ground for
every redrawn tract and reports the difference as "rent growth".

Measured on this database (2026-08-22): of 60,853 GEOIDs present in both the
2017 and 2023 vintages, 39,766 are unchanged but **21,069 were split or only
partially overlap**. Before this module existed, 19,571 scored tracts (23% of
all scored tracts) carried a mixed-boundary trend.

This module lives on its own precisely because `calibrate.py` used to
crosswalk correctly while `score_tracts.py` did not -- so the backtest fit
weights against a clean feature and the live scorer then applied them to a
noisy one. Both now import from here; neither restates the logic.

CAVEAT worth knowing: area-weighting a *median* is an approximation. There is
no exact way to recombine medians from sub-geographies without the underlying
microdata. Area weighting is the standard practice and is far better than
comparing incompatible geographies, but a crosswalked 2017 value is an
estimate, not a measurement.
"""
from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from db import engine

log = logging.getLogger("acs_boundaries")

# First ACS vintage published on 2020 tract boundaries. Vintages below this
# are on 2010 boundaries and must be crosswalked before being compared to
# anything at or above it.
FIRST_2020_BOUNDARY_VINTAGE = 2020


def crosswalk_2010_to_2020(df: pd.DataFrame, value_cols: list[str]) -> pd.DataFrame:
    """
    Reproject rows keyed by a 2010 tract GEOID onto 2020 GEOIDs, area-weighted.

    A 2010 tract that split contributes its value to each 2020 child; a 2020
    tract assembled from several 2010 parents gets the area-weighted mean of
    its contributors.

    `df` must have a `geoid` column (2010) plus every column in `value_cols`.
    Any other columns are dropped -- they have no meaning after reprojection.
    """
    if df.empty:
        return pd.DataFrame(columns=["geoid"] + value_cols)

    xwalk = pd.read_sql(
        "SELECT geoid_2010, geoid_2020, area_weight FROM tract_xwalk_2010_2020",
        engine(),
    )
    if xwalk.empty:
        log.warning("tract_xwalk_2010_2020 is empty -- cannot reconcile boundaries; "
                    "pre-2020 vintages will be dropped rather than silently mixed")
        return pd.DataFrame(columns=["geoid"] + value_cols)

    m = xwalk.merge(df, left_on="geoid_2010", right_on="geoid", how="inner")
    if m.empty:
        return pd.DataFrame(columns=["geoid"] + value_cols)

    key = m["geoid_2020"]
    base_w = m["area_weight"].fillna(0.0)

    result = pd.DataFrame(index=pd.Index(sorted(key.unique()), name="geoid_2020"))
    for col in value_cols:
        vals = pd.to_numeric(m[col], errors="coerce")
        # Zero out the weight wherever the value is missing. Without this the
        # numerator skips NaN rows (pandas sum defaults to skipna) while the
        # denominator still counts their weight, biasing every partially-null
        # tract downward.
        w = base_w.where(vals.notna(), 0.0)
        num = (vals.fillna(0.0) * w).groupby(key).sum()
        den = w.groupby(key).sum().replace(0.0, np.nan)
        result[col] = num / den

    return result.reset_index().rename(columns={"geoid_2020": "geoid"})


def unify_acs_boundaries(df: pd.DataFrame, value_cols: list[str]) -> pd.DataFrame:
    """
    Take a raw multi-vintage acs_tract frame and return it entirely on 2020
    boundaries, so a trend computed across vintages compares like with like.

    Vintages >= FIRST_2020_BOUNDARY_VINTAGE pass through untouched. Earlier
    vintages are crosswalked per-vintage and re-stamped with the 2020 GEOID.
    """
    if df.empty:
        return df

    old = df[df["vintage"] < FIRST_2020_BOUNDARY_VINTAGE]
    new = df[df["vintage"] >= FIRST_2020_BOUNDARY_VINTAGE]

    if old.empty:
        return new.reset_index(drop=True)

    pieces = [new]
    for vintage, chunk in old.groupby("vintage"):
        mapped = crosswalk_2010_to_2020(chunk, value_cols)
        if mapped.empty:
            continue
        mapped["vintage"] = vintage
        pieces.append(mapped)

    unified = pd.concat(pieces, ignore_index=True, sort=False)
    log.info("boundary reconciliation: %d pre-2020 rows crosswalked onto 2020 "
             "geoids, %d rows already native; %d unified rows",
             len(old), len(new), len(unified))
    return unified
