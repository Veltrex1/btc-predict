#!/bin/bash
# One-time Mac setup: installs the background server (launchd) and the SwiftBar menu-bar widget.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
UV="$(command -v uv || true)"
[ -n "$UV" ] || { echo "Install uv first: curl -LsSf https://astral.sh/uv/install.sh | sh"; exit 1; }
mkdir -p "$REPO/data"
(cd "$REPO" && "$UV" sync)

PLIST="$HOME/Library/LaunchAgents/com.btcpredict.server.plist"
sed -e "s#__REPO__#$REPO#g" -e "s#__UV__#$UV#g" "$REPO/server/launchd/com.btcpredict.server.plist" > "$PLIST"
launchctl bootout "gui/$(id -u)/com.btcpredict.server" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "server installed: http://127.0.0.1:8765/health"

if ! [ -d "/Applications/SwiftBar.app" ]; then
  if command -v brew >/dev/null; then brew install --cask swiftbar; else
    echo "Install SwiftBar from https://swiftbar.app (or: brew install --cask swiftbar)"; fi
fi
PLUGINS="$(defaults read com.ameba.SwiftBar PluginDirectory 2>/dev/null || echo "$HOME/SwiftBarPlugins")"
mkdir -p "$PLUGINS"
ln -sf "$REPO/widget/btc-kalshi.2s.sh" "$PLUGINS/btc-kalshi.2s.sh"
echo "widget linked into $PLUGINS (choose that folder as SwiftBar's plugin folder on first launch)"
open -a SwiftBar || true
