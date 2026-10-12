#!/bin/bash
# <xbar.title>Kalshi BTC 15m signal</xbar.title>
# <xbar.desc>ABOVE / BELOW call and live bot P&L for Kalshi's 15-minute BTC market.</xbar.desc>
# <swiftbar.hideAbout>true</swiftbar.hideAbout>
# <swiftbar.hideRunInTerminal>true</swiftbar.hideRunInTerminal>
# <swiftbar.hideDisablePlugin>true</swiftbar.hideDisablePlugin>
#
# SwiftBar runs this every 2 seconds (the ".2s." in the filename). All logic lives
# in the server; this just prints what /swiftbar returns.
# To use a remote server (e.g. the DigitalOcean droplet), create ~/.btc-predict with:
#   BTC_SERVER_URL=https://<your-ip>.sslip.io
#   BTC_API_TOKEN=<token printed by deploy/setup_droplet.sh>
BTC_SERVER_URL="http://127.0.0.1:8765"
BTC_API_TOKEN=""
[ -f "$HOME/.btc-predict" ] && . "$HOME/.btc-predict"
out=$(curl -s --max-time 1.8 -H "Authorization: Bearer $BTC_API_TOKEN" "$BTC_SERVER_URL/swiftbar")
if [ -z "$out" ] || [[ "$out" == \{* ]]; then
  echo "₿ offline | color=#a3a3a3 font=Menlo size=12"
  echo "---"
  echo "Can't reach $BTC_SERVER_URL"
  [[ "$out" == *unauthorized* ]] && echo "Token rejected: check BTC_API_TOKEN in ~/.btc-predict"
  exit 0
fi
echo "$out"
