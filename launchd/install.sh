#!/usr/bin/env bash
# Install (or reinstall) the daily-search LaunchAgent for the current user.
# Loading it runs the workflow once right away (RunAtLoad); the guard makes
# that a no-op if a run already succeeded since the last 19:00.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON="$(command -v python3)"
LABEL="com.job-harness.daily-search"
DEST="$HOME/Library/LaunchAgents/$LABEL.plist"

mkdir -p "$REPO/workflows/daily_search/logs" "$HOME/Library/LaunchAgents"
sed -e "s|__REPO__|$REPO|g" -e "s|__PYTHON__|$PYTHON|g" -e "s|__LABEL__|$LABEL|g" \
  "$REPO/launchd/daily-search.plist.template" > "$DEST"
plutil -lint "$DEST" >/dev/null

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$DEST"
echo "Installed $LABEL -> $DEST (python: $PYTHON)"
echo "Run now:  launchctl kickstart gui/$(id -u)/$LABEL"
echo "Output:   $REPO/workflows/daily_search/logs/launchd.{out,err}.log"
