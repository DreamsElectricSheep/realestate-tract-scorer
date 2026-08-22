#!/bin/bash
# Deploy from the OneDrive source of truth to the Beelink runtime.
# Run from the Claude Code machine (Git Bash). OneDrive is NOT synced on the
# Beelink, so C:\DeepRock is the staging hop.
set -eu

SRC="/c/Users/ccast/OneDrive/Documents/Claude Projects/Real Estate"
HOST="deeprock@192.168.1.252"
KEY="$HOME/.ssh/id_ed25519"
STAGE="C:/DeepRock/realestate"

echo "=== staging ==="
ssh -i "$KEY" -o BatchMode=yes "$HOST" "if not exist C:\\DeepRock\\realestate mkdir C:\\DeepRock\\realestate" 2>/dev/null || true

scp -q -i "$KEY" -o BatchMode=yes -r "$SRC/scripts" "$HOST:$STAGE/"
scp -q -i "$KEY" -o BatchMode=yes -r "$SRC/sql"     "$HOST:$STAGE/"
scp -q -i "$KEY" -o BatchMode=yes -r "$SRC/web"     "$HOST:$STAGE/"
echo "staged to $STAGE"

echo "=== installing to /opt/realestate ==="
# Staged-script pattern: nested quoting through Windows SSH -> WSL bash is
# unreliable, so the remote half always lives in its own file.
ssh -i "$KEY" -o BatchMode=yes "$HOST" \
  "wsl -d Ubuntu-22.04 -u hedgefund -- bash /mnt/c/DeepRock/realestate/scripts/_install_remote.sh"
echo "=== deploy complete ==="
