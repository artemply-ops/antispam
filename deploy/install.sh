#!/usr/bin/env bash
# Выполняется на сервере из deploy.py. Ставит/обновляет бота в /opt/antispam.
set -euo pipefail
SRC=/tmp/antispam-deploy/app
APP=/opt/antispam

id antispam >/dev/null 2>&1 || useradd --system --home-dir "$APP" --shell /usr/sbin/nologin antispam
mkdir -p "$APP"
cp -a "$SRC/." "$APP/"   # .env и antispam.db на сервере не трогаются, если их нет в архиве

if [ ! -x "$APP/.venv/bin/python" ]; then
  python3 -m venv "$APP/.venv" 2>/dev/null || {
    apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3-venv >/dev/null
    python3 -m venv "$APP/.venv"
  }
fi
"$APP/.venv/bin/pip" install -q --disable-pip-version-check -r "$APP/requirements.txt"

chown -R antispam:antispam "$APP"
[ -f "$APP/.env" ] && chmod 600 "$APP/.env"

install -m 644 "$APP/deploy/antispam-bot.service" /etc/systemd/system/antispam-bot.service
systemctl daemon-reload
systemctl enable -q antispam-bot
systemctl restart antispam-bot
sleep 8
echo "status: $(systemctl is-active antispam-bot)"
journalctl -u antispam-bot -n 12 --no-pager -o cat
rm -rf /tmp/antispam-deploy
