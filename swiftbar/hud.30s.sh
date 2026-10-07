#!/bin/bash
# <swiftbar.hideAbout>true</swiftbar.hideAbout>
# <swiftbar.hideRunInTerminal>true</swiftbar.hideRunInTerminal>
# <swiftbar.hideLastUpdated>true</swiftbar.hideLastUpdated>
# <swiftbar.hideDisablePlugin>true</swiftbar.hideDisablePlugin>

export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
DB="${HUD_DB:-$HOME/.claude/hud/hud.db}"
HUD="$(cd "$(dirname "$0")/.." && pwd)/hud.py"

# Menu clicks run this script again, so hud.py gets the PATH above instead of SwiftBar's minimal one.
if [ "${1:-}" = go ] && [[ "${2:-}" =~ ^[0-9a-f-]+$ ]]; then
  exec "$HUD" go "$2"
fi
q() { sqlite3 -separator $'\x1f' -cmd '.timeout 2000' "$DB" "$1" 2>/dev/null; }

NEEDS="state = 'open' AND (live IN ('waiting', 'idle') OR (live = 'detached' AND pr_alert IS NOT NULL))"
needs="$(q "SELECT count(*) FROM items WHERE $NEEDS")"
waiting="$(q "SELECT count(*) FROM items WHERE state = 'open' AND live = 'waiting'")"
open="$(q "SELECT count(*) FROM items WHERE state = 'open'")"

if [ "${waiting:-0}" -gt 0 ]; then
  echo "$needs | sfimage=exclamationmark.bubble.fill sfcolor=#e19a2c"
elif [ "${needs:-0}" -gt 0 ]; then
  echo "$needs | sfimage=bubble.left"
else
  echo "$open | sfimage=checklist"
fi
echo "---"

flat() { echo "replace(replace(replace(replace($1, char(10), ' '), char(13), ' '), char(31), ' '), char(9), ' ')"; }

section() {
  local heading="$1" where="$2"
  local rows
  # Titles come from transcripts: flatten control characters so one can't start a new menu line with its own params.
  rows="$(q "SELECT session_id, $(flat "coalesce(name, ai_title, first_prompt, session_id)"), live,
                    $(flat "coalesce(live_detail, pr_event, '')"), (unixepoch() - last_activity_at),
                    coalesce(pr_alert, '')
             FROM items WHERE $where ORDER BY live = 'waiting' DESC, last_activity_at")"
  [ -n "$rows" ] || return
  echo "$heading"
  while IFS=$'\x1f' read -r sid title live detail secs alert; do
    [[ "$sid" =~ ^[0-9a-f-]+$ ]] || continue
    if [ "$secs" -ge 86400 ]; then a="$((secs / 86400))d"; elif [ "$secs" -ge 3600 ]; then a="$((secs / 3600))h"; else a="$((secs / 60))m"; fi
    icon="circle"
    case "$alert" in
      failed|changes) icon="xmark.octagon" ;;
      merged|closed) icon="checkmark.seal" ;;
    esac
    [ "$live" = detached ] || case "$live" in
      waiting) icon="exclamationmark.circle.fill" ;;
      idle) icon="arrowshape.turn.up.left" ;;
      working) icon="ellipsis.circle" ;;
    esac
    title="${title//|/-}"
    # SwiftBar reads a line starting with -- as a submenu item or separator.
    title="${title#"${title%%[!-]*}"}"
    [ ${#title} -gt 60 ] && title="${title:0:59}..."
    tip="${detail//[|\"]/-}"
    echo "$title  $a | sfimage=$icon bash=\"$0\" param1=go param2=$sid terminal=false refresh=true tooltip=\"${tip:-$live}\""
  done <<< "$rows"
  echo "---"
}

section "Needs you" "$NEEDS"
section "Working" "state = 'open' AND live = 'working'"
section "Open, no tab" "state = 'open' AND live = 'detached' AND pr_alert IS NULL"
echo "Open board | href=http://localhost:7777 sfimage=rectangle.split.3x1"
