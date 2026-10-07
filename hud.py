#!/usr/bin/env -S uv run --no-project --python >=3.12 --script
"""Ledger of Claude Code sessions as open loops: what needs me, what's detached, what's done."""

import argparse
import json
import os
import shlex
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

DB_PATH = Path(os.environ.get("HUD_DB", Path.home() / ".claude/hud/hud.db"))
PROJECTS = Path.home() / ".claude/projects"
LIVE_STATES = ("working", "waiting", "idle")

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    session_id       TEXT PRIMARY KEY,
    cwd              TEXT,
    transcript_path  TEXT,
    git_branch       TEXT,
    name             TEXT,
    ai_title         TEXT,
    first_prompt     TEXT,
    last_prompt      TEXT,
    note             TEXT,
    state            TEXT NOT NULL DEFAULT 'open',
    live             TEXT NOT NULL DEFAULT 'detached',
    live_detail      TEXT,
    kind             TEXT,
    bg_id            TEXT,
    linked_to        TEXT,
    created_at       INTEGER,
    last_activity_at INTEGER,
    done_at          INTEGER,
    pid              INTEGER,
    gh_ref           TEXT,
    gh_url           TEXT,
    gh_login         TEXT,
    gh_scanned       INTEGER,
    gh_checked_at    INTEGER,
    pr_ref           TEXT,
    pr_state         TEXT,
    pr_rollup        TEXT,
    pr_event         TEXT,
    pr_event_at      INTEGER,
    pr_alert         TEXT,
    pr_checked_at    INTEGER
);
"""
# Columns added after the first release; connect() adds them to older databases.
ADDED_COLUMNS = {
    "pid": "INTEGER",
    "gh_ref": "TEXT",
    "gh_url": "TEXT",
    "gh_login": "TEXT",
    "gh_scanned": "INTEGER",
    "gh_checked_at": "INTEGER",
    "pr_ref": "TEXT",
    "pr_state": "TEXT",
    "pr_rollup": "TEXT",
    "pr_event": "TEXT",
    "pr_event_at": "INTEGER",
    "pr_alert": "TEXT",
    "pr_checked_at": "INTEGER",
}


def connect() -> sqlite3.Connection:
    os.umask(0o077)  # the ledger holds prompt text, so its files are private to this user
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH, timeout=5)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript(SCHEMA)
    have = {r[1] for r in db.execute("PRAGMA table_info(items)")}
    for col, typ in ADDED_COLUMNS.items():
        if col not in have:
            db.execute(f"ALTER TABLE items ADD COLUMN {col} {typ}")
    return db


def title(row) -> str:
    return row["name"] or row["ai_title"] or row["first_prompt"] or row["session_id"][:8]


def find_transcript(session_id: str, hint: str | None = None) -> Path | None:
    if hint and Path(hint).exists():
        return Path(hint)
    return next(PROJECTS.glob(f"*/{session_id}.jsonl"), None)


def typed_text(rec: dict) -> str | None:
    """Text a person typed in a user record; None for tool results, injected context and skill text."""
    if rec.get("isMeta") or rec.get("isCompactSummary"):
        return None
    content = rec.get("message", {}).get("content")
    if isinstance(content, list):  # a prompt with pasted images
        content = " ".join(part.get("text", "") for part in content if part.get("type") == "text")
    if not isinstance(content, str) or content.startswith("<") or not content.strip():
        return None
    return content


def scan_transcript(path: Path, tail_bytes: int | None = None) -> dict:
    """Pull titles, prompts, cwd, branch and timestamps out of a transcript."""
    info: dict = {}
    size = path.stat().st_size
    with path.open("rb") as f:
        if tail_bytes and size > tail_bytes:
            f.seek(size - tail_bytes)
            f.readline()
        for raw in f:
            try:
                rec = json.loads(raw)
            except ValueError:
                continue
            t = rec.get("type")
            if t == "custom-title":
                info["name"] = rec.get("customTitle")
            elif t == "ai-title":
                info["ai_title"] = rec.get("aiTitle")
            elif t == "last-prompt":
                info["last_prompt"] = rec.get("lastPrompt")
            elif t == "pr-link" and rec.get("prRepository") and rec.get("prNumber"):
                info["pr_ref"] = f"{rec['prRepository']}#{rec['prNumber']}"
            elif t == "user" and (text := typed_text(rec)):
                info.setdefault("first_prompt", " ".join(text.split())[:300])
            if rec.get("cwd"):
                info.setdefault("cwd", rec["cwd"])
            if rec.get("gitBranch"):
                info["git_branch"] = rec["gitBranch"]
            if ts := rec.get("timestamp"):
                info.setdefault("first_ts", ts)
    info["mtime"] = int(path.stat().st_mtime)
    return info


def iso_to_epoch(ts: str) -> int:
    from datetime import datetime

    return int(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp())


def live_sessions() -> list[dict] | None:
    """Sessions Claude Code reports as running, or None when it can't say (failed, timed out, not on PATH)."""
    try:
        out = subprocess.run(
            ["claude", "agents", "--json", "--all"], capture_output=True, text=True, check=False, timeout=20
        )
        data = json.loads(out.stdout) if out.returncode == 0 else None
    except (OSError, subprocess.SubprocessError, ValueError):
        data = None
    return data if isinstance(data, list) else None


def live_state(agent: dict) -> str:
    # Background sessions report a `state`; interactive ones, and background ones without a job, report `status`.
    if agent.get("kind") == "background" and "state" in agent:
        return {"working": "working", "blocked": "waiting"}.get(agent["state"], "detached")
    return {"busy": "working", "waiting": "waiting", "idle": "idle"}.get(agent.get("status"), "working")


def cmd_snapshot(args=None) -> None:
    db = connect()
    now = int(time.time())
    agents = live_sessions()
    if agents is None:
        # Reconciling against nothing would flip every live session to detached.
        print("claude agents unavailable, skipping the live-state reconcile", file=sys.stderr)
    seen = set()
    for agent in agents or []:
        sid = agent.get("sessionId")
        if not sid:
            continue
        live = live_state(agent)
        if live == "detached":
            # Finished background sessions: only enrich rows we already track.
            db.execute(
                "UPDATE items SET kind = ?, bg_id = ? WHERE session_id = ?", (agent.get("kind"), agent.get("id"), sid)
            )
            continue
        seen.add(sid)
        db.execute(
            """INSERT INTO items (session_id, cwd, kind, bg_id, pid, live, live_detail, state,
                                  created_at, last_activity_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)
               ON CONFLICT (session_id) DO UPDATE SET
                 cwd = coalesce(cwd, excluded.cwd), kind = excluded.kind, bg_id = excluded.bg_id, pid = excluded.pid,
                 live = excluded.live,
                 live_detail = CASE WHEN excluded.live = 'waiting' THEN coalesce(excluded.live_detail, live_detail) END,
                 state = CASE WHEN state = 'stale' AND excluded.live != 'detached' THEN 'open' ELSE state END""",
            (
                sid,
                agent.get("cwd"),
                agent.get("kind"),
                agent.get("id"),
                agent.get("pid"),
                live,
                agent.get("waitingFor"),
                agent.get("startedAt", now * 1000) // 1000,
                now,
            ),
        )
    # Anything the hooks think is live but claude no longer lists has gone away (tab closed, reboot).
    for row in db.execute(f"SELECT session_id FROM items WHERE live IN {LIVE_STATES}").fetchall():
        if agents is not None and row["session_id"] not in seen:
            db.execute(
                "UPDATE items SET live = 'detached', live_detail = NULL WHERE session_id = ?", (row["session_id"],)
            )
    # Refresh titles for anything touched in the last two days.
    for row in db.execute(
        "SELECT session_id, transcript_path, coalesce(pr_ref, gh_ref) AS status_ref FROM items "
        "WHERE last_activity_at > ? OR live != 'detached'",
        (now - 2 * 86400,),
    ).fetchall():
        path = find_transcript(row["session_id"], row["transcript_path"])
        if not path:
            continue
        info = scan_transcript(path, tail_bytes=1_000_000)
        if info.get("pr_ref") and info["pr_ref"] != row["status_ref"]:
            # The session moved on to another PR: drop the old status so the new one is polled right away.
            db.execute(
                "UPDATE items SET pr_state = NULL, pr_rollup = NULL, pr_event = NULL, pr_event_at = NULL, "
                "pr_alert = NULL, pr_checked_at = NULL WHERE session_id = ?",
                (row["session_id"],),
            )
        db.execute(
            """UPDATE items SET transcript_path = ?, name = coalesce(?, name), ai_title = coalesce(?, ai_title),
                 last_prompt = coalesce(?, last_prompt), git_branch = coalesce(?, git_branch),
                 pr_ref = coalesce(?, pr_ref), last_activity_at = max(coalesce(last_activity_at, 0), ?)
               WHERE session_id = ?""",
            (
                str(path),
                info.get("name"),
                info.get("ai_title"),
                info.get("last_prompt"),
                info.get("git_branch"),
                info.get("pr_ref"),
                info["mtime"],
                row["session_id"],
            ),
        )
    db.commit()
    if not shutil.which("gh"):
        return  # no avatars or PR status without gh
    import github

    try:
        github.refresh(db)
        github.refresh_status(db)
    except (OSError, subprocess.SubprocessError, ValueError) as e:
        print(f"GitHub refresh skipped: {e}", file=sys.stderr)


def cmd_backfill(args) -> None:
    db = connect()
    cutoff = time.time() - args.days * 86400
    added = 0
    for path in PROJECTS.glob("*/*.jsonl"):
        if path.stat().st_mtime < cutoff:
            continue
        sid = path.stem
        if db.execute("SELECT 1 FROM items WHERE session_id = ?", (sid,)).fetchone():
            continue
        info = scan_transcript(path)
        if not info.get("first_prompt"):
            continue
        created = iso_to_epoch(info["first_ts"]) if info.get("first_ts") else info["mtime"]
        db.execute(
            """INSERT INTO items (session_id, cwd, transcript_path, git_branch, name, ai_title, first_prompt,
                                  last_prompt, pr_ref, state, live, created_at, last_activity_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'stale', 'detached', ?, ?)""",
            (
                sid,
                info.get("cwd"),
                str(path),
                info.get("git_branch"),
                info.get("name"),
                info.get("ai_title"),
                info.get("first_prompt"),
                info.get("last_prompt"),
                info.get("pr_ref"),
                created,
                info["mtime"],
            ),
        )
        added += 1
    db.commit()
    print(f"backfilled {added} sessions as stale")
    cmd_snapshot(args)


def resolve(db, ref: str | None) -> sqlite3.Row:
    ref = ref or os.environ.get("CLAUDE_CODE_SESSION_ID")
    if not ref:
        sys.exit("no session given and not inside a Claude Code session")
    rows = db.execute(
        "SELECT * FROM items WHERE session_id LIKE ? OR bg_id = ? OR name = ?", (ref + "%", ref, ref)
    ).fetchall()
    if len(rows) != 1:
        sys.exit(f"{len(rows)} sessions match {ref!r}")
    return rows[0]


def set_state(args, state: str) -> None:
    db = connect()
    row = resolve(db, args.session)
    db.execute(
        "UPDATE items SET state = ?, done_at = ?, note = coalesce(?, note) WHERE session_id = ?",
        (state, int(time.time()) if state == "done" else None, args.note, row["session_id"]),
    )
    db.commit()
    print(f"{state}: {title(row)}")


def cmd_note(args) -> None:
    db = connect()
    row = resolve(db, args.session)
    db.execute("UPDATE items SET note = ? WHERE session_id = ?", (args.text or None, row["session_id"]))
    db.commit()


def cmd_link(args) -> None:
    db = connect()
    row = resolve(db, args.session)
    other = resolve(db, args.other)
    db.execute("UPDATE items SET linked_to = ? WHERE session_id = ?", (other["session_id"], row["session_id"]))
    db.commit()
    print(f"{title(row)} -> {title(other)}")


def age(epoch: int | None) -> str:
    if not epoch:
        return "?"
    s = int(time.time()) - epoch
    for unit, n in (("d", 86400), ("h", 3600), ("m", 60)):
        if s >= n:
            return f"{s // n}{unit}"
    return f"{s}s"


def cmd_ls(args) -> None:
    db = connect()
    states = ("open", "parked", "stale", "done") if args.all else ("open", "parked")
    rows = db.execute(
        f"SELECT * FROM items WHERE state IN ({','.join('?' * len(states))}) "
        "ORDER BY live = 'waiting' DESC, live = 'idle' DESC, live = 'working' DESC, last_activity_at DESC",
        states,
    ).fetchall()
    for r in rows:
        repo = Path(r["cwd"] or "?").name
        print(
            f"{r['session_id'][:8]}  {r['state']:<6} {r['live']:<8} {age(r['last_activity_at']):>4}  "
            f"{repo:<22.22} {title(r)[:70]}"
        )


def resume_command(row) -> str:
    if row["kind"] == "background" and row["bg_id"]:
        return f"cd {shlex.quote(row['cwd'])} && claude attach {row['bg_id']}"
    return f"cd {shlex.quote(row['cwd'])} && claude --resume {row['session_id']}"


def open_in_iterm(command: str) -> None:
    script = f"""
tell application "iTerm"
  activate
  if (count of windows) = 0 then
    create window with default profile
  else
    tell current window to create tab with default profile
  end if
  tell current session of current window to write text {json.dumps(command)}
end tell"""
    subprocess.run(["osascript", "-e", script], check=True, capture_output=True)


def focus_tab(row) -> str:
    """Bring the iTerm tab running a live interactive session to the front, matched by tty."""
    if not row["pid"]:
        return "no pid recorded yet"
    tty = subprocess.run(
        ["ps", "-o", "tty=", "-p", str(row["pid"])], capture_output=True, text=True, check=False
    ).stdout.strip()
    if not tty or tty == "??":
        return "session has no terminal"
    script = f"""
tell application "iTerm"
  repeat with w in windows
    repeat with t in tabs of w
      repeat with s in sessions of t
        if tty of s is "/dev/{tty}" then
          select w
          tell t to select
          tell s to select
          activate
          return "focused"
        end if
      end repeat
    end repeat
  end repeat
  return "tab not found"
end tell"""
    return (
        subprocess.run(["osascript", "-e", script], capture_output=True, text=True, check=False).stdout.strip()
        or "tab not found"
    )


def resume(row) -> str:
    """Open the session in a new iTerm tab. Refuses when an interactive tab already has it."""
    if row["live"] in LIVE_STATES and row["kind"] != "background":
        return "already open in another tab"
    if not row["cwd"]:
        return "no cwd recorded"
    open_in_iterm(resume_command(row))
    return "opened"


def cmd_resume(args) -> None:
    db = connect()
    row = resolve(db, args.session)
    if args.print:
        print(resume_command(row))
    else:
        print(resume(row))


def cmd_go(args) -> None:
    db = connect()
    row = resolve(db, args.session)
    live = row["live"] in LIVE_STATES and row["kind"] != "background"
    print(focus_tab(row) if live else resume(row))


def cmd_digest(args) -> None:
    db = connect()
    now = int(time.time())
    needs = db.execute(
        "SELECT * FROM items WHERE state = 'open' AND live IN ('waiting', 'idle') AND last_activity_at < ? "
        "ORDER BY last_activity_at",
        (now - args.waiting_hours * 3600,),
    ).fetchall()
    cold = db.execute(
        "SELECT * FROM items WHERE state = 'open' AND live = 'detached' AND last_activity_at < ? "
        "ORDER BY last_activity_at",
        (now - args.cold_hours * 3600,),
    ).fetchall()
    if not needs and not cold:
        return

    def line(r) -> str:
        # Only titles go to Slack, never prompt text, and escaped so a title can't ping a channel.
        name = " ".join((r["name"] or r["ai_title"] or f"untitled session {r['session_id'][:8]}").split())[:90]
        name = name.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        return f"- {name} ({Path(r['cwd'] or '?').name}, {age(r['last_activity_at'])})"

    lines = []
    if needs:
        lines.append(f"*Waiting on you ({len(needs)})*")
        lines += [line(r) for r in needs]
    if cold:
        lines.append(f"*Open but untouched ({len(cold)})*")
        lines += [line(r) for r in cold]
    lines.append("Board: http://localhost:7777")
    text = "\n".join(lines)
    if args.dry_run:
        print(text)
        return
    service = os.environ.get("HUD_SLACK_KEYCHAIN_SERVICE", "claude-slack-webhook")
    webhook = (
        os.environ.get("HUD_SLACK_WEBHOOK")
        or subprocess.run(
            ["security", "find-generic-password", "-a", os.environ["USER"], "-s", service, "-w"],
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()
    )
    if not webhook:
        sys.exit(f"set HUD_SLACK_WEBHOOK or store the webhook in Keychain as {service!r}")
    import urllib.request

    req = urllib.request.Request(
        webhook, data=json.dumps({"text": text}).encode(), headers={"Content-Type": "application/json"}
    )
    urllib.request.urlopen(req, timeout=10)


def cmd_serve(args) -> None:
    from server import serve

    serve(args.port)


def main() -> None:
    p = argparse.ArgumentParser(prog="hud")
    sub = p.add_subparsers(required=True)

    s = sub.add_parser("ls", help="list open and parked sessions")
    s.add_argument("-a", "--all", action="store_true", help="include stale and done")
    s.set_defaults(func=cmd_ls)

    for name, state in (("done", "done"), ("park", "parked"), ("open", "open")):
        s = sub.add_parser(name, help=f"mark a session {state} (default: the current one)")
        s.add_argument("session", nargs="?")
        s.add_argument("-m", "--note")
        s.set_defaults(func=lambda a, st=state: set_state(a, st))

    s = sub.add_parser("note", help="set a note on a session")
    s.add_argument("text")
    s.add_argument("-s", "--session")
    s.set_defaults(func=cmd_note)

    s = sub.add_parser("link", help="link a session to the one it works for (e.g. a reviewer tab)")
    s.add_argument("other")
    s.add_argument("-s", "--session")
    s.set_defaults(func=cmd_link)

    s = sub.add_parser("resume", help="open a session in a new iTerm tab")
    s.add_argument("session")
    s.add_argument("-p", "--print", action="store_true", help="print the command instead")
    s.set_defaults(func=cmd_resume)

    s = sub.add_parser("go", help="focus the session's tab, or resume it in a new one")
    s.add_argument("session")
    s.set_defaults(func=cmd_go)

    s = sub.add_parser("snapshot", help="reconcile with live sessions and refresh titles")
    s.set_defaults(func=cmd_snapshot)

    s = sub.add_parser("backfill", help="import recent transcripts as stale")
    s.add_argument("--days", type=int, default=30)
    s.set_defaults(func=cmd_backfill)

    s = sub.add_parser("digest", help="post open loops to Slack")
    s.add_argument("--waiting-hours", type=float, default=1)
    s.add_argument("--cold-hours", type=float, default=24)
    s.add_argument("-n", "--dry-run", action="store_true")
    s.set_defaults(func=cmd_digest)

    s = sub.add_parser("serve", help="run the web board")
    s.add_argument("--port", type=int, default=7777)
    s.set_defaults(func=cmd_serve)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
