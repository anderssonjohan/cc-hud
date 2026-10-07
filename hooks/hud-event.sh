#!/bin/bash
# Claude Code hook for every session lifecycle event. Records live state in the
# hud DB. Must stay fast and must never fail the session: every path exits 0.

DB="${HUD_DB:-$HOME/.claude/hud/hud.db}"
[ -f "$DB" ] || exit 0

sql="$(jq -r -f "$(dirname "$0")/hud-event.jq" 2>/dev/null)"
[ -n "$sql" ] && sqlite3 -cmd '.timeout 2000' "$DB" "$sql" >/dev/null 2>&1
exit 0
