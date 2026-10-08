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
# /clear hands the work on to a new session in the same tab: it joins the thread of the one that just ended there
# and carries its note and PR status, so the card keeps its place on the board.
| (if $e == "SessionStart" and .source == "clear" and .cwd != null then
     "FROM items p WHERE p.cwd = \(.cwd | q) AND p.state = 'cleared' AND p.session_id != \(.session_id | q)
        AND p.last_activity_at >= unixepoch() - 30"
   else null end) as $predecessor
| "INSERT INTO items (session_id, cwd, transcript_path, live, live_detail, first_prompt, last_prompt, gh_ref, gh_url, state, created_at, last_activity_at)
   VALUES (\(.session_id | q), \(.cwd | q), \(.transcript_path | q), \($live | q), \($detail | q), \($prompt | q), \($prompt | q),
           \(if $ref then ($ref.captures[0].string + "#" + $ref.captures[2].string) else null end | q), \(if $ref then ("https://" + $ref.string) else null end | q),
           \(if $cleared then "'cleared'" else "'open'" end), unixepoch(), unixepoch())
   ON CONFLICT (session_id) DO UPDATE SET
     live = excluded.live,
     live_detail = excluded.live_detail,
     cwd = coalesce(excluded.cwd, cwd),
     transcript_path = coalesce(excluded.transcript_path, transcript_path),
     first_prompt = coalesce(first_prompt, excluded.first_prompt),
     last_prompt = coalesce(excluded.last_prompt, last_prompt),
     gh_ref = CASE WHEN gh_scanned = 2 THEN gh_ref ELSE coalesce(gh_ref, excluded.gh_ref) END,
     gh_url = CASE WHEN gh_scanned = 2 THEN gh_url ELSE coalesce(gh_url, excluded.gh_url) END,
     last_activity_at = excluded.last_activity_at,
     state = CASE WHEN \($e == "UserPromptSubmit") AND state != 'open' THEN 'open'
                  WHEN \($cleared) AND state != 'done' THEN 'cleared' ELSE state END,
     done_at = CASE WHEN \($e == "UserPromptSubmit") THEN NULL ELSE done_at END;"
  + if $predecessor == null then "" else "
   UPDATE items SET (thread_id, note, gh_ref, gh_url, gh_login, gh_scanned, gh_checked_at, pr_ref, pr_state, pr_rollup,
                     pr_event, pr_event_at, pr_alert, pr_checked_at) =
     (SELECT coalesce(p.thread_id, p.session_id), p.note, p.gh_ref, p.gh_url, p.gh_login,
             CASE WHEN p.gh_scanned = 2 THEN 2 END, p.gh_checked_at, p.pr_ref, p.pr_state, p.pr_rollup, p.pr_event,
             p.pr_event_at, p.pr_alert, p.pr_checked_at
      \($predecessor) ORDER BY p.last_activity_at DESC LIMIT 1)
   WHERE session_id = \(.session_id | q) AND thread_id IS NULL AND EXISTS (SELECT 1 \($predecessor));" end
