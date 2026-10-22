#!/bin/bash
# Kalshi + Polymarket US arbitrage trader on this Mac. Polymarket US only accepts orders from a home/office
# connection it can place in an allowed state, not from a cloud server, so the live trader runs here.
# Run from the repo folder (works with the Mac's built-in bash 3.2):
#   bash deploy/mac_arb.sh setup     one time: install, enter keys (hidden), link to the dashboard
#   bash deploy/mac_arb.sh check     read-only: balances, both books, Polymarket order validation
#   bash deploy/mac_arb.sh shadow    run without sending orders
#   bash deploy/mac_arb.sh live      real orders (asks you to type LIVE)
#   bash deploy/mac_arb.sh status    recent attempts and P&L
#   bash deploy/mac_arb.sh off       stop trading
# It runs as a background service (launchd) wrapped in `caffeinate`, so it keeps going when Terminal is closed
# and the Mac stays awake while plugged in. Only one machine may be live: keep the server in shadow or off.
set -euo pipefail
APP="$(cd "$(dirname "$0")/.." && pwd)"
DIR="$HOME/.btc-predict-arb"
ENVF="$DIR/env"
PEM="$DIR/kalshi.pem"
LOG="$DIR/arb.log"
LABEL="com.btc-predict.arb"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
SERVER="${ARB_SERVER:-root@144.202.12.14}"
UV="$(command -v uv || echo "$HOME/.local/bin/uv")"
cmd="${1:-status}"

need_setup() { [ -f "$ENVF" ] || { echo "Run first: bash deploy/mac_arb.sh setup"; exit 1; }; }

setenv() {  # setenv KEY VALUE: replace or append (values are shell-quoted, so secrets with symbols are safe)
  touch "$ENVF"
  grep -v "^$1=" "$ENVF" > "$ENVF.tmp" || true
  printf '%s=%q\n' "$1" "$2" >> "$ENVF.tmp"
  mv "$ENVF.tmp" "$ENVF"
  chmod 600 "$ENVF"
}
has() { grep -q "^$1=." "$ENVF" 2>/dev/null; }
run_py() { ( cd "$APP" && set -a && . "$ENVF" && set +a && "$UV" run python "$@" ); }
clear_halt() { rm -f "$APP/data/STOP"; run_py -m server.arb_trader --clear-halt; }

install_agent() {
  mkdir -p "$HOME/Library/LaunchAgents"
  cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array>
    <string>/bin/bash</string><string>-c</string>
    <string>cd "$APP" &amp;&amp; set -a &amp;&amp; . "$ENVF" &amp;&amp; set +a &amp;&amp; exec /usr/bin/caffeinate -is "$UV" run python -m server.arb_trader --run</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$LOG</string>
  <key>StandardErrorPath</key><string>$LOG</string>
</dict></plist>
PLIST
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$PLIST"
}
stop_agent() { launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true; }
agent_running() { launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1; }

case $cmd in
  setup)
    if ! command -v uv >/dev/null; then
      echo "Installing uv (the Python tool this project uses) from astral.sh ..."
      curl -LsSf https://astral.sh/uv/install.sh | sh
      UV="$HOME/.local/bin/uv"
    fi
    echo "Installing project packages (a few minutes the first time) ..."
    (cd "$APP" && "$UV" sync -q)
    mkdir -p "$DIR" "$APP/data"; chmod 700 "$DIR"
    if [ ! -f "$PEM" ]; then
      echo "Copying your Kalshi private key from the server (it asks for the server password) ..."
      scp "$SERVER:/etc/btc-predict/kalshi.pem" "$PEM"
    fi
    chmod 600 "$PEM"
    setenv KALSHI_PRIVATE_KEY_PATH "$PEM"
    if ! has KALSHI_KEY_ID; then
      read -r -p "Kalshi key id (press Enter to read it from the server): " v
      if [ -z "$v" ]; then v="$(ssh "$SERVER" "grep '^KALSHI_KEY_ID=' /etc/btc-predict/env" | cut -d= -f2-)"; fi
      setenv KALSHI_KEY_ID "$v"
    fi
    if ! has POLYMARKET_KEY_ID; then
      read -r -p "Polymarket US key id: " v; setenv POLYMARKET_KEY_ID "$v"
      read -r -s -p "Polymarket US secret key (typing is hidden): " v; echo; setenv POLYMARKET_SECRET_KEY "$v"
    fi
    for kv in ARB_MODE=off ARB_SOURCE=mac ARB_MAX_CONTRACTS=10 ARB_MIN_EDGE=0.02 ARB_MIN_SECS=60 \
              ARB_PAIRS_PER_WINDOW=1 ARB_DAILY_STOP=5 ARB_FLOOR=2; do
      has "${kv%%=*}" || setenv "${kv%%=*}" "${kv#*=}"
    done
    # report to the dashboard using the server link the SwiftBar widget already uses
    if [ -f "$HOME/.btc-predict" ]; then
      # shellcheck disable=SC1091
      . "$HOME/.btc-predict"
      if [ -n "${BTC_SERVER_URL:-}" ] && [ -n "${BTC_API_TOKEN:-}" ]; then
        setenv ARB_REPORT_URL "${BTC_SERVER_URL%/}/arb/report"
        setenv BTC_API_TOKEN "$BTC_API_TOKEN"
        echo "Dashboard link found: trades will show on your dashboard."
      fi
    fi
    has ARB_REPORT_URL || echo "No ~/.btc-predict dashboard link found: trades will not show on the dashboard."
    echo "Setup done. Next: bash deploy/mac_arb.sh check" ;;
  check)
    need_setup; run_py -m server.arb_trader --check ;;
  shadow)
    need_setup; setenv ARB_MODE shadow; clear_halt; install_agent
    echo "SHADOW mode running in the background (no orders). Log: tail -f $LOG" ;;
  live)
    need_setup
    run_py -m server.arb_trader --check
    echo
    read -r -p "Start LIVE arbitrage on this Mac (up to $(grep '^ARB_MAX_CONTRACTS=' "$ENVF" | cut -d= -f2) contracts per pair, daily stop \$$(grep '^ARB_DAILY_STOP=' "$ENVF" | cut -d= -f2))? Type LIVE: " ans
    [ "$ans" = "LIVE" ] || { echo "Not started."; exit 1; }
    setenv ARB_MODE live; clear_halt; install_agent
    echo "LIVE. Keep this Mac plugged in and online. Watch: tail -f $LOG    Stop: bash deploy/mac_arb.sh off" ;;
  status)
    need_setup
    if agent_running; then echo "trader service: running"; else echo "trader service: NOT running"; fi
    [ -f "$LOG" ] && grep -E "arb |HALT|starting|failed|could not" "$LOG" | tail -12 || true
    run_py -m server.arb_trader --summary ;;
  off)
    stop_agent
    if [ -f "$ENVF" ]; then setenv ARB_MODE off; fi
    mkdir -p "$APP/data"; touch "$APP/data/STOP"
    echo "Arbitrage trading stopped." ;;
  *) echo "usage: bash deploy/mac_arb.sh setup|check|shadow|live|status|off"; exit 1 ;;
esac
