#!/usr/bin/env bash
# Installs deps into a local .venv and registers the launchd agent.
# Run once from the terminal after filling in .env.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLIST="$SCRIPT_DIR/de.inberlinwohnen.scraper.plist"
LAUNCH_AGENTS="$HOME/Library/LaunchAgents"

echo "→ Creating virtual environment…"
python3 -m venv "$SCRIPT_DIR/.venv"

echo "→ Installing dependencies…"
"$SCRIPT_DIR/.venv/bin/pip" install -q -r "$SCRIPT_DIR/requirements.txt"

echo "→ Copying plist to ~/Library/LaunchAgents/…"
mkdir -p "$LAUNCH_AGENTS"
cp "$PLIST" "$LAUNCH_AGENTS/"

echo "→ Loading launchd agent…"
launchctl unload "$LAUNCH_AGENTS/de.inberlinwohnen.scraper.plist" 2>/dev/null || true
launchctl load -w "$LAUNCH_AGENTS/de.inberlinwohnen.scraper.plist"

echo ""
echo "Done. Agent is loaded and will run every 5 minutes."
echo "Logs: $SCRIPT_DIR/launchd.log and $SCRIPT_DIR/scraper.log"
echo ""
echo "To uninstall: launchctl unload ~/Library/LaunchAgents/de.inberlinwohnen.scraper.plist"
