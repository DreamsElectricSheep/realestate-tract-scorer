#!/bin/bash
# Monthly refresh: re-pull the annual/quarterly source datasets, then re-score.
#
# Safe to run at any time. The HTTP layer caches every response on disk and
# enforces a per-host daily budget, so a re-run against unchanged upstream
# data costs approximately zero requests. OSM is deliberately excluded -- it
# is a 13 GB download whose POI layer barely moves year to year; refresh it
# by hand with `ingest_osm.py all` when it's actually worth the bandwidth.
#
# Ordering matters: sources first, scoring last, because score_tracts.py reads
# everything else.
set -u

PY=/opt/realestate/venv/bin/python3
S=/opt/realestate/scripts
LOG=/opt/realestate/logs/refresh.log

exec >> "$LOG" 2>&1
echo "════════════════════════════════════════════════════════════"
echo "[$(date -Is)] refresh starting"

run() {
  local name="$1"; shift
  echo "--- $name ---"
  if "$@"; then
    echo "[$(date -Is)] $name OK"
  else
    # Deliberately non-fatal: one dead upstream URL should not stop the
    # others or block the re-score. Failures are visible in this log and in
    # the ingest_log table.
    echo "[$(date -Is)] $name FAILED (rc=$?) -- continuing"
  fi
}

run "ACS demographics"  "$PY" "$S/ingest_acs.py"
run "Building permits"  "$PY" "$S/ingest_bps.py"
run "Zillow ZHVI/ZORI"  "$PY" "$S/ingest_zillow.py"
run "FHFA HPI"          "$PY" "$S/ingest_fhfa.py"
run "Score tracts"      "$PY" "$S/score_tracts.py"

echo "[$(date -Is)] refresh complete"
