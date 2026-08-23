#!/bin/bash
# Nightly backup of the realestate database.
#
# WHAT'S WORTH BACKING UP, AND WHAT ISN'T
# ---------------------------------------
# The database is ~1.5 GB, but most of that is DERIVED and cheap-ish to
# rebuild (osm_poi, tract_poi_counts, tract_geom -- all reproducible from
# files on disk or public downloads). What is genuinely expensive or
# irreplaceable:
#
#   acs_tract    357 rate-limited Census API requests behind an API key
#   tract_scores the actual product; ~3 min of compute plus everything above
#   calibration_report  a historical record that cannot be regenerated
#   ingest_log          provenance; same
#
# So this takes two backups: a small "core" dump that restores the valuable
# state in seconds, and a full dump for genuine disaster recovery. Both are
# compressed; the core one is small enough to keep many copies of.
#
# Usage:  bash backup_db.sh          (run from cron, or by hand)
set -euo pipefail

DB=realestate
OUT=/opt/realestate/backups
KEEP_DAYS=30
STAMP=$(date +%Y%m%d-%H%M%S)

mkdir -p "$OUT"

CORE_TABLES=(acs_tract tract_scores calibration_report ingest_log
             tract_xwalk_2010_2020 county_cbsa_xwalk bps_permits fhfa_hpi_msa)

core_args=()
for t in "${CORE_TABLES[@]}"; do core_args+=(-t "$t"); done

echo "[$(date -Is)] core dump -> $OUT/core-$STAMP.sql.gz"
sudo -u postgres pg_dump -d "$DB" "${core_args[@]}" | gzip -9 > "$OUT/core-$STAMP.sql.gz"

echo "[$(date -Is)] full dump -> $OUT/full-$STAMP.dump"
# Custom format: parallel-restorable and already compressed. Written via stdout
# rather than pg_dump -f, because -f writes as the postgres user, which has no
# write access to this hedgefund-owned directory.
sudo -u postgres pg_dump -d "$DB" -Fc > "$OUT/full-$STAMP.dump"

# Verify the dumps are non-trivial rather than assuming success.
for f in "$OUT/core-$STAMP.sql.gz" "$OUT/full-$STAMP.dump"; do
  sz=$(stat -c %s "$f")
  if [ "$sz" -lt 100000 ]; then
    echo "ERROR: $f is only $sz bytes -- dump likely failed" >&2
    exit 1
  fi
  printf '  ok  %-46s %s\n' "$(basename "$f")" "$(du -h "$f" | cut -f1)"
done

# Prune old backups, but never leave zero: if a run ever starts failing, the
# last good backup must survive the retention sweep.
find "$OUT" -name 'core-*.sql.gz' -mtime +$KEEP_DAYS -delete 2>/dev/null || true
find "$OUT" -name 'full-*.dump'   -mtime +$KEEP_DAYS -delete 2>/dev/null || true
remaining=$(find "$OUT" -name 'full-*.dump' | wc -l)
echo "[$(date -Is)] done. $remaining full backup(s) retained in $OUT"

# RESTORE (for whoever needs this at 2am):
#   core:  gunzip -c core-STAMP.sql.gz | sudo -u postgres psql -d realestate
#   full:  sudo -u postgres pg_restore -d realestate -c -j4 full-STAMP.dump
