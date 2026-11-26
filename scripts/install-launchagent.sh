#!/bin/bash
# Install PkgWatch as a per-user LaunchAgent: starts at login, restarts if it crashes,
# runs independently of any terminal or Claude Code session.
set -euo pipefail

LABEL="com.pkgwatch.agent"
PROJECT="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON="${PYTHON:-$(command -v python3)}"
PORT="${PKGWATCH_PORT:-8787}"
DATA="$HOME/Library/Application Support/PkgWatch"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

"$PYTHON" -c 'import sys; assert sys.version_info >= (3, 10), "Python 3.10+ required"'
mkdir -p "$DATA" "$HOME/Library/LaunchAgents"

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PYTHON</string><string>-m</string><string>pkgwatch</string>
    <string>serve</string><string>--port</string><string>$PORT</string>
  </array>
  <key>WorkingDirectory</key><string>$PROJECT</string>
  <key>EnvironmentVariables</key>
  <dict><key>PYTHONUNBUFFERED</key><string>1</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>ProcessType</key><string>Interactive</string>
  <key>StandardOutPath</key><string>$DATA/launchd.out.log</string>
  <key>StandardErrorPath</key><string>$DATA/launchd.err.log</string>
</dict>
</plist>
EOF

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
launchctl enable "gui/$(id -u)/$LABEL"

for _ in $(seq 1 20); do
  curl -fs -o /dev/null "http://127.0.0.1:$PORT/" && break
  sleep 0.5
done
echo "PkgWatch installed and running: http://127.0.0.1:$PORT/"
echo "Logs: $DATA/pkgwatch.log   Events: $DATA/events.jsonl"
