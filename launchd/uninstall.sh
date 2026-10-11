#!/usr/bin/env bash
# Remove the daily-search LaunchAgent.
set -euo pipefail
LABEL="com.job-harness.daily-search"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
rm -f "$HOME/Library/LaunchAgents/$LABEL.plist"
echo "Removed $LABEL"
