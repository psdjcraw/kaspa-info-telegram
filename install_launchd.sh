#!/usr/bin/env zsh
set -euo pipefail

cd "$(dirname "$0")"

LABEL="com.psdjcraw.kaspa-info-telegram"
DOMAIN="gui/$(id -u)"
SOURCE_PLIST="$PWD/launchd/$LABEL.plist"
TARGET_DIR="$HOME/Library/LaunchAgents"
TARGET_PLIST="$TARGET_DIR/$LABEL.plist"

mkdir -p "$TARGET_DIR"
cp "$SOURCE_PLIST" "$TARGET_PLIST"

launchctl bootout "$DOMAIN" "$TARGET_PLIST" 2>/dev/null || true
launchctl bootstrap "$DOMAIN" "$TARGET_PLIST"
launchctl enable "$DOMAIN/$LABEL"
launchctl kickstart -k "$DOMAIN/$LABEL"

echo "installed $LABEL"
launchctl print "$DOMAIN/$LABEL" | sed -n '1,40p'
