#!/bin/bash
# Usage: ./install.sh            install or update (safe to re-run)
#        ./install.sh --uninstall
set -euo pipefail
umask 077

REPO="$(cd "$(dirname "$0")" && pwd)"
HUD="$REPO/hud.py"
HOOK="$REPO/hooks/hud-event.sh"
HUD_HOME="$HOME/.claude/hud"
SETTINGS="$HOME/.claude/settings.json"
COMMANDS="$HOME/.claude/commands"
PREFIX="${HUD_LABEL_PREFIX:-io.github.anderssonjohan.cc-hud}"
EVENTS='["SessionStart","UserPromptSubmit","Notification","PermissionRequest","Stop","SessionEnd","PostToolUse"]'

need() {
  for bin in "$@"; do
    command -v "$bin" >/dev/null || { echo "missing: $bin" >&2; exit 1; }
  done
}

# Rewrite settings.json in place rather than replacing it, so a symlink (dotfiles) and its permissions survive.
edit_settings() {
  local tmp
  tmp="$(mktemp)"
  if jq "$@" "$SETTINGS" > "$tmp"; then
    cat "$tmp" > "$SETTINGS"
    rm -f "$tmp"
  else
    rm -f "$tmp"
    echo "could not update $SETTINGS, left it unchanged" >&2
    exit 1
  fi
}

# Ours is any hook that runs a hooks/hud-event.sh, so a clone that moved since the last install is cleaned up too.
# Other hooks in the same group, and groups or events without our hook, are left alone.
remove_hooks() {
  [ -f "$SETTINGS" ] || return 0
  edit_settings '
    def ours: (.command // "") | endswith("/hooks/hud-event.sh");
    if (.hooks | type) == "object" then
      .hooks |= with_entries(
        if (.value | type) == "array" then
          .value |= map(
            if (.hooks | type) == "array" and any(.hooks[]; ours) then
              (.hooks |= map(select(ours | not))) | select(.hooks | length > 0)
            else . end)
        else . end)
    else . end'
}

add_hooks() {
  [ -f "$SETTINGS" ] || echo '{}' > "$SETTINGS"
  # Keep the copy from before the first install; later runs would only back up our own edits.
  [ -e "$SETTINGS.bak-cc-hud" ] || cp "$SETTINGS" "$SETTINGS.bak-cc-hud"
  remove_hooks
  local cmd
  cmd="$(printf '%q' "$HOOK")"
  [ -z "${HUD_DB:-}" ] || cmd="HUD_DB=$(printf '%q' "$HUD_DB") $cmd"
  # shellcheck disable=SC2016 # $e and $cmd are jq variables
  edit_settings --arg cmd "$cmd" --argjson events "$EVENTS" '
    reduce $events[] as $e (.;
      .hooks[$e] = ((.hooks[$e] // []) + [{"hooks": [{"type": "command", "command": $cmd, "timeout": 3}]}]))'
}

install_command() { # name
  local dest="$COMMANDS/$1.md"
  if [ -f "$dest" ] && ! grep -qF "hud.py" "$dest"; then
    echo "skipped /$1: $dest already exists and isn't ours" >&2
    return 0
  fi
  local tmp
  tmp="$(mktemp)"
  # awk with the path in the environment: sed and awk -v would both treat characters in it as special.
  HUD_PATH="$HUD" awk '{
    out = ""
    while ((i = index($0, "__HUD__")) > 0) { out = out substr($0, 1, i - 1) ENVIRON["HUD_PATH"]; $0 = substr($0, i + 7) }
    print out $0
  }' "$REPO/commands/$1.md" > "$tmp"
  mv "$tmp" "$dest"
}

unload() { # label
  launchctl bootout "gui/$UID/$1" 2>/dev/null || return 0
  # bootout returns before the job is gone, and bootstrapping it again too early fails with an I/O error.
  for _ in $(seq 40); do
    launchctl print "gui/$UID/$1" >/dev/null 2>&1 || return 0
    sleep 0.25
  done
}

agent() { # name, hud.py argument, extra plist keys as JSON
  local label="$PREFIX.$1" dest="$HOME/Library/LaunchAgents/$PREFIX.$1.plist" path=""
  for bin in uv claude gh; do
    command -v "$bin" >/dev/null && path="$path$(dirname "$(command -v "$bin")"):"
  done
  path="$path$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
  unload "$label"
  # Built as JSON and converted, so any character in a path ends up correctly escaped in the plist.
  jq -n --arg label "$label" --arg hud "$HUD" --arg arg "$2" --arg path "$path" --arg db "${HUD_DB:-}" \
    --arg log "$HUD_HOME/$1.log" --argjson extra "$3" '
    {
      Label: $label,
      ProgramArguments: [$hud, $arg],
      EnvironmentVariables: ({PATH: $path} + (if $db == "" then {} else {HUD_DB: $db} end)),
      StandardOutPath: $log,
      StandardErrorPath: $log
    } + $extra' | plutil -convert xml1 -o "$dest" -
  launchctl bootstrap "gui/$UID" "$dest"
}

if [ "${1:-}" = "--uninstall" ]; then
  need jq
  remove_hooks
  for name in server snapshot digest; do
    unload "$PREFIX.$name"
    rm -f "$HOME/Library/LaunchAgents/$PREFIX.$name.plist"
  done
  for name in "done" steps; do
    grep -qF "hud.py" "$COMMANDS/$name.md" 2>/dev/null && rm -f "$COMMANDS/$name.md"
  done
  echo "Uninstalled. Your data is still in $HUD_HOME, and your settings from before the first install in $SETTINGS.bak-cc-hud."
  exit 0
fi

need uv jq sqlite3 claude
mkdir -p "$HUD_HOME" "$COMMANDS"
chmod 700 "$HUD_HOME"

"$HUD" ls >/dev/null
add_hooks
install_command "done"
install_command steps

# Backfill until it has completed once; an interrupted first run is picked up again by the next install.
if [ ! -e "$HUD_HOME/.backfilled" ]; then
  "$HUD" backfill --days "${HUD_BACKFILL_DAYS:-30}"
  touch "$HUD_HOME/.backfilled"
fi

agent server serve '{"RunAtLoad": true, "KeepAlive": true, "ThrottleInterval": 30}'
agent snapshot snapshot '{"RunAtLoad": true, "StartInterval": 60}'
agent digest digest '{"StartCalendarInterval": [{"Hour": 9, "Minute": 0}, {"Hour": 13, "Minute": 30}]}'

echo "Installed. Board: http://localhost:7777"
echo "Menu bar: point SwiftBar's plugin folder at $REPO/swiftbar"
