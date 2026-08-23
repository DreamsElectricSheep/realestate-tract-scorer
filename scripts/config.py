"""
Central config for the multifamily tract scorer.

Deliberately isolated from the trading fleet:
  - own database (realestate, NOT marketdata)
  - own venv (/opt/realestate/venv)
  - own project root (/opt/realestate), outside the board_review.py audit glob
"""
import json
import os
from pathlib import Path

# ---------------------------------------------------------------- paths
ROOT = Path(os.environ.get("RE_ROOT", "/opt/realestate"))
DATA = ROOT / "data"
RAW = DATA / "raw"
CACHE = DATA / "cache"
LOGS = ROOT / "logs"
SQL = ROOT / "sql"
WEB = ROOT / "web"

for _p in (DATA, RAW, CACHE, LOGS, SQL, WEB):
    _p.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------- database
DB_URL = os.environ.get(
    "RE_DB_URL",
    "postgresql+psycopg2://quantadmin:quantadmin123@localhost:5432/realestate",
)
DB_URL_RAW = DB_URL.replace("postgresql+psycopg2://", "postgres://")

# ---------------------------------------------------------------- api keys
# Optional. Drop a JSON file at /opt/realestate/.keys.json (mode 600):
#   {"census": "...", "fbi": "...", "hud": "...", "fred": "...", "rentcast": "..."}
#
# Every key here is OPTIONAL for Phases 0-3:
#   census — free, instant, https://api.census.gov/data/key_signup.html
#            Without it the Census API allows ~500 requests/day per IP, which our
#            state-level bulk pulls stay under. With it, the ceiling is far higher.
#   fbi    — free, https://api.data.gov/signup/  (crime tier degrades gracefully to neutral)
#   hud    — free, https://www.huduser.gov/portal/dataset/fmr-api.html
#   fred   — free, https://fred.stlouisfed.org/docs/api/api_key.html (10Y treasury)
#   rentcast — PAID. Phase 4 only. Not used anywhere in Phases 0-3.
_KEYFILE = ROOT / ".keys.json"


def _load_keys() -> dict:
    if _KEYFILE.exists():
        try:
            return json.loads(_KEYFILE.read_text())
        except Exception:
            return {}
    return {}


_KEYS = _load_keys()


def key(name: str, default: str | None = None) -> str | None:
    """Fetch an optional API key. Env var RE_KEY_<NAME> wins over the keyfile."""
    return os.environ.get(f"RE_KEY_{name.upper()}") or _KEYS.get(name) or default


CENSUS_KEY = key("census")
FBI_KEY = key("fbi")
HUD_KEY = key("hud")
FRED_KEY = key("fred")

# ---------------------------------------------------------------- acs
# 5-year estimates. Multi-vintage is what makes this spatial-TEMPORAL rather than
# a snapshot. Non-overlapping vintages are what you compare for real change:
# 2013-2017 vs 2018-2022 share no sample years.
ACS_VINTAGES = [2017, 2018, 2019, 2020, 2021, 2022, 2023]
ACS_BASE = "https://api.census.gov/data/{year}/acs/acs5"

# Census returns these as "unavailable/suppressed" sentinels, NOT null.
# Casting them straight to int is the single most common way to poison a model.
ACS_NULL_SENTINELS = {
    -666666666, -999999999, -888888888, -555555555, -222222222, -333333333,
}

ACS_VARS = {
    "B01003_001E": "population",
    "B19013_001E": "median_hh_income",
    "B25064_001E": "median_gross_rent",
    "B25077_001E": "median_home_value",
    "B25103_001E": "median_re_taxes",       # -> free effective tax rate proxy
    "B25003_001E": "tenure_total",
    "B25003_003E": "tenure_renter",
    "B15003_001E": "edu_total",
    "B15003_022E": "edu_bachelors",
    "B15003_023E": "edu_masters",
    "B15003_024E": "edu_professional",
    "B15003_025E": "edu_doctorate",
    "B25004_001E": "vacancy_total",
    "B17001_002E": "below_poverty",
    "B25071_001E": "median_rent_pct_income",  # ACS's own rent burden measure
}
# NOTE: "bachelor's or higher" = 022+023+024+025, divided by B15003_001E.
# B15003_022E alone is bachelor's ONLY — a common and quiet mislabeling.

# ---------------------------------------------------------------- geo
TIGER_YEAR_2020 = 2023          # TIGER vintage carrying 2020 tract boundaries
GEOFABRIK_US = "https://download.geofabrik.de/north-america/us-latest.osm.pbf"

# POI classes pulled from OSM. Queried as nwr (node/way/relation) because
# universities and industrial buildings are polygons, essentially never nodes.
OSM_POI_FILTERS = {
    "university":  ["amenity=university", "amenity=college"],
    "hospital":    ["amenity=hospital"],
    "transit_stop": ["highway=bus_stop", "public_transport=station", "railway=station"],
    "industrial":  ["building=warehouse", "landuse=industrial", "building=industrial"],
    "grocery":     ["shop=supermarket"],
    "cafe":        ["amenity=cafe"],          # classic early-gentrification marker
    "school":      ["amenity=school"],
    "park":        ["leisure=park"],
}

PROXIMITY_RADIUS_M = 5000

# ---------------------------------------------------------------- scoring
# Single source of truth for the composite score. Lives here, in the one module
# with no heavy dependencies, because THREE consumers need it and every copy
# that got restated somewhere went stale: score_tracts.py computes with it,
# calibrate.py backtests against it, and the dashboard explains it to the user.
#
# Weights sum to 100 across components that actually carry signal. Two earlier
# components (safety, regulatory) were removed rather than left at a hardcoded
# neutral 50 -- see score_tracts.py. Calibrated 2026-08-22; the proportions
# come from a backtest of ACS 2017->2019 predictors against realized
# 2019->2025 FHFA appreciation, blended by judgment rather than applied raw
# (see calibrate.py and CLAUDE_MEMORY.md for why).
SCORE_COMPONENTS = [
    {
        "key": "s_rent_momentum", "weight": 26,
        "label": "Rent growth vs. income growth",
        "desc": "How much faster rent is rising here than local incomes "
                "-- the core “getting hot” signal.",
    },
    {
        "key": "s_supply_risk", "weight": 26,
        "label": "New construction risk",
        "desc": "Higher is better: fewer new apartments are being built nearby "
                "to compete with.",
    },
    {
        "key": "s_spatial", "weight": 21,
        "label": "Walkability & transit access",
        "desc": "Proximity to universities, transit, hospitals and other amenities.",
    },
    {
        "key": "s_affordability", "weight": 14,
        "label": "Room for rent to grow",
        "desc": "Rent is high enough to matter but hasn't already maxed out "
                "what renters can pay.",
    },
    {
        "key": "s_education_influx", "weight": 13,
        "label": "Education influx",
        "desc": "Growth in college-educated residents moving into the area.",
    },
]

SCORE_WEIGHTS = {c["key"]: c["weight"] for c in SCORE_COMPONENTS}

# Below this share of components present, a tract's score is not published to
# the leaderboard and renders as "thin data" on the map.
MIN_COMPLETENESS = 0.6

# ---------------------------------------------------------------- misc
USER_AGENT = "DeepRock-RealEstate-Scorer/1.0 (personal research; contact ccast77@gmail.com)"
REQUEST_TIMEOUT = 45  # seconds. The reference implementation had no timeouts at all.
