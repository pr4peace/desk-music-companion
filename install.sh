#!/bin/bash
# Sets up Desk Music Companion on this Mac: builds the helpers, starts the server now and at
# every login, and puts the menu bar helper in ~/Applications. Safe to run again.
set -euo pipefail
cd "$(dirname "$0")"
FOLDER="$(pwd)"

command -v swiftc >/dev/null || { echo "Swift is missing. Run: xcode-select --install"; exit 1; }
PYTHON="$(command -v python3 || true)"
[ -n "$PYTHON" ] || { echo "Python 3 is missing. Run: brew install python"; exit 1; }
if ! command -v media-control >/dev/null; then
  echo "Installing media-control, which reports what the Mac is playing..."
  brew install media-control
fi
BREW_BIN="$(dirname "$(command -v media-control)")"

echo "Building the audio helper..."
swiftc -O -o visualizer/audio_levels visualizer/audio_levels.swift \
  -Xlinker -sectcreate -Xlinker __TEXT -Xlinker __info_plist -Xlinker visualizer/Info.plist

echo "Building the menu bar helper..."
APP="$HOME/Applications/Desk Music Companion.app"
pkill -x DeskLyricsMenu 2>/dev/null || true
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
swiftc -O -parse-as-library -o "$APP/Contents/MacOS/DeskLyricsMenu" mac/DeskLyricsMenu.swift
cp mac/Info.plist "$APP/Contents/Info.plist"
cp mac/AppIcon.icns "$APP/Contents/Resources/AppIcon.icns"
codesign --force --sign - "$APP" >/dev/null 2>&1

AGENTS="$HOME/Library/LaunchAgents"
mkdir -p "$AGENTS" "$HOME/Library/Logs"
SERVER_PLIST="$AGENTS/com.desklyrics.server.plist"
LOG="$HOME/Library/Logs/DeskLyrics.log"
cat > "$SERVER_PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>Label</key><string>com.desklyrics.server</string>
	<key>ProgramArguments</key>
	<array><string>$PYTHON</string><string>server.py</string></array>
	<key>WorkingDirectory</key><string>$FOLDER</string>
	<key>EnvironmentVariables</key>
	<dict><key>PATH</key><string>$BREW_BIN:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string></dict>
	<key>RunAtLoad</key><true/>
	<key>KeepAlive</key><true/>
	<key>ThrottleInterval</key><integer>10</integer>
	<key>ProcessType</key><string>Interactive</string>
	<key>StandardOutPath</key><string>$LOG</string>
	<key>StandardErrorPath</key><string>$LOG</string>
</dict>
</plist>
PLIST
cat > "$AGENTS/com.desklyrics.menu.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>Label</key><string>com.desklyrics.menu</string>
	<key>ProgramArguments</key>
	<array><string>$APP/Contents/MacOS/DeskLyricsMenu</string></array>
	<key>RunAtLoad</key><true/>
	<key>LimitLoadToSessionType</key><string>Aqua</string>
</dict>
</plist>
PLIST

SESSION="gui/$(id -u)"
launchctl bootout "$SESSION/com.desklyrics.server" 2>/dev/null || true
sleep 1
launchctl bootstrap "$SESSION" "$SERVER_PLIST"
open "$APP"

NAME="$(scutil --get LocalHostName)"
echo
echo "Desk Music Companion is running. Look for its ring in the menu bar."
echo "On your iPhone, use: http://$NAME.local:8787"
