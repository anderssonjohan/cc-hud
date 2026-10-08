#!/bin/bash
# Runs real hook payloads through hooks/hud-event.sh against a scratch database and checks the rows it leaves.
set -euo pipefail
cd "$(dirname "$0")/.."

HUD_DB="$(mktemp -d)/hud.db"
export HUD_DB
./hud.py ls >/dev/null

fire() { printf '%s' "$1" | ./hooks/hud-event.sh; }
row() { sqlite3 "$HUD_DB" "SELECT $1 FROM items WHERE session_id = '${2:-s1}'"; }
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
check "/clear hands the old session on instead of closing it" "detached|cleared" "$(row "live || '|' || state")"

fire '{"session_id":"s1","hook_event_name":"UserPromptSubmit","prompt":"back again"}'
check "typing reopens it" "open|1" "$(row "state || '|' || (done_at IS NULL)")"

fire '{"session_id":"s1","hook_event_name":"SessionEnd","reason":"prompt_input_exit"}'
check "closing the tab only detaches" "detached|open" "$(row "live || '|' || state")"

fire '{}'
fire '{"hook_event_name":"Stop"}'
check "payloads without a session add nothing" 1 "$(sqlite3 "$HUD_DB" "SELECT count(*) FROM items")"

fire '{"session_id":"s2","cwd":"/tmp/w","hook_event_name":"UserPromptSubmit","prompt":"ship https://github.com/acme/web/pull/9"}'
./hud.py note "after the release, bump the app" -s s2
./hud.py step add "Trigger the downstream release" -s s2 >/dev/null
fire '{"session_id":"s2","cwd":"/tmp/w","hook_event_name":"SessionEnd","reason":"clear"}'
fire '{"session_id":"s3","cwd":"/tmp/w","hook_event_name":"SessionStart","source":"clear"}'
check "the session after /clear continues the thread" "s2" "$(row thread_id s3)"
check "it carries the note and the PR" "after the release, bump the app|acme/web#9" "$(row "note || '|' || gh_ref" s3)"
check "the thread's steps come along" "1. [ ] Trigger the downstream release" "$(./hud.py step ls -s s3 | head -1)"

fire '{"session_id":"s3","cwd":"/tmp/w","hook_event_name":"SessionEnd","reason":"clear"}'
fire '{"session_id":"s4","cwd":"/tmp/w","hook_event_name":"SessionStart","source":"clear"}'
check "a second /clear stays in the first session's thread" "s2" "$(row thread_id s4)"

fire '{"session_id":"s5","cwd":"/tmp/elsewhere","hook_event_name":"SessionStart","source":"clear"}'
check "a /clear in another folder starts its own thread" "" "$(row thread_id s5)"

fire '{"session_id":"s6","cwd":"/tmp/v","hook_event_name":"SessionStart","source":"startup"}'
./hud.py "done" s6 >/dev/null
fire '{"session_id":"s6","cwd":"/tmp/v","hook_event_name":"SessionEnd","reason":"clear"}'
fire '{"session_id":"s7","cwd":"/tmp/v","hook_event_name":"SessionStart","source":"clear"}'
check "/done before /clear stays done and starts nothing" "done|" "$(row state s6)|$(row thread_id s7)"

fire '{"session_id":"r1","cwd":"/tmp/u","hook_event_name":"UserPromptSubmit","prompt":"I usually type: review https://github.com/acme/web/pull/7/changes"}'
sqlite3 "$HUD_DB" "UPDATE items SET gh_login = 'someone', gh_checked_at = 1, pr_event = 'ci passed', pr_checked_at = 1 WHERE session_id = 'r1'"
./hud.py ref none -s r1 >/dev/null
check "ref none drops the guessed PR, its face and its status" "||||2" \
  "$(row "coalesce(gh_ref, '') || '|' || coalesce(gh_url, '') || '|' || coalesce(gh_login, '') || '|' || coalesce(pr_event, '') || '|' || gh_scanned" r1)"
fire '{"session_id":"r1","cwd":"/tmp/u","hook_event_name":"UserPromptSubmit","prompt":"and https://github.com/acme/web/issues/8"}'
check "a later link does not override a ref set by hand" "" "$(row gh_ref r1)"

./hud.py ref https://github.com/acme/api/pull/12/changes -s r1 >/dev/null
check "ref takes a PR URL" "acme/api#12|https://github.com/acme/api/pull/12|1" "$(row "gh_ref || '|' || gh_url || '|' || (gh_checked_at IS NULL)" r1)"
./hud.py ref acme/api#13 -s r1 >/dev/null
check "ref takes owner/repo#N" "acme/api#13|" "$(row "gh_ref || '|' || coalesce(gh_url, '')" r1)"
if ./hud.py ref "not a ref" -s r1 2>/dev/null; then
  echo "::error::ref accepted garbage"
  exit 1
fi
check "a bad ref changes nothing" "acme/api#13" "$(row gh_ref r1)"

fire '{"session_id":"r1","cwd":"/tmp/u","hook_event_name":"SessionEnd","reason":"clear"}'
fire '{"session_id":"r2","cwd":"/tmp/u","hook_event_name":"SessionStart","source":"clear"}'
fire '{"session_id":"r2","cwd":"/tmp/u","hook_event_name":"UserPromptSubmit","prompt":"now https://github.com/acme/web/pull/9"}'
check "a ref set by hand survives /clear" "acme/api#13|2" "$(row "gh_ref || '|' || gh_scanned" r2)"

fire '{"session_id":"s2","cwd":"/tmp/w","hook_event_name":"UserPromptSubmit","prompt":"also https://github.com/acme/web/pull/99"}'
fire '{"session_id":"s2","cwd":"/tmp/w","hook_event_name":"SessionEnd","reason":"clear"}'
fire '{"session_id":"s8","cwd":"/tmp/w","hook_event_name":"SessionStart","source":"clear"}'
check "a guessed ref does not pin the session after /clear" "acme/web#9|" "$(row "gh_ref || '|' || coalesce(gh_scanned, '')" s8)"
