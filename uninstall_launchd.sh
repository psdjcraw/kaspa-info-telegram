#!/usr/bin/env zsh
set -euo pipefail

LABEL="com.psdjcraw.kaspa-info-telegram"
DOMAIN="gui/$(id -u)"
TARGET_PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

launchctl bootout "$DOMAIN" "$TARGET_PLIST" 2>/dev/null || true
rm -f "$TARGET_PLIST"
echo "uninstalled $LABEL"
