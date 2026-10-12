#!/bin/bash
# Switch the droplet's trader from shadow to LIVE (real orders). Run as root on the droplet.
set -euo pipefail
ETC=/etc/btc-predict; APP=/opt/btc-predict; UV=/home/btc/.local/bin/uv
grep -q '^KALSHI_KEY_ID=.\+' "$ETC/env" || { echo "Set KALSHI_KEY_ID in $ETC/env first"; exit 1; }
[ -f "$ETC/kalshi.pem" ] || { echo "Copy your private key to $ETC/kalshi.pem first"; exit 1; }
echo "Checking credentials (read-only)..."
sudo -u btc bash -c "cd $APP && set -a && . $ETC/env && $UV run python -m server.trader --check"
echo
read -r -p "Start LIVE trading with \$$(grep ^TRADE_ALLOCATION= $ETC/env | cut -d= -f2) for $(grep ^TRADE_HOURS= $ETC/env | cut -d= -f2)h? Type LIVE to confirm: " ans
[ "$ans" = "LIVE" ] || { echo "Not started."; exit 1; }
sudo -u btc sqlite3 "$APP/data/live.sqlite" "DELETE FROM bot_state" 2>/dev/null || \
  sudo -u btc bash -c "cd $APP && $UV run python -c \"import sqlite3;c=sqlite3.connect('data/live.sqlite');c.execute('DELETE FROM bot_state');c.commit()\""
rm -f "$APP/data/STOP"
sed -i 's/^TRADER_MODE=.*/TRADER_MODE=live/' "$ETC/env"
systemctl restart btc-predict
sleep 8
journalctl -u btc-predict -n 20 --no-pager | grep -E "TRADER|refusing|Error" || true
echo "Live. Watch: journalctl -u btc-predict -f    Stop: bash $APP/deploy/stop.sh"
