# Multifamily Tract Scorer

Scores all ~84,000 U.S. census tracts for multifamily investment potential, and
underwrites individual deals against that backdrop. Runs entirely on free public
data — no paid API is required for anything currently built.

**Dashboard:** http://192.168.1.252:5011 (LAN only)

---

## What it does

| Tab | What it answers |
|---|---|
| **Filters + map** | "Where in the country is worth looking, and why?" National choropleth drilling from county to tract, filtered by state, score, market pattern, home value, or driving distance from home base. |
| **Top 50** | "What are the best-scoring tracts, unfiltered?" |
| **Deal calculator** | "Does *this specific listing* work?" Cap rate, NOI, mortgage, cash flow, cash-on-cash, DSCR. |
| **Deal scanner** | "Given my down payment and mortgage size, where in the country do the numbers work?" Runs the calculator's math against every tract and ranks by cash-on-cash return. |

Clicking any tract gives a plain-English breakdown of *why* it scored the way it
did, plus a Zillow deep-link to live listings inside that exact tract boundary.

## The score

Five components, weights summing to 100, defined in one place
(`scripts/config.py` → `SCORE_COMPONENTS`) and imported by everything else:

| Component | Weight | Signal |
|---|---:|---|
| Rent growth vs. income growth | 26 | Rent outpacing local incomes — the core "getting hot" signal |
| New construction risk | 26 | *Inverted*: heavy multifamily permitting nearby is a penalty |
| Walkability & transit access | 21 | Universities, transit, hospitals, amenities within 5 km |
| Room for rent to grow | 14 | Burdened but not maxed out |
| Education influx | 13 | Growth in the college-educated share |

Plus **Local Moran's I** spatial clustering, surfaced as plain-English market
patterns: *Momentum zone* (strong tract, strong neighbors), *Hidden value*
(lagging tract inside a hot area — the classic value-add target), *Isolated
spike*, *Quiet market*, *No clear pattern*.

**Crime and local-regulation signals are deliberately excluded**, not set to a
neutral placeholder. No free nationwide source exists for either, and a
hardcoded midpoint silently ate 15 of the 100 points before this was fixed.

## Architecture

```
OneDrive (source of truth)  ──deploy.sh──▶  Beelink /opt/realestate
                                            ├── venv/          isolated from the trading fleet
                                            ├── data/raw/      13 GB OSM extract + source files
                                            └── postgres: realestate DB (PostGIS)
```

The design is **tract-first**, deliberately: score all 84k tracts offline from
bulk data, and only then look at individual properties. The inverse
(address-first, hitting paid APIs per property) is what the original design
called for and is financially impossible at national scale.

### Data sources — all free

| Source | Table | Notes |
|---|---|---|
| ACS 5-year, 2017–2023 | `acs_tract` | 51 states × 7 vintages = 357 requests total, not 595,000 |
| TIGER tract geometry | `tract_geom` | 2020 boundaries |
| Census 2010→2020 crosswalk | `tract_xwalk_2010_2020` | **Load-bearing** — see below |
| OpenStreetMap (Geofabrik) | `osm_poi`, `tract_poi_counts` | One 13 GB download, filtered locally; replaces ~680,000 Overpass calls |
| Census Building Permits | `bps_permits` | Supply-risk signal |
| FHFA HPI (MSA level) | `fhfa_hpi_msa` | Calibration ground truth |
| Zillow ZHVI/ZORI | `zillow_series` | Ingested; not yet consumed by the scorer |

## The boundary problem (read this before touching the scorer)

ACS vintages straddle a tract redesign: **2017–2019 use 2010 boundaries,
2020–2023 use 2020 boundaries.** A GEOID can survive the redesign while the
polygon it names changes shape — so a naive `groupby("geoid")` across vintages
compares two different pieces of ground and reports the difference as rent
growth. Measured here: of 60,853 GEOIDs in both the 2017 and 2023 vintages,
**21,069 were split or only partially overlap.**

`scripts/acs_boundaries.py` reconciles this, and both the scorer and the
calibrator import it. It exists as a shared module specifically because the two
once had separate implementations — the calibrator crosswalked correctly, the
scorer didn't, so weights fitted on a clean feature were applied to a noisy one.

## Running it

```bash
# deploy local changes to the Beelink
bash scripts/deploy.sh

# full refresh (also runs monthly via cron)
bash scripts/refresh_all.sh

# re-score only
python3 scripts/score_tracts.py

# backtest the weights (--dry-run to skip writing a report row)
python3 scripts/calibrate.py --dry-run

# tests
python3 tests/test_core.py
```

**Scheduled:** nightly backup (03:15), monthly refresh + re-score (2nd of the
month, 04:30). Backups land in `/opt/realestate/backups` — a small "core" dump
of the expensive-to-rebuild tables, plus a full dump, both pruned at 30 days.

## Honest limitations

- **The score is not validated.** The most recent backtest shows a top-minus-
  bottom decile spread of **−0.48pp** against realized 2019–2025 FHFA
  appreciation. That is close to flat, and slightly the wrong sign. The window
  straddles COVID-era migration, which plausibly explains it, but nothing here
  has yet demonstrated predictive lift. Treat the score as a structured way to
  triage 84,000 tracts, not as evidence a tract will appreciate.
- **The scanner uses tract medians, not listings.** Its "price" is the tract's
  median home value and its "rent" the tract median — two different groups of
  homes, which can genuinely diverge. Filters catch the worst artifacts
  (suppressed values, ACS top-codes, impossible yields), but a scanner row is a
  lead to verify, never a deal.
- **Insurance has no data source.** It is a flat assumption everywhere and
  varies enormously by geography. Coastal results are the least trustworthy.
- **Opportunity Zones are not loaded.** The tax mechanics also changed under the
  2025 law; verify current rules before weighting OZ status at all.
- **Fair housing:** scoring neighborhoods on income and education is fine for
  acquisition screening. Keep it out of any tenant-selection or marketing path.

## Layout

```
scripts/
  config.py            paths, API keys, score weights ← single source of truth
  acs_boundaries.py    2010→2020 tract reconciliation
  http_cache.py        cached + throttled + budgeted HTTP; makes rate limits structurally impossible
  ingest_*.py          one per source, all idempotent and re-runnable at zero request cost
  score_tracts.py      composite score + Moran's I
  calibrate.py         backtest and refit
  backup_db.sh         nightly dumps
  refresh_all.sh       monthly re-ingest + re-score
web/
  app.py               Flask API + dashboard
  templates/index.html single-page UI
tests/test_core.py     crosswalk arithmetic + deal math vs reference formulas
sql/schema.sql         idempotent DDL
```
