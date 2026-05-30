#!/bin/bash
# ── Priya deploy update script ─────────────────────────────────────────────────
# Run on the VPS to pull latest code and restart:
#   sudo bash /opt/priya/deploy/update.sh

set -e

APP_DIR=/opt/priya
APP_USER=priya

echo "==> Pulling latest code..."
git -C $APP_DIR pull origin main

echo "==> Installing new dependencies (if any)..."
$APP_DIR/venv/bin/pip install -r $APP_DIR/requirements.txt --quiet

echo "==> Restarting Priya..."
systemctl restart priya

echo "==> Checking status..."
sleep 2
systemctl status priya --no-pager

echo ""
echo "✅ Update complete!"
echo "   Logs: sudo journalctl -u priya -f"
