#!/bin/bash
# Stop trading immediately: sells any open position and halts, then switches the service back to shadow.
set -euo pipefail
ETC=/etc/btc-predict; APP=/opt/btc-predict
TOKEN=$(grep ^BTC_API_TOKEN= "$ETC/env" | cut -d= -f2)
curl -fsS -X POST -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8765/trading/stop >/dev/null && echo "trading halted"
sed -i 's/^TRADER_MODE=.*/TRADER_MODE=shadow/' "$ETC/env"
echo "Service left running (halted). To shut it down completely: systemctl stop btc-predict"
