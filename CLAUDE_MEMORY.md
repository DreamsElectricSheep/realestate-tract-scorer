# Real Estate — Project Memory

This file is the permanent, append-only session log for the Real Estate (Deep Rock
Multifamily Tract Scorer) project. Newest entry on top. Written by `/save`.

For "where exactly am I right now" between sessions, see the lighter `/checkpoint`
note instead (Hub `state/realestate/handoff.md`) — this file is the full history.

---

## Session History

### 2026-08-19 — Full pipeline goes live end-to-end

**What changed:**
- Diagnosed and fixed the bug that had killed the nationwide OSM POI-count job
  three nights running (silent `QueryCanceled` on a 4h statement timeout). Root
  cause: the `near`-tract CTE in `scripts/ingest_osm.py::step_counts()` joined
  `tract_geom` (84,415 rows) against `osm_poi` (2,059,874 rows) via
  `ST_DWithin(geography, geography, 5000)`, and Postgres's planner would *not*
  push that predicate down to the existing GiST index on `osm_poi.geom` — it
  fell back to a full nested-loop sequential scan (~173 billion comparisons,
  EXPLAIN cost ~1.8 trillion). Fix: added an explicit `p.geom && ST_Expand(pt,
  0.15)` bounding-box pre-filter ahead of the `ST_DWithin` check — this matches
  the GiST index's native `&&` operator class and forces an index scan (EXPLAIN
  cost dropped ~10,000x, to ~182M). Job then completed in ~10 minutes producing
  675,320 correct rows. Deployed fix via `scripts/deploy.sh`.
- User obtained a free Census API key and pasted it in-session; installed to
  `/opt/realestate/.keys.json` (mode 600) on the Beelink.
- Ran the full nationwide ACS 5-year ingest (`ingest_acs.py`): 51 states x 7
  vintages (2017–2023) = 357 requests, 556,811 tract-rows loaded. First attempt
  via plain `nohup ... &` vanished with zero output/trace for unknown reasons;
  relaunched with `setsid` and an explicit post-launch alive-check, which then
  ran cleanly to completion.
- Ran `score_tracts.py` against real data for the first time: all 84,415 tracts
  scored, 83,533 meet the 60% data-completeness threshold, Local Moran's I
  computed (8,206 High-High clusters, 2,588 Low-High outliers — the "expanding
  gentrification frontier" / "underpriced-in-a-hot-neighborhood" signals the
  source paper's theory called for but never implemented). County rollup
  refreshed (3,144 counties).
- Confirmed the live dashboard (`http://192.168.1.252:5008`, systemd service
  `realestate-dashboard.service`) is serving real scores via `/api/health` and
  `/api/top` — top-ranked tract is Providence RI (score 84.5, HH cluster,
  13.9pp rent-vs-income growth spread/yr).
- Registered this project in the Hub (`_Hub/PROJECTS.md`) as slug `realestate`
  for the first time — it wasn't previously indexed.

**What's still open:**
- **Phase 3 calibration** — not started. Current component weights (rent
  momentum 30 / spatial 20 / supply risk 15 / affordability 10 / education
  influx 10 / safety 10 (neutral placeholder) / regulatory 5 (neutral
  placeholder)) are asserted, not fitted. Next real step: backtest by scoring
  tracts as-of a past date and comparing to realized FHFA/Zillow appreciation
  since, then refit.
- **Opportunity Zones** — deliberately deferred (user's call). `opportunity_zones`
  table is empty; `scripts/ingest_oz.py` is ready and waiting on a manually
  downloaded designation list from the CDFI Fund (no stable public URL exists).
- **GitHub repo** — user wants this project pushed to a new private GitHub
  repo. Blocked mid-session: no `gh auth`, no SSH key registered with
  `github.com` from this machine (verified: no GITHUB_TOKEN/GH_TOKEN env vars,
  no cached credential-manager entry, `ssh -T git@github.com` → permission
  denied). Gave the user this machine's existing public key
  (`ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOK4t5O/P+6yfIpwZyN3WMCDCa0BUAp4KTdGI49ZQZj3 claude-code@windows`)
  to add at github.com/settings/keys. **Not yet confirmed added** — repo
  creation and push still pending as of this entry.
- **Known, accepted risk**: `scripts/config.py` hardcodes a Postgres password
  (`quantadmin123`) as a connection-string fallback default. User's explicit
  decision: leave as-is for now since it's a LAN-only DB with no financial/PII
  data, condition being the GitHub repo stays **private**. Revisit if the repo
  is ever made public or the DB scope changes.
- Phase 4 (paid RentCast property-financials tier) intentionally not started —
  by design, waits until Phase 3 proves the free tract-level model has real
  predictive lift.

**Architecture reference** (for future sessions): local source of truth is
this OneDrive folder; deployed via `scripts/deploy.sh` (stages through
`C:/DeepRock/realestate` on the Windows side, then `wsl -d Ubuntu-22.04 -u
hedgefund -- bash /mnt/c/DeepRock/realestate/scripts/_install_remote.sh`) to
`/opt/realestate` on the Beelink (`deeprock@192.168.1.252`, WSL2 Ubuntu
22.04). Own Postgres/PostGIS database (`realestate`, separate from the
trading fleet's `marketdata`), own venv (`/opt/realestate/venv`), deliberately
outside the board_review.py audit glob. **Nested-quoting gotcha**: complex
multi-command strings passed through `ssh ... "wsl ... -- bash -c '...'"` get
mangled by the Windows SSH layer (commands silently run against `cmd.exe`
instead of WSL bash). Reliable pattern: write the remote-side script to a
local file, `scp` it over, then `ssh ... "wsl ... -- bash /mnt/c/.../script.sh"`
— never inline multi-command strings through the SSH command argument.
