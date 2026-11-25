#!/bin/bash
# Saves an Anthropic API key in your login Keychain, so Claude writes the Natural translation,
# the notes on what lines mean and the Story. The lyrics of each song are then sent to Anthropic.
#   tools/claude-key.sh           save or replace the key
#   tools/claude-key.sh remove    stop using Claude; the model on this Mac writes again
set -euo pipefail
SERVICE="Desk Music Companion"
ACCOUNT="anthropic-api-key"
restart_server() {
  launchctl kickstart -k "gui/$(id -u)/com.desklyrics.server" 2>/dev/null || true
}

if [ "${1:-}" = "remove" ]; then
  if security delete-generic-password -s "$SERVICE" -a "$ACCOUNT" >/dev/null 2>&1; then
    restart_server
    echo "Removed. Songs are written on this Mac again."
  else
    echo "No key was saved."
  fi
  exit 0
fi

printf "Paste your Anthropic API key (it won't show), then press Return: "
read -rs KEY
echo
KEY="$(printf '%s' "$KEY" | tr -d '[:space:]')"
case "$KEY" in
  sk-ant-*) ;;
  *) echo "That doesn't look like an Anthropic API key. They start with sk-ant-."; exit 1 ;;
esac

# Check it before saving. Listing models is free. The key goes in on stdin, so it never shows in the process list.
STATUS="$(printf 'x-api-key: %s\n' "$KEY" | curl -s -o /dev/null -w '%{http_code}' -H @- \
  -H "anthropic-version: 2026-10-21" https://api.anthropic.com/v1/models || true)"
case "$STATUS" in
  200) ;;
  401|403) echo "Anthropic didn't accept that key. Check it in the Claude Console at https://platform.claude.com"; exit 1 ;;
  *) echo "Couldn't reach Anthropic to check the key (got '${STATUS:-nothing}'). Saving it anyway." ;;
esac

printf 'add-generic-password -U -s "%s" -a "%s" -w "%s"\n' "$SERVICE" "$ACCOUNT" "$KEY" | security -i >/dev/null
restart_server
echo "Saved in your Keychain. Claude now writes the Natural translation, the meaning notes and the Story."
echo "Songs already written on this Mac are written again by Claude the next time they play."
