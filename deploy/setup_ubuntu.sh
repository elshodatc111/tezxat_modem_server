#!/usr/bin/env bash
# Ubuntu serverda o'rnatish:  sudo bash deploy/setup_ubuntu.sh
set -euo pipefail

APP_DIR=/opt/sms-modem-server
SRC_DIR="$(cd "$(dirname "$0")/.." && pwd)"

if [ "$(id -u)" -ne 0 ]; then echo "root (sudo) bilan ishga tushiring"; exit 1; fi

apt-get update
apt-get install -y python3 python3-venv python3-pip usb-modeswitch minicom rsync

id smsmodem >/dev/null 2>&1 || useradd --system --create-home --shell /usr/sbin/nologin smsmodem
usermod -aG dialout smsmodem

mkdir -p "$APP_DIR"
rsync -a --delete --exclude '.venv' --exclude '.env' --exclude 'data' --exclude '__pycache__' "$SRC_DIR"/ "$APP_DIR"/
mkdir -p "$APP_DIR/data"

python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --upgrade pip
"$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt"

if [ ! -f "$APP_DIR/.env" ]; then
  cp "$APP_DIR/.env.example" "$APP_DIR/.env"
  sed -i "s/^SECRET_KEY=.*/SECRET_KEY=$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')/" "$APP_DIR/.env"
  echo ">>> $APP_DIR/.env yaratildi. DATABASE_URL va ADMIN_PASSWORD ni to'g'rilang!"
fi
chmod 600 "$APP_DIR/.env"
chown -R smsmodem:smsmodem "$APP_DIR"

cp "$APP_DIR/deploy/sms-modem-worker.service" /etc/systemd/system/
cp "$APP_DIR/deploy/sms-modem-web.service" /etc/systemd/system/
systemctl daemon-reload
echo
echo "Tayyor. Keyingi qadamlar:"
echo "  1) nano $APP_DIR/.env          # DATABASE_URL, ADMIN_PASSWORD"
echo "  2) systemctl enable --now sms-modem-worker sms-modem-web"
echo "  3) Brauzerda: http://SERVER_IP:8088"
echo "  Loglar: journalctl -u sms-modem-worker -f"
