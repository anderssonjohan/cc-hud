def q: if . == null or . == "" then "NULL" else "'" + (tostring | gsub("'"; "''")) + "'" end;

(.hook_event_name // "") as $e
| {
    "SessionStart": "idle",
    "UserPromptSubmit": "working",
    "PostToolUse": "working",
    "Notification": "waiting",
    "PermissionRequest": "waiting",
    "Stop": "idle",
    "SessionEnd": "detached"
  }[$e] as $base
# A subagent's or parallel tool's completion must not hide a permission prompt the main thread is waiting on.
| select($base != null and .session_id != null and ($e != "PostToolUse" or .agent_id == null))
| (if $e == "Notification" and .notification_type == "idle_prompt" then "idle" else $base end) as $live
| ($e == "SessionEnd" and .reason == "clear") as $cleared
| (if $e == "UserPromptSubmit" then (.prompt // "" | tostring | gsub("\\s+"; " ") | gsub("^ | $"; "") | .[0:300]) else null end) as $prompt
| (if $e == "UserPromptSubmit" then (.prompt // "" | tostring | [match("github\\.com/([\\w.-]+/[\\w.-]+)/(pull|issues)/([0-9]+)")] | first // null) else null end) as $ref
| (if $e == "Notification" then .message elif $e == "PermissionRequest" then "permission: " + (.tool_name // "tool") else null end) as $detail
| "INSERT INTO items (session_id, cwd, transcript_path, live, live_detail, first_prompt, last_prompt, gh_ref, gh_url, state, created_at, last_activity_at)
   VALUES (\(.session_id | q), \(.cwd | q), \(.transcript_path | q), \($live | q), \($detail | q), \($prompt | q), \($prompt | q),
           \(if $ref then ($ref.captures[0].string + "#" + $ref.captures[2].string) else null end | q), \(if $ref then ("https://" + $ref.string) else null end | q),
           \(if $cleared then "'done'" else "'open'" end), unixepoch(), unixepoch())
   ON CONFLICT (session_id) DO UPDATE SET
     live = excluded.live,
     live_detail = excluded.live_detail,
     cwd = coalesce(excluded.cwd, cwd),
     transcript_path = coalesce(excluded.transcript_path, transcript_path),
     first_prompt = coalesce(first_prompt, excluded.first_prompt),
     last_prompt = coalesce(excluded.last_prompt, last_prompt),
     gh_ref = coalesce(gh_ref, excluded.gh_ref),
     gh_url = coalesce(gh_url, excluded.gh_url),
     last_activity_at = excluded.last_activity_at,
     state = CASE WHEN \($e == "UserPromptSubmit") AND state != 'open' THEN 'open'
                  WHEN \($cleared) THEN 'done' ELSE state END,
     done_at = CASE WHEN \($e == "UserPromptSubmit") THEN NULL
                    WHEN \($cleared) THEN unixepoch() ELSE done_at END;"
