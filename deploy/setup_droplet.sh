#!/bin/bash
# One-time setup on a fresh DigitalOcean droplet (Ubuntu 24.04, run as root):
#
#   curl -fsSL https://raw.githubusercontent.com/Veltrex1/btc-predict/claude/kalshi-odds-widget-tm3lw0/deploy/setup_droplet.sh | bash
#
# Installs the server as a systemd service behind Caddy (HTTPS on <ip>.sslip.io),
# generates an API token, and starts in SHADOW mode (no real orders).
# Then: put your Kalshi key in place (instructions printed at the end) and run
# deploy/go_live.sh when you're ready.
set -euo pipefail
BRANCH="${BRANCH:-claude/kalshi-odds-widget-tm3lw0}"
REPO="${REPO:-https://github.com/Veltrex1/btc-predict.git}"
APP=/opt/btc-predict
ETC=/etc/btc-predict

apt-get update -qq
apt-get install -y -qq git curl ufw debian-keyring debian-archive-keyring apt-transport-https gnupg nodejs npm
if ! command -v caddy >/dev/null; then
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -qq && apt-get install -y -qq caddy
fi
id btc >/dev/null 2>&1 || useradd --system --create-home --shell /usr/sbin/nologin btc
if [ ! -d "$APP/.git" ]; then git clone --branch "$BRANCH" "$REPO" "$APP"; else git -C "$APP" pull --ff-only; fi
chown -R btc:btc "$APP"
sudo -u btc bash -c "curl -LsSf https://astral.sh/uv/install.sh | sh" >/dev/null
UV=/home/btc/.local/bin/uv
sudo -u btc bash -c "cd $APP && $UV sync -q && cd dashboard && npm install --silent --no-audit --no-fund && npm run build --silent"

IP=$(curl -fsS https://api.ipify.org)
HOST="$(echo "$IP" | tr . -).sslip.io"
sudo -u btc mkdir -p "$APP/data"
mkdir -p "$ETC" && chmod 700 "$ETC" && chown btc:btc "$ETC"
if [ ! -f "$ETC/env" ]; then
  TOKEN=$(head -c 24 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 32)
  cat > "$ETC/env" <<ENV
TRADER_MODE=shadow
BTC_API_TOKEN=$TOKEN
BTC_PUBLIC_URL=https://$HOST
KALSHI_KEY_ID=
KALSHI_PRIVATE_KEY_PATH=$ETC/kalshi.pem
TRADE_ALLOCATION=10
TRADE_STOP_LOSS=5
TRADE_HOURS=24
ENV
  chmod 600 "$ETC/env" && chown btc:btc "$ETC/env"
fi
TOKEN=$(grep ^BTC_API_TOKEN= "$ETC/env" | cut -d= -f2)

cat > /etc/systemd/system/btc-predict.service <<UNIT
[Unit]
Description=btc-predict signal server and Kalshi trader
After=network-online.target
Wants=network-online.target

[Service]
User=btc
WorkingDirectory=$APP
EnvironmentFile=$ETC/env
ExecStart=$UV run uvicorn server.app:app --host 127.0.0.1 --port 8765
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT

# recorder (second-level Kalshi book/trades + spot venues) and the learning loop run 24/7
for svc in recorder loop; do
  case $svc in
    recorder) CMD="$UV run python -m recorder.run" ;;
    loop)     CMD="$UV run python -m pipeline.loop --every 6" ;;
  esac
  cat > /etc/systemd/system/btc-$svc.service <<UNIT
[Unit]
Description=btc-predict $svc
After=network-online.target
Wants=network-online.target

[Service]
User=btc
WorkingDirectory=$APP
EnvironmentFile=$ETC/env
ExecStart=$CMD
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
UNIT
done

# one-time history for the model (runs in the background; ~1h): 3 years of Coinbase bars, then train
if [ ! -f "$APP/models/model.pkl" ]; then
  sudo -u btc nohup bash -c "cd $APP && $UV run python -m pipeline.backfill_coinbase --days 1095 && \
    $UV run python -m pipeline.backfill_kalshi --candles --hist-every 6 && \
    $UV run python -m pipeline.train --years 3 --test-months 11 && systemctl --no-block restart btc-predict || true" \
    > "$APP/data/bootstrap.log" 2>&1 &
fi

cat > /etc/caddy/Caddyfile <<CADDY
$HOST {
    reverse_proxy 127.0.0.1:8765
}
CADDY
ufw allow OpenSSH >/dev/null && ufw allow 80,443/tcp >/dev/null && ufw --force enable >/dev/null
systemctl daemon-reload
systemctl enable --now btc-predict btc-recorder btc-loop caddy
systemctl restart caddy btc-predict btc-recorder btc-loop

cat <<DONE

================================================================
 Server running in SHADOW mode (no real orders) at:
   https://$HOST/health
 Dashboard:  https://$HOST/?token=$TOKEN
 API token:  $TOKEN

 Next steps
 1. Copy your Kalshi private key (from your Mac):
      scp kalshi-key.pem root@$IP:$ETC/kalshi.pem
    then on the droplet:
      chown btc:btc $ETC/kalshi.pem && chmod 600 $ETC/kalshi.pem
 2. Put your Kalshi Key ID in $ETC/env (KALSHI_KEY_ID=...), then:
      systemctl restart btc-predict
      sudo -u btc bash -c 'cd $APP && set -a && . $ETC/env && $UV run python -m server.trader --check'
 3. Mac widget: put this in ~/.btc-predict on your Mac:
      BTC_SERVER_URL=https://$HOST
      BTC_API_TOKEN=$TOKEN
 4. Go live (real money, 24h, \$10, stop at -\$5):  bash $APP/deploy/go_live.sh
    Stop at any time:                               bash $APP/deploy/stop.sh
================================================================
DONE
