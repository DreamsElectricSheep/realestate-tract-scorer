#!/bin/bash
# Install the dashboard as a systemd service. Run as root in WSL.
# Follows the fleet's hard-won pattern: NO ExecStartPre — combined with
# Restart=always it creates a self-defeating restart loop.
set -u

cat > /etc/systemd/system/realestate-dashboard.service <<'UNIT'
[Unit]
Description=Deep Rock Real Estate Tract Scorer Dashboard
After=network.target postgresql.service

[Service]
User=hedgefund
WorkingDirectory=/opt/realestate/web
Environment=PYTHONPATH=/opt/realestate/scripts
ExecStart=/opt/realestate/venv/bin/python3 /opt/realestate/web/app.py
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable realestate-dashboard.service
systemctl restart realestate-dashboard.service
sleep 4

echo "=== STATUS ==="
systemctl is-active realestate-dashboard.service
systemctl is-enabled realestate-dashboard.service

echo "=== LOCAL HTTP ==="
curl -s -o /dev/null -w "GET /        -> %{http_code}\n" http://localhost:5008/
curl -s -o /dev/null -w "GET /health  -> %{http_code}\n" http://localhost:5008/api/health
echo "--- health payload ---"
curl -s http://localhost:5008/api/health | head -c 700; echo

echo "=== WSL IP (for portproxy) ==="
hostname -I | awk '{print $1}'
