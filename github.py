"""Find the GitHub PR or issue a session is about and cache the avatar of the person behind it."""

import json
import re
import subprocess
import time
import urllib.request
from pathlib import Path

AVATARS = Path.home() / ".claude/hud/avatars"
REF_URL = re.compile(r"github\.com/([\w.-]+/[\w.-]+)/(pull|issues)/(\d+)")
LOGIN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
RECHECK_AFTER = 7 * 86400
LOOKUPS_PER_RUN = 15


def first_ref(path: Path) -> tuple[str, str] | None:
    """The first PR or issue linked in a prompt, else the first PR Claude Code linked to the session."""
    fallback = None
    with path.open("rb") as f:
        for raw in f:
            if b"github.com/" not in raw and b'"pr-link"' not in raw:
                continue
            try:
                rec = json.loads(raw)
            except ValueError:
                continue
            if rec.get("type") == "user" and isinstance(content := rec.get("message", {}).get("content"), str):
                if m := REF_URL.search(content):
                    return f"{m[1]}#{m[3]}", f"https://{m[0]}"
            elif rec.get("type") == "pr-link" and not fallback and rec.get("prRepository") and rec.get("prNumber"):
                fallback = f"{rec['prRepository']}#{rec['prNumber']}", rec.get("prUrl")
    return fallback


def gh_json(*args: str):
    out = subprocess.run(["gh", "api", *args], capture_output=True, text=True, timeout=15)
    return json.loads(out.stdout) if out.returncode == 0 else None


def me() -> str | None:
    cache = AVATARS.parent / "gh-user"
    if cache.exists():
        return cache.read_text().strip() or None
    user = gh_json("user")
    login = user.get("login") if user else None
    if login:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(login)
    return login


def person(ref: str, myself: str | None) -> str | None:
    """Whoever opened the PR or issue, or the first assignee when that was me."""
    repo, number = ref.split("#")
    data = gh_json(f"repos/{repo}/issues/{number}")
    if not data:
        return None
    ignore = ignored()
    for user in [data.get("user") or {}] + list(data.get("assignees") or []):
        login = user.get("login")
        if login and login != myself and login not in ignore and user.get("type") == "User" and LOGIN.match(login):
            return login
    return None


def ignored() -> set[str]:
    """Logins to skip, such as automation accounts that look like people: one per line in ~/.claude/hud/ignore-logins."""
    path = AVATARS.parent / "ignore-logins"
    return {line.strip() for line in path.read_text().splitlines() if line.strip()} if path.exists() else set()


def cache_avatar(login: str) -> None:
    dest = AVATARS / login
    if dest.exists():
        return
    AVATARS.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(f"https://avatars.githubusercontent.com/{login}?s=96", timeout=10) as r:
        dest.write_bytes(r.read())


def refresh(db) -> None:
    """Discover refs once per session, then resolve a few people per run so a backlog never stalls the snapshot."""
    for row in db.execute("SELECT session_id, transcript_path FROM items WHERE gh_scanned IS NULL AND transcript_path IS NOT NULL").fetchall():
        path = Path(row["transcript_path"] or "")
        ref = first_ref(path) if path.is_file() else None
        db.execute("UPDATE items SET gh_scanned = 1, gh_ref = coalesce(gh_ref, ?), gh_url = coalesce(gh_url, ?) "
                   "WHERE session_id = ?", (*(ref or (None, None)), row["session_id"]))
    db.commit()

    now = int(time.time())
    rows = db.execute(
        "SELECT session_id, gh_ref FROM items WHERE gh_ref IS NOT NULL AND gh_login IS NULL "
        "AND coalesce(gh_checked_at, 0) < ? ORDER BY last_activity_at DESC LIMIT ?",
        (now - RECHECK_AFTER, LOOKUPS_PER_RUN)).fetchall()
    if not rows:
        return
    myself = me()
    for row in rows:
        try:
            login = person(row["gh_ref"], myself)
            if login:
                cache_avatar(login)
        except (OSError, subprocess.SubprocessError, ValueError):
            login = None
        db.execute("UPDATE items SET gh_login = ?, gh_checked_at = ? WHERE session_id = ?",
                   (login, now, row["session_id"]))
    db.commit()


def avatar_path(login: str) -> Path | None:
    if not LOGIN.match(login):
        return None
    path = AVATARS / login
    return path if path.is_file() else None
