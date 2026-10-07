#!/bin/bash
# Usage: ./install.sh            install or update (safe to re-run)
#        ./install.sh --uninstall
set -euo pipefail

REPO="$(cd "$(dirname "$0")" && pwd)"
HUD="$REPO/hud.py"
HOOK="$REPO/hooks/hud-event.sh"
SETTINGS="$HOME/.claude/settings.json"
COMMAND="$HOME/.claude/commands/done.md"
PREFIX="${HUD_LABEL_PREFIX:-io.github.anderssonjohan.cc-hud}"
EVENTS='["SessionStart","UserPromptSubmit","Notification","PermissionRequest","Stop","SessionEnd","PostToolUse"]'

for bin in uv jq sqlite3 claude; do
  command -v "$bin" >/dev/null || { echo "missing: $bin" >&2; exit 1; }
done

remove_hooks() {
  [ -f "$SETTINGS" ] || return 0
  jq --arg cmd "$HOOK" '
    if .hooks then .hooks |= with_entries(
      .value |= map(select(.hooks | all(.command != $cmd))) | select(.value | length > 0))
    else . end' "$SETTINGS" > "$SETTINGS.tmp" && mv "$SETTINGS.tmp" "$SETTINGS"
}

add_hooks() {
  [ -f "$SETTINGS" ] || echo '{}' > "$SETTINGS"
  cp "$SETTINGS" "$SETTINGS.bak-cc-hud"
  remove_hooks
  jq --arg cmd "$HOOK" --argjson events "$EVENTS" '
    reduce $events[] as $e (.;
      .hooks[$e] = ((.hooks[$e] // []) + [{"hooks": [{"type": "command", "command": $cmd, "timeout": 3}]}]))' \
    "$SETTINGS" > "$SETTINGS.tmp" && mv "$SETTINGS.tmp" "$SETTINGS"
}

agent() { # name, args, schedule xml
  local label="$PREFIX.$1" dest="$HOME/Library/LaunchAgents/$PREFIX.$1.plist"
  launchctl bootout "gui/$UID/$label" 2>/dev/null || true
  cat > "$dest" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$label</string>
  <key>ProgramArguments</key>
  <array><string>$HUD</string><string>$2</string></array>
  <key>EnvironmentVariables</key>
  <dict><key>PATH</key><string>$(dirname "$(command -v uv)"):$(dirname "$(command -v claude)")$(command -v gh >/dev/null && echo ":$(dirname "$(command -v gh)")"):/usr/bin:/bin</string></dict>
  <key>StandardErrorPath</key><string>$HOME/.claude/hud/$1.log</string>
$3
</dict>
</plist>
EOF
  launchctl bootstrap "gui/$UID" "$dest"
}

if [ "${1:-}" = "--uninstall" ]; then
  remove_hooks
  for name in server snapshot digest; do
    launchctl bootout "gui/$UID/$PREFIX.$name" 2>/dev/null || true
    rm -f "$HOME/Library/LaunchAgents/$PREFIX.$name.plist"
  done
  grep -q "$HUD" "$COMMAND" 2>/dev/null && rm -f "$COMMAND"
  echo "Uninstalled. Your data is still in ~/.claude/hud/."
  exit 0
fi

mkdir -p "$HOME/.claude/hud" "$HOME/.claude/commands"
first_run=false
[ -f "$HOME/.claude/hud/hud.db" ] || first_run=true

"$HUD" ls >/dev/null
if $first_run; then
  "$HUD" backfill --days "${HUD_BACKFILL_DAYS:-30}"
fi

add_hooks

if [ -f "$COMMAND" ] && ! grep -q "$HUD" "$COMMAND"; then
  echo "skipped /done: $COMMAND already exists and isn't ours" >&2
else
  sed "s#__HUD__#$HUD#g" "$REPO/commands/done.md" > "$COMMAND"
fi

agent server serve '  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>'
agent snapshot snapshot '  <key>RunAtLoad</key><true/>
  <key>StartInterval</key><integer>60</integer>'
agent digest digest '  <key>StartCalendarInterval</key>
  <array>
    <dict><key>Hour</key><integer>9</integer><key>Minute</key><integer>0</integer></dict>
    <dict><key>Hour</key><integer>13</integer><key>Minute</key><integer>30</integer></dict>
  </array>'

echo "Installed. Board: http://localhost:7777"
echo "Menu bar: point SwiftBar's plugin folder at $REPO/swiftbar"
