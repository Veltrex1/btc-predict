#!/bin/bash
# <xbar.title>Kalshi BTC 15m signal</xbar.title>
# <xbar.desc>ABOVE / BELOW call for Kalshi's 15-minute BTC market, from the local btc-predict server.</xbar.desc>
# <swiftbar.hideAbout>true</swiftbar.hideAbout>
# <swiftbar.hideRunInTerminal>true</swiftbar.hideRunInTerminal>
# <swiftbar.hideDisablePlugin>true</swiftbar.hideDisablePlugin>
#
# SwiftBar runs this every 2 seconds (the ".2s." in the filename). All logic lives
# in the server; this just prints what /swiftbar returns.
out=$(curl -s --max-time 1.5 "http://127.0.0.1:8765/swiftbar")
if [ -z "$out" ]; then
  echo "₿ offline | color=#a3a3a3 font=Menlo size=12"
  echo "---"
  echo "btc-predict server is not running"
  echo "Start it: launchctl kickstart -k gui/$(id -u)/com.btcpredict.server"
  exit 0
fi
echo "$out"
