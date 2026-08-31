# Real Estate — Project Memory

This file is the permanent, append-only session log for the Real Estate (Deep Rock
Multifamily Tract Scorer) project. Newest entry on top. Written by `/save`.

For "where exactly am I right now" between sessions, see the lighter `/checkpoint`
note instead (Hub `state/realestate/handoff.md`) — this file is the full history.

---

## Session History

### 2026-08-31 (later) — Closed the Connecticut boundary crosswalk gap

Picked up the top open item from the 2026-08-23 discovery: 884 CT tracts
(every CT tract) had zero 2010→2020 crosswalk coverage, so CT trends
couldn't get a multi-year rent/income signal — the home-market state, since
Hamden CT is the distance-filter base.

**Root cause had two layers, both fixed.**

1. **Geometry**: CT replaced 8 counties with 9 Planning Regions in 2022,
   changing every tract GEOID's county-code digits (001–015 → 110–190)
   independent of the national 2010→2020 boundary redesign. The official
   Census crosswalk predates the change and can't bridge it. Fix: new
   `scripts/ingest_ct_relabel.py` — downloads TIGER 2021 (old-coded)
   geometry via the existing `ingest_tiger.py --state 09 --tiger-year 2021
   --vintage 2021`, computes an area-weighted intersection crosswalk against
   current `tract_geom`, upserts directly into the existing
   `tract_xwalk_2010_2020` table (zero downstream schema change). Achieved
   883/883 (100%) coverage of old CT tracts — rejected an earlier
   suffix-matching shortcut that only got 86.8%. Spot-checked: 881 clean 1:1
   matches at weight≈1.0, 4 partial-split rows summing to exactly 1.0000
   (genuine water-tract splits).

2. **A second, non-obvious gap the crosswalk alone didn't close**: CT's ACS
   vintages 2020 and 2021 are *also* old-coded — its coding-scheme cutover
   is vintage 2022, not the national vintage-2020 boundary cutover. The
   crosswalk fix alone still left those two years missing (series showed
   `[2017,2018,2019,2022,2023]`, silently skipping 2020-2021). Two things
   were gating on the wrong constant:
   - `scripts/acs_boundaries.py::unify_acs_boundaries` used a single
     hardcoded `FIRST_2020_BOUNDARY_VINTAGE = 2020` cutoff for the whole
     country. Replaced with `STATE_CODING_CUTOVER_VINTAGE = {"09": 2022}`,
     a per-state override — **not** a geoid-membership check (tried that
     first, reverted it): ~22,000 real 2010→2020 split parents nationally
     reuse their exact parent GEOID for one child at ~99%+ area weight, so
     checking "does this geoid already name a current tract" would
     misclassify their pre-2020 rows as native and skip crosswalking them.
     The per-state-vintage rule avoids that collision entirely.
   - `web/app.py::load_acs_series_unified` had its *own* hardcoded
     `vintage >= 2020` / `vintage < 2020` SQL split ahead of calling
     `unify_acs_boundaries` — so even after the module-level fix, this
     caller was still pre-filtering CT's 2020-2021 rows out before they
     ever reached the reconciliation logic. Fixed to fetch candidates in
     one un-filtered `OR`-based query (a single query, not two concatenated
     ones, to avoid double-fetching the ~22,000 geoids that are their own
     historical contributor) and let `unify_acs_boundaries` decide nativity.

**Verified end-to-end**: tract 09150815000 (the original bug-report tract)
went from `vintages: [2022, 2023]` → `[2017, 2018, 2019, 2020, 2021, 2022,
2023]`, full 7-year span. Spot-checked 5 random CT tracts: 3 got the full
7-year span, 2 correctly show only 2020-2023 — confirmed those two have no
ACS data at all pre-2020 under their old-coded geoid (genuinely new tracts
introduced by the 2020 redesign, not a bug). Rescored all 84,415 tracts
nationally (`score_tracts.py`) to propagate the fix into live scores/trends.
Test suite grown 33 → 38 checks (added CT-cutover and non-CT-state cases);
all pass. Committed and pushed
(`github.com/DreamsElectricSheep/realestate-tract-scorer`).

**Still open**: score remains statistically unvalidated (−0.48pp decile
spread, see 2026-08-23 entry); `zillow_series` unused by the scorer; empty
tables `irs_migration`, `qcew_county`, `opportunity_zones`, `reg_flags`,
`crime_agency`; Flask dev server in production (low priority, LAN-only).


### 2026-08-31 — Found and fixed a real ~24-hour outage; project status check

User asked "what's left for this project," which is normally a status
question. Checking live state before answering found the dashboard actually
down: `curl` to :5008 timed out completely (connection refused at the
network level, not an HTTP error).

**Root cause: a WSL2 networking fault after a host reboot, not application
code.** `systemctl status` showed `realestate-dashboard.service` crash-looping
(17,000+ restarts, ~24h) on `OSError: [Errno 98] Address already in use` for
port 5008. But nothing was actually holding it: `/proc/net/tcp` inside WSL
showed zero entries for port 5008 (hex 1388), `ss`/`netstat` agreed, and
checking the WINDOWS side directly (`netstat -ano`, `netsh interface
portproxy show all`) found no listener and no stale port-forward rule either.
Conclusion: a phantom kernel-level port reservation inside the WSL2 utility
VM, most likely from an abrupt host sleep/reboot that didn't cleanly tear down
the previous process's socket. The standard fix is `wsl --shutdown` from
Windows, which resets the whole VM's network stack -- **not done**, because
that VM also runs the live trading fleet (freedom_bot.py, pumpfun_recon.py,
telegram_nwbo_scanner.py, other gunicorn dashboards on :8000 etc.) and would
have killed all of it. This is a hard boundary, not a judgment call: never
run `wsl --shutdown` on this host without the owner doing it themselves, or
explicit confirmation naming the trading-fleet impact.

**Fix applied instead: moved the dashboard off the cursed port.**
5008 -> **5011** (verified free in `/proc/net/tcp` and via `ss` before
committing to it, on both the WSL and Windows side). Updated every reference:
`web/app.py` (docstring + `app.run`), `README.md`, `scripts/install_service.sh`,
`tests/test_core.py`. Service now binds cleanly and immediately -- confirmed
`curl http://192.168.1.252:5011/api/health` responds. **New URL:
http://192.168.1.252:5011** -- the old bookmark (`:5008`) will not work again
without a `wsl --shutdown` the owner runs deliberately.

**Second real bug found while fixing the first: `deploy.sh` never staged
`tests/`.** Editing `tests/test_core.py` locally and running `deploy.sh`
silently left the OLD file (still referencing :5008) on the Beelink --
`runtests.sh` was therefore testing stale code and failed with a connection
error that had nothing to do with the actual fix. Fixed `deploy.sh` and
`_install_remote.sh` to stage/install `tests/` alongside `scripts/`, `sql/`,
`web/`. Re-ran after the fix: all 33 checks pass for real this time.

Everything else from the 2026-08-23 audit remains in the state that session
left it -- see that entry for the still-open items (Connecticut crosswalk
gap, unvalidated score, unused zillow_series, etc.). This entry is purely the
outage-and-port-move.


### 2026-08-23 (later) — Plain-English rewrite + a CT geography discovery

**Rewrote the jargon out of the deal scanner, deal calculator, and trend
panel.** Owner's complaint was concrete: "I don't want to have to look up what
CAGR means." Every industry term now leads with what it means and names the
jargon second, e.g. "Return if paid in cash / 4.74% / ...Real estate calls
this the cap rate." Scanner rows now lead with the two numbers a buyer decides
on — "14.1% a year on your cash · +$8,446/yr left over" — then plain-language
mortgage coverage ("Comfortably covers the mortgage" / "Does not cover the
mortgage") instead of a bare DSCR figure. Trend card retitled "How this area
has changed" with a one-line explainer under every row. Input labels
de-jargoned too (vacancy → "Months empty / unpaid rent", opex → "Running
costs").

**Found and fixed another instance of the boundary bug.** The audit fixed
`score_tracts.py`, but `/api/tract/<geoid>` had the same flaw in a code path I
never touched: it queried `acs_tract` raw by geoid with no crosswalk, so a
tract reshaped in the 2020 redesign showed only whichever vintages happened to
share its exact 2020 geoid. That is what produced the owner's "Trend ·
2022-2023" — a one-year span silently labeled as the trend. Added
`load_acs_series_unified()` in `web/app.py`, reusing `acs_boundaries`.
Verified: a normal tract now spans 2017-2023 (was collapsing for redrawn ones).

**DISCOVERY — Connecticut is a genuine geography exception, and it's the home
market.** While verifying the fix, found 884 CT tracts with ZERO crosswalk
coverage — and CT is the *only* affected state (884 of 84,415 nationally).
Root cause: Connecticut replaced its 8 counties with 9 Planning Regions
effective 2022, which changed its tract GEOID county prefixes. Confirmed in
the data:
  - `tract_geom` (TIGER 2023) CT county codes: 110–190 (Planning Regions)
  - Census 2010→2020 crosswalk file CT codes: 001–015 (old counties)
  - `acs_tract`: vintages 2017–2021 use 001–015, vintages 2022–2023 use 110–190
So the official Census crosswalk predates the change and cannot bridge it.
Consequence: **CT tracts show only a 2022–2023 trend and cannot get a
multi-year rent/income signal**, which matters because Hamden CT is the
distance-filter home base. NOT fixed this session — needs Connecticut's
specific old-county-tract → planning-region-tract relationship file
(Census publishes CT-specific 2020→2022 relationship files separately).
This is the top open item.

Tests still pass (33 checks). Committed and pushed.


### 2026-08-23 — Full audit, then fixed every finding

Audited the whole project against the live database (not by reading code and
inferring) and then fixed all 18 findings. Audit report published as an
artifact: https://claude.ai/code/artifact/4b588bca-ca78-49a2-ac8b-eaba737130a8

**The big one — boundary mixing corrupted the core signal for 23% of tracts.**
`score_tracts.py::load_acs_trends()` computed rent/income CAGR with a plain
`groupby("geoid")` across all seven ACS vintages. But 2017–2019 sit on 2010
tract boundaries and 2020–2023 on 2020 boundaries, and a GEOID can survive
the redesign while its polygon changes shape. Measured: of 60,853 GEOIDs in
both the 2017 and 2023 vintages, 39,766 are unchanged but **21,069 were split
or only partially overlap**; 19,571 scored tracts carried a mixed-boundary
trend. Worse, `calibrate.py` *did* crosswalk correctly — so the backtest fit
weights against a clean feature and the scorer applied them to a noisy one.
Fix: new `scripts/acs_boundaries.py` (`crosswalk_2010_to_2020`,
`unify_acs_boundaries`), imported by **both** scripts so they cannot diverge
again. Also fixed a latent bug in the original crosswalk: it summed a
NaN-skipping numerator over a full-weight denominator, biasing every
partially-null tract downward. After the fix, 83,494 tracts have a full
2017→2023 span on *consistent* boundaries (up from 60,853 mixed).

**Two calculator traps that silently flattered every deal.**
(1) `/api/scan` accepted a `units` multiplier that scaled rent but not price —
since price is the tract's median value for ONE home, `units=3` produced a
24.4% cap rate vs 10.3% at `units=1` on the same scan. Removed the field
entirely; there is no honest way to scale a single-home median to a multi-unit
building without listing data. (2) The Deal Calculator treated blank insurance
as $0 — the one input with no free data source, so the one most often left
blank. Now defaults to $1,500, labels it "(assumed)", and warns below the
results.

**Closed the structural split.** `/api/scan` never joined `tract_scores`, so
months of neighborhood scoring and the money math were two separate products.
It now returns score + Moran cluster per row and accepts `min_score`, so you
can rank by cash return *within* areas the scorer rates well.

**Score now means what it says.** `s_safety` (10) and `s_regulatory` (5) were
hardcoded to 50 for every tract in the country because `crime_agency` and
`reg_flags` were never populated — 15 of every 100 points were a constant.
Removed from the weight vector rather than faked (writing NULL, not 50, so
nothing downstream reads them as "measured average"). Remaining five rescaled
to sum to 100: rent momentum 26, supply risk 26, spatial 21, affordability 14,
education 13. Top score moved 84.5 → 89.2 as the constant drag came out; rank
order essentially preserved.

**Killed the weight-drift bug class for good.** Weights had been restated in
three places (scorer, calibrator, dashboard template) and all three had gone
stale. Now defined once in `config.py::SCORE_COMPONENTS` and imported by the
scorer, the calibrator, and injected into the template at render time.

**Other fixes:** map now renders sub-threshold tracts faded/dashed with a
"thin data" tooltip and legend entry instead of coloring them identically to
fully-scored ones; `calibrate.py` baseline read from live config and both
comparison columns rescaled to the same total; pandas `fillna` downcast
deprecation resolved before its behavior flips; dropped `fhfa_hpi_tract`
(FHFA publishes no tract-level series — it could never be filled); removed
unused `metric` param; corrected the stale port-5007 docstring.

**Ops, all previously absent:** `scripts/backup_db.sh` (core dump 26 MB +
full dump 703 MB, both verified restorable via `pg_restore --list` and gzip
integrity check, 30-day retention); `scripts/refresh_all.sh`; cron installed
at 03:15 nightly backup and 04:30 monthly refresh — verified the existing 61
trading-fleet cron lines were preserved and the cron daemon is running.
`requirements.txt` pinned from the live venv (45 packages, Python 3.10.12).
`tests/test_core.py` — 33 checks covering crosswalk arithmetic (including the
null-weight regression) and the SQL scanner's deal math against independently
computed reference formulas; all pass. `README.md` written (project had none).

**Re-calibrated after the fix.** Decile spread improved from **−2.19pp to
−0.48pp** — close to flat now, still marginally the wrong sign. Recorded as
`calibration_report` row 2.

**Still open / deliberately not done:**
- **The score remains unvalidated.** −0.48pp is not predictive lift. The
  window straddles COVID migration, which plausibly explains it, but that is
  a hypothesis. Testing it properly needs older ACS vintages (2013–2016) to
  build a non-COVID control window.
- `zillow_series` — 596,654 rows ingested, still consumed by nothing. Best
  candidate for the next real feature (fresher, monthly cross-check on the
  ACS rent trend).
- `qcew_county`, `irs_migration`, `opportunity_zones`, `crime_agency`,
  `reg_flags` still empty. IRS county-to-county migration was flagged early as
  a better "affluent influx" measure than the POI counting that carries 21
  weight points. `reg_flags` was NOT populated deliberately — those are
  verifiable legal facts about real jurisdictions and inventing them would be
  worse than leaving the table empty.
- Dashboard still runs on Flask's dev server (LAN-only, single user).
- DB password still hardcoded in `config.py` (accepted risk; repo is private,
  Postgres bound to 127.0.0.1 only).


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

## 2026-08-22 — GitHub auth resolved, repo created, history scrubbed

`gh` CLI authenticated on Christopher-XPS via the device-code flow (`gh auth
login --web`) — same machine the SSH-key path in `github-access` memory
already unblocked for pushing, this closes the remaining gap (repo
*creation*, which needs the REST API, not just git's SSH transport). Logged
in as `DreamsElectricSheep`, token scopes include `repo`. This machine can
now both push to existing repos and create new ones.

Created `github.com/DreamsElectricSheep/realestate-tract-scorer` (**private**)
and pushed the already-prepared initial commit.

**Found and fixed during the push:** `.claude/settings.local.json` (local
Claude Code permission config — Bash allowlist entries containing the
Beelink's LAN IP, SSH command patterns, and the same `quantadmin123` DB
password already accepted in `scripts/config.py`) had been committed
alongside the real project files. Untracked it and added it to `.gitignore`
— it's machine-specific config, not project code, and shouldn't be versioned
regardless of contents.

**Owner asked for zero risk, not just "low risk enough."** The untrack commit
only removed it from the current tree; it was still recoverable from the
first commit's history. Verified before deciding what to do: swept every
tracked file for other secret-shaped strings (found nothing else), and
checked whether the Beelink's Postgres is actually reachable from the LAN —
confirmed bound to `127.0.0.1:5432` only, not exposed, which is what made
the original "accepted risk conditional on staying private" call in the
Phase-3 entry above actually sound (not just asserted). Then did a full
history rewrite: orphan-branch squash to a single clean commit with the same
tree contents minus the settings file, force-pushed over the two-commit
history. Verified the old commits are gone from the local object store
(`git gc --prune=now`, confirmed unfetchable) and that GitHub's own protocol
has no reachable ref to them by SHA. The one thing not fully provable from
this end is GitHub's own backend GC timing — outside what git itself can
control or confirm — but for every practical purpose (private repo, SHA
never shared, not fetchable, not visible in the web UI) this is complete.

Current state: `main` = single commit, clean tree, no secrets in current
files or reachable history. `.claude/settings.local.json` gitignored.
