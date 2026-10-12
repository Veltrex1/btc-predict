#!/bin/bash
# One-time history + first model on the droplet (~1-2 hours). Run as root; started by setup_droplet.sh.
# Progress: tail -f /opt/btc-predict/data/bootstrap.log
set -euo pipefail
APP=/opt/btc-predict; UV=/home/btc/.local/bin/uv
run() { sudo -u btc -H bash -c "cd $APP && $UV run python -m $*"; }
echo "[$(date -u +%T)] Coinbase 1m history (3 years)"; run pipeline.backfill_coinbase --days 1095
echo "[$(date -u +%T)] Kalshi markets + quotes";        run pipeline.backfill_kalshi --candles --hist-every 6
echo "[$(date -u +%T)] training";                       run pipeline.train --years 3 --test-months 11
systemctl restart btc-predict
echo "[$(date -u +%T)] BOOTSTRAP DONE"
