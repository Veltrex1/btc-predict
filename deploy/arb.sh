#!/bin/bash
# Kalshi + Polymarket US arbitrage trader on the droplet. Run as root.
#   bash deploy/arb.sh shadow    install the service (first time) and run it without sending orders
#   bash deploy/arb.sh check     read-only: balances, both books, current gap, Polymarket order preview
#   bash deploy/arb.sh live      real orders (asks you to type LIVE)
#   bash deploy/arb.sh status    recent attempts and P&L
#   bash deploy/arb.sh off       stop trading (the service keeps running idle)
set -euo pipefail
ETC=/etc/btc-predict; APP=/opt/btc-predict; UV=/home/btc/.local/bin/uv
cmd=${1:-status}

setenv() {  # setenv KEY VALUE: replace or append in the env file
  if grep -q "^$1=" "$ETC/env"; then sed -i "s/^$1=.*/$1=$2/" "$ETC/env"; else echo "$1=$2" >> "$ETC/env"; fi
}
run_py() { sudo -u btc -H bash -c "cd $APP && set -a && . $ETC/env && set +a && $UV run python $*"; }
clear_halt() { rm -f "$APP/data/STOP"; run_py -m server.arb_trader --clear-halt; }

install_unit() {
  [ -f /etc/systemd/system/btc-arb.service ] && return
  cat > /etc/systemd/system/btc-arb.service <<UNIT
[Unit]
Description=btc-predict Kalshi + Polymarket US arbitrage
After=network-online.target
Wants=network-online.target

[Service]
User=btc
WorkingDirectory=$APP
EnvironmentFile=$ETC/env
ExecStart=$UV run python -m server.arb_trader --run
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
UNIT
  systemctl daemon-reload
  systemctl enable btc-arb >/dev/null
}

defaults() {
  for kv in ARB_MAX_CONTRACTS=10 ARB_MIN_EDGE=0.02 ARB_MIN_SECS=60 ARB_PAIRS_PER_WINDOW=1 ARB_DAILY_STOP=5 ARB_FLOOR=2; do
    grep -q "^${kv%%=*}=" "$ETC/env" || echo "$kv" >> "$ETC/env"
  done
}

case $cmd in
  shadow)
    install_unit; defaults; setenv ARB_MODE shadow; clear_halt
    systemctl restart btc-arb btc-predict
    echo "Arbitrage trader running in SHADOW mode (no orders). Watch: journalctl -u btc-arb -f" ;;
  check)
    run_py -m server.arb_trader --check ;;
  live)
    grep -q '^POLYMARKET_KEY_ID=.\+' "$ETC/env" || { echo "Polymarket US key missing in $ETC/env"; exit 1; }
    grep -q '^KALSHI_KEY_ID=.\+' "$ETC/env" || { echo "Kalshi key missing in $ETC/env"; exit 1; }
    install_unit; defaults
    run_py -m server.arb_trader --check
    echo
    read -r -p "Start LIVE arbitrage (up to $(grep ^ARB_MAX_CONTRACTS= $ETC/env | cut -d= -f2) contracts per pair, daily stop \$$(grep ^ARB_DAILY_STOP= $ETC/env | cut -d= -f2))? Type LIVE: " ans
    [ "$ans" = "LIVE" ] || { echo "Not started."; exit 1; }
    setenv ARB_MODE live; clear_halt
    systemctl restart btc-arb btc-predict
    echo "LIVE. Watch: journalctl -u btc-arb -f    Stop: bash $APP/deploy/arb.sh off" ;;
  off)
    setenv ARB_MODE off; touch "$APP/data/STOP"; chown btc:btc "$APP/data/STOP"
    systemctl restart btc-arb btc-predict 2>/dev/null || true
    echo "Arbitrage trading stopped." ;;
  status)
    journalctl -u btc-arb -n 15 --no-pager | grep -E "arb |HALT|starting|failed" || true
    run_py -m server.arb_trader --summary ;;
  *) echo "usage: bash deploy/arb.sh shadow|check|live|status|off"; exit 1 ;;
esac
