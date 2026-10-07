#!/bin/bash
# Runs real hook payloads through hooks/hud-event.sh against a scratch database and checks the rows it leaves.
set -euo pipefail
cd "$(dirname "$0")/.."

HUD_DB="$(mktemp -d)/hud.db"
export HUD_DB
./hud.py ls >/dev/null

fire() { printf '%s' "$1" | ./hooks/hud-event.sh; }
row() { sqlite3 "$HUD_DB" "SELECT $1 FROM items WHERE session_id = 's1'"; }
check() { # what, expected, actual
  if [ "$2" != "$3" ]; then
    echo "::error::$1: expected '$2', got '$3'"
    exit 1
  fi
  echo "ok: $1"
}

fire '{"session_id":"s1","cwd":"/tmp/w","hook_event_name":"UserPromptSubmit","prompt":"  see https://github.com/acme/web/pull/7 for O'"'"'Brien  "}'
check "a prompt sets working" working "$(row live)"
check "the PR link in a prompt is picked up" "acme/web#7" "$(row gh_ref)"
check "quotes survive and whitespace is trimmed" "see https://github.com/acme/web/pull/7 for O'Brien" "$(row first_prompt)"

fire '{"session_id":"s1","hook_event_name":"PermissionRequest","tool_name":"Bash"}'
check "a permission request waits" "waiting|permission: Bash" "$(row "live || '|' || live_detail")"

fire '{"session_id":"s1","hook_event_name":"PostToolUse","agent_id":"x1"}'
check "a subagent's tool use keeps the prompt visible" waiting "$(row live)"

fire '{"session_id":"s1","hook_event_name":"Notification","notification_type":"idle_prompt","message":"waiting for input"}'
check "an idle reminder is idle, not waiting" idle "$(row live)"

fire '{"session_id":"s1","hook_event_name":"SessionEnd","reason":"clear"}'
check "/clear closes the old session" "detached|done" "$(row "live || '|' || state")"

fire '{"session_id":"s1","hook_event_name":"UserPromptSubmit","prompt":"back again"}'
check "typing reopens it" "open|1" "$(row "state || '|' || (done_at IS NULL)")"

fire '{"session_id":"s1","hook_event_name":"SessionEnd","reason":"prompt_input_exit"}'
check "closing the tab only detaches" "detached|open" "$(row "live || '|' || state")"

fire '{}'
fire '{"hook_event_name":"Stop"}'
check "payloads without a session add nothing" 1 "$(sqlite3 "$HUD_DB" "SELECT count(*) FROM items")"
