#!/bin/bash
# Stop and remove the PkgWatch LaunchAgent. Keeps your data, logs and Keychain entries.
set -euo pipefail
LABEL="com.pkgwatch.agent"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
rm -f "$HOME/Library/LaunchAgents/$LABEL.plist"
echo "PkgWatch LaunchAgent removed. Data kept in ~/Library/Application Support/PkgWatch"
