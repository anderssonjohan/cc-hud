def q: if . == null or . == "" then "NULL" else "'" + (tostring | gsub("'"; "''")) + "'" end;

.hook_event_name as $e
| {
    "SessionStart": "idle",
    "UserPromptSubmit": "working",
    "PostToolUse": "working",
    "Notification": "waiting",
    "PermissionRequest": "waiting",
    "Stop": "idle",
    "SessionEnd": "detached"
  }[$e] as $live
| select($live != null and .session_id != null)
| (if $e == "UserPromptSubmit" then (.prompt // "" | tostring | gsub("\\s+"; " ") | .[0:300]) else null end) as $prompt
| (if $e == "UserPromptSubmit" then (.prompt // "" | tostring | [match("github\\.com/([\\w.-]+/[\\w.-]+)/(pull|issues)/([0-9]+)")] | first // null) else null end) as $ref
| (if $e == "Notification" then .message elif $e == "PermissionRequest" then "permission: " + (.tool_name // "tool") else null end) as $detail
| "INSERT INTO items (session_id, cwd, transcript_path, live, live_detail, first_prompt, last_prompt, gh_ref, gh_url, state, created_at, last_activity_at)
   VALUES (\(.session_id | q), \(.cwd | q), \(.transcript_path | q), \($live | q), \($detail | q), \($prompt | q), \($prompt | q),
           \(if $ref then ($ref.captures[0].string + "#" + $ref.captures[2].string) else null end | q), \(if $ref then ("https://" + $ref.string) else null end | q),
           'open', unixepoch(), unixepoch())
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
     state = CASE WHEN \($e == "UserPromptSubmit") AND state != 'open' THEN 'open' ELSE state END,
     done_at = CASE WHEN \($e == "UserPromptSubmit") THEN NULL ELSE done_at END;"
