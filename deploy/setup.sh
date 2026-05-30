#!/bin/bash
# ── Priya VPS one-time setup script ───────────────────────────────────────────
# Run as root on a fresh Ubuntu 22.04 VPS:
#   sudo bash setup.sh YOUR_DOMAIN
#
# Example:
#   sudo bash setup.sh priya.nehachildcare.in

set -e   # exit on any error

DOMAIN=${1:?"Usage: sudo bash setup.sh YOUR_DOMAIN"}
APP_DIR=/opt/priya
APP_USER=priya

echo "==> Setting up Priya for domain: $DOMAIN"

# ── 1. System packages ─────────────────────────────────────────────────────────
echo "==> Installing system packages..."
apt-get update -q
apt-get install -y \
    python3.11 python3.11-venv python3-pip \
    nginx \
    certbot python3-certbot-nginx \
    git \
    ufw

# ── 2. Firewall ────────────────────────────────────────────────────────────────
echo "==> Configuring firewall..."
ufw allow OpenSSH
ufw allow 'Nginx Full'    # ports 80 and 443
ufw --force enable

# ── 3. App user and directory ──────────────────────────────────────────────────
echo "==> Creating app user and directory..."
useradd -r -s /bin/bash -d $APP_DIR -m $APP_USER 2>/dev/null || true
mkdir -p $APP_DIR/recordings $APP_DIR/metrics

# ── 4. Clone / pull repo ───────────────────────────────────────────────────────
echo "==> Pulling code..."
if [ -d "$APP_DIR/.git" ]; then
    git -C $APP_DIR pull origin main
else
    git clone https://github.com/alokranjan04/gemini_hinglish_voice_agent.git $APP_DIR
fi

# ── 5. Python virtual environment ─────────────────────────────────────────────
echo "==> Setting up Python venv..."
python3.11 -m venv $APP_DIR/venv
$APP_DIR/venv/bin/pip install --upgrade pip --quiet
$APP_DIR/venv/bin/pip install -r $APP_DIR/requirements.txt --quiet

# ── 6. .env file ───────────────────────────────────────────────────────────────
if [ ! -f "$APP_DIR/.env" ]; then
    echo "==> Creating .env template — fill in your API keys!"
    cat > $APP_DIR/.env << 'EOF'
DEEPGRAM_API_KEY=your_deepgram_key
SARVAM_API_KEY=your_sarvam_key
GEMINI_API_KEY=your_gemini_key
GOOGLE_CALENDAR_ID=your_calendar@gmail.com
GOOGLE_SPREADSHEET_ID=your_sheet_id
GMAIL_USER=your@gmail.com
GMAIL_APP_PASSWORD=xxxx xxxx xxxx xxxx
DOCTOR_EMAIL=doctor@example.com
EOF
    echo "   *** Edit $APP_DIR/.env with real values before starting! ***"
fi

# ── 7. Google credentials file ─────────────────────────────────────────────────
if [ ! -f "$APP_DIR/google-credentials.json" ]; then
    echo "   *** Copy your google-credentials.json to $APP_DIR/ ***"
fi

# ── 8. Ownership ───────────────────────────────────────────────────────────────
chown -R $APP_USER:$APP_USER $APP_DIR

# ── 9. Nginx config ────────────────────────────────────────────────────────────
echo "==> Configuring nginx..."
sed "s/YOUR_DOMAIN/$DOMAIN/g" $APP_DIR/deploy/nginx.conf \
    > /etc/nginx/sites-available/priya
ln -sf /etc/nginx/sites-available/priya /etc/nginx/sites-enabled/priya
rm -f /etc/nginx/sites-enabled/default   # remove default page

# Test nginx config before reloading
nginx -t
systemctl reload nginx

# ── 10. SSL certificate (Let's Encrypt) ───────────────────────────────────────
echo "==> Getting SSL certificate for $DOMAIN..."
certbot --nginx -d $DOMAIN --non-interactive --agree-tos \
    --email admin@$DOMAIN --redirect

# Auto-renewal via cron (certbot installs this automatically, but make sure)
systemctl enable certbot.timer

# ── 11. Systemd service ────────────────────────────────────────────────────────
echo "==> Installing systemd service..."
cp $APP_DIR/deploy/priya.service /etc/systemd/system/priya.service
systemctl daemon-reload
systemctl enable priya
systemctl start priya

# ── 12. Status ─────────────────────────────────────────────────────────────────
echo ""
echo "✅ Setup complete!"
echo ""
echo "   App status : sudo systemctl status priya"
echo "   App logs   : sudo journalctl -u priya -f"
echo "   Nginx logs : sudo tail -f /var/log/nginx/error.log"
echo ""
echo "   Set in Vobiz → Answer URL:"
echo "   https://$DOMAIN/answer"
echo ""
echo "   Dashboard: https://$DOMAIN/"
