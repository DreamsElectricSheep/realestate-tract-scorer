#!/usr/bin/env python3
"""
Tests for the two things that have actually produced wrong numbers here:
boundary crosswalk weighting, and the deal math.

No pytest dependency -- run it directly:
    /opt/realestate/venv/bin/python3 tests/test_core.py

The crosswalk tests use a synthetic weight table injected via monkeypatch, so
they assert the arithmetic rather than the current contents of the database.
The deal-math tests check the SQL scanner against independently-computed
reference values -- the scanner's formulas live in SQL for speed across 85k
tracts, so this is the only place they can be verified.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import numpy as np
import pandas as pd

FAILURES: list[str] = []
PASSES = 0


def check(name: str, actual, expected, tol: float = 1e-6):
    global PASSES
    if expected is None:
        ok = actual is None or (isinstance(actual, float) and np.isnan(actual))
    elif isinstance(expected, (int, float)) and not isinstance(expected, bool):
        ok = actual is not None and not (isinstance(actual, float) and np.isnan(actual)) \
             and abs(float(actual) - float(expected)) <= tol
    else:
        ok = actual == expected
    if ok:
        PASSES += 1
        print(f"  ok   {name}")
    else:
        FAILURES.append(name)
        print(f"  FAIL {name}: got {actual!r}, expected {expected!r}")


# ───────────────────────────────────────────── crosswalk weighting
def test_crosswalk():
    import acs_boundaries

    # 2010 tract "A" splits evenly into 2020 tracts X and Y.
    # 2010 tracts "B" and "C" merge into 2020 tract Z, 75/25.
    fake_xwalk = pd.DataFrame([
        {"geoid_2010": "A", "geoid_2020": "X", "area_weight": 0.5},
        {"geoid_2010": "A", "geoid_2020": "Y", "area_weight": 0.5},
        {"geoid_2010": "B", "geoid_2020": "Z", "area_weight": 0.75},
        {"geoid_2010": "C", "geoid_2020": "Z", "area_weight": 0.25},
    ])
    acs_boundaries.pd.read_sql = lambda *a, **k: fake_xwalk  # type: ignore[assignment]

    print("\ncrosswalk_2010_to_2020")

    # A split: both children inherit the parent's value unchanged.
    src = pd.DataFrame([{"geoid": "A", "rent": 1000.0}])
    out = acs_boundaries.crosswalk_2010_to_2020(src, ["rent"]).set_index("geoid")
    check("split sends parent value to both children (X)", out.loc["X", "rent"], 1000.0)
    check("split sends parent value to both children (Y)", out.loc["Y", "rent"], 1000.0)

    # A merge: area-weighted mean, 0.75*1000 + 0.25*2000 = 1250.
    src = pd.DataFrame([{"geoid": "B", "rent": 1000.0}, {"geoid": "C", "rent": 2000.0}])
    out = acs_boundaries.crosswalk_2010_to_2020(src, ["rent"]).set_index("geoid")
    check("merge is area-weighted, not a plain mean", out.loc["Z", "rent"], 1250.0)

    # THE BUG THIS TEST EXISTS FOR: when one contributor's value is NULL, its
    # weight must be excluded from the denominator too. The original
    # implementation summed a NaN-skipping numerator over a full-weight
    # denominator, biasing every partially-null tract downward -- here it
    # would have returned 0.75*1000/1.0 = 750 instead of the correct 1000.
    src = pd.DataFrame([{"geoid": "B", "rent": 1000.0}, {"geoid": "C", "rent": np.nan}])
    out = acs_boundaries.crosswalk_2010_to_2020(src, ["rent"]).set_index("geoid")
    check("null contributor drops out of BOTH numerator and denominator",
          out.loc["Z", "rent"], 1000.0)

    # All contributors null -> null, not zero.
    src = pd.DataFrame([{"geoid": "B", "rent": np.nan}, {"geoid": "C", "rent": np.nan}])
    out = acs_boundaries.crosswalk_2010_to_2020(src, ["rent"]).set_index("geoid")
    check("all-null merge yields null, not 0", out.loc["Z", "rent"], None)


def test_unify_boundaries():
    import acs_boundaries

    fake_xwalk = pd.DataFrame([
        {"geoid_2010": "OLD", "geoid_2020": "NEW", "area_weight": 1.0},
    ])
    acs_boundaries.pd.read_sql = lambda *a, **k: fake_xwalk  # type: ignore[assignment]

    print("\nunify_acs_boundaries")
    raw = pd.DataFrame([
        {"geoid": "OLD", "vintage": 2017, "rent": 900.0},   # 2010 boundaries
        {"geoid": "NEW", "vintage": 2023, "rent": 1200.0},  # 2020 boundaries
    ])
    out = acs_boundaries.unify_acs_boundaries(raw, ["rent"])
    got = out[out.geoid == "NEW"].sort_values("vintage")
    check("pre-2020 vintage is restamped onto its 2020 geoid", len(got), 2)
    check("  ...carrying the old value", float(got.iloc[0]["rent"]), 900.0)
    check("  ...and the native value untouched", float(got.iloc[1]["rent"]), 1200.0)
    check("no rows remain under the retired 2010 geoid",
          int((out.geoid == "OLD").sum()), 0)


# ───────────────────────────────────────────── deal math
def reference_deal(price, rent, units, vacancy_pct, opex_pct, tax, insurance,
                   down_payment, rate_pct, term_years):
    """Textbook underwriting, computed independently of the SQL implementation."""
    gpr = rent * units * 12
    egi = gpr * (1 - vacancy_pct / 100)
    noi = egi - egi * (opex_pct / 100) - tax - insurance
    cap = noi / price * 100
    down = min(price, down_payment)
    loan = max(price - down_payment, 0)
    r = (rate_pct / 100) / 12
    n = term_years * 12
    pi = loan / n if r == 0 else loan * (r * (1 + r) ** n) / ((1 + r) ** n - 1)
    cash_flow = noi - pi * 12
    return {"noi": noi, "cap": cap, "monthly_pi": pi, "cash_flow": cash_flow,
            "coc": (cash_flow / down * 100) if down else None,
            "dscr": (noi / (pi * 12)) if pi else None}


def test_scan_math_against_reference():
    """Integration: the live /api/scan must agree with the reference formulas."""
    import json
    import urllib.request

    print("\n/api/scan vs reference underwriting")
    url = ("http://127.0.0.1:5008/api/scan?down_payment=60000&max_loan=240000"
           "&limit=5&rate=7&term=30&vacancy=7&opex=40&insurance=1500")
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            payload = json.load(r)
    except Exception as e:
        FAILURES.append(f"scan endpoint unreachable: {e}")
        print(f"  FAIL scan endpoint unreachable: {e}")
        return

    a = payload["assumptions"]
    # Regression guard: the scanner used to accept a `units` multiplier that
    # scaled rent but not price, producing impossible cap rates (24.4% at
    # units=3). It is now pinned to 1.
    check("scanner is pinned to a single unit", float(a["units"]), 1.0)

    for t in payload["tracts"][:5]:
        ref = reference_deal(
            price=float(t["price"]), rent=float(t["rent_per_unit"]), units=1,
            vacancy_pct=a["vacancy_pct"], opex_pct=a["opex_pct"],
            tax=float(t["tax"]), insurance=a["insurance"],
            down_payment=60000, rate_pct=a["rate"], term_years=a["term_years"])
        label = (t["county_name"] or t["geoid"])[:22]
        check(f"NOI      {label}", float(t["noi"]), ref["noi"], tol=0.01)
        check(f"cap rate {label}", float(t["cap_rate"]), ref["cap"], tol=0.01)
        check(f"mortgage {label}", float(t["monthly_pi"]), ref["monthly_pi"], tol=0.01)
        check(f"cashflow {label}", float(t["annual_cash_flow"]), ref["cash_flow"], tol=0.01)

    # Sanity ceilings that exist because ACS data artifacts blew past them.
    for t in payload["tracts"]:
        cap = float(t["cap_rate"])
        if not (0 < cap <= 25):
            FAILURES.append(f"implausible cap rate {cap} in {t['geoid']}")
            print(f"  FAIL implausible cap rate {cap} in {t['geoid']}")
    print(f"  ok   all {len(payload['tracts'])} cap rates within plausible bounds")
    global PASSES
    PASSES += 1


def test_score_weights_sum():
    from config import SCORE_COMPONENTS, SCORE_WEIGHTS
    print("\nscore weights")
    check("weights sum to 100", sum(SCORE_WEIGHTS.values()), 100)
    check("every component has a human label",
          all(c.get("label") and c.get("desc") for c in SCORE_COMPONENTS), True)


def main() -> int:
    test_crosswalk()
    test_unify_boundaries()
    test_score_weights_sum()
    test_scan_math_against_reference()

    print(f"\n{'─'*54}")
    if FAILURES:
        print(f"{len(FAILURES)} FAILED, {PASSES} passed")
        for f in FAILURES:
            print(f"   ✗ {f}")
        return 1
    print(f"all {PASSES} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
