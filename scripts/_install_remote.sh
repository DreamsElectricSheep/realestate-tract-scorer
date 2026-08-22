#!/bin/bash
# Runs ON the Beelink (WSL, user hedgefund). Staged by deploy.sh.
set -eu
SRC=/mnt/c/DeepRock/realestate
DST=/opt/realestate

cp -f "$SRC/scripts"/*.py "$DST/scripts/"
cp -f "$SRC/scripts"/*.sh "$DST/scripts/" 2>/dev/null || true
cp -f "$SRC/sql"/*.sql    "$DST/sql/"
mkdir -p "$DST/web/templates"
cp -f "$SRC/web"/*.py "$DST/web/" 2>/dev/null || true
cp -f "$SRC/web/templates"/*.html "$DST/web/templates/" 2>/dev/null || true
chmod +x "$DST/scripts"/*.py

echo "=== scripts ==="
ls -1 "$DST/scripts/"
echo "=== sql ==="
ls -1 "$DST/sql/"
echo "=== web ==="
ls -1 "$DST/web/" "$DST/web/templates/"
echo "INSTALL_OK"
