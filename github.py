"""Find the GitHub PR or issue a session is about and cache the avatar of the person behind it."""

import json
import os
import re
import subprocess
import time
import urllib.request
from fnmatch import fnmatchcase
from pathlib import Path

import hud

AVATARS = Path(os.environ.get("HUD_DB", Path.home() / ".claude/hud/hud.db")).parent / "avatars"
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
            if rec.get("type") == "user" and (content := hud.typed_text(rec)):
                if m := REF_URL.search(content):
                    return f"{m[1]}#{m[3]}", f"https://{m[0]}"
            elif rec.get("type") == "pr-link" and not fallback and rec.get("prRepository") and rec.get("prNumber"):
                fallback = f"{rec['prRepository']}#{rec['prNumber']}", rec.get("prUrl")
    return fallback


def gh_json(*args: str):
    out = subprocess.run(["gh", "api", *args], capture_output=True, text=True, check=False, timeout=15)
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
    """Logins to skip, such as automation accounts that look like people. One per line in ignore-logins."""
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
    for row in db.execute(
        "SELECT session_id, transcript_path FROM items WHERE gh_scanned IS NULL AND transcript_path IS NOT NULL"
    ).fetchall():
        path = Path(row["transcript_path"] or "")
        ref = first_ref(path) if path.is_file() else None
        db.execute(
            "UPDATE items SET gh_scanned = 1, gh_ref = coalesce(gh_ref, ?), gh_url = coalesce(gh_url, ?) "
            "WHERE session_id = ?",
            (*(ref or (None, None)), row["session_id"]),
        )
        # Commit per row: holding the write lock across transcript reads and network calls makes hooks give up.
        db.commit()

    now = int(time.time())
    rows = db.execute(
        "SELECT session_id, gh_ref FROM items WHERE gh_ref IS NOT NULL AND gh_login IS NULL "
        "AND coalesce(gh_checked_at, 0) < ? ORDER BY last_activity_at DESC LIMIT ?",
        (now - RECHECK_AFTER, LOOKUPS_PER_RUN),
    ).fetchall()
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
        db.execute(
            "UPDATE items SET gh_login = ?, gh_checked_at = ? WHERE session_id = ?", (login, now, row["session_id"])
        )
        db.commit()


def avatar_path(login: str) -> Path | None:
    if not LOGIN.match(login):
        return None
    path = AVATARS / login
    return path if path.is_file() else None


REPO_PART = re.compile(r"^[\w.-]+$")
FAST, SLOW, FINAL = 60, 900, 86400
PRS_PER_QUERY = 20
STEPS_PER_QUERY = 10

STATUS_FIELDS = """
  __typename
  ... on Issue { state closedAt }
  ... on PullRequest {
    state mergedAt closedAt reviewDecision
    commits(last: 1) { nodes { commit { statusCheckRollup { state contexts(first: 50) { nodes {
      __typename
      ... on CheckRun { name status conclusion startedAt completedAt }
      ... on StatusContext { context state createdAt }
    } } } } } }
    latestReviews(first: 10) { nodes { author { login } state submittedAt } }
  }
"""
RELEASE_FIELDS = (
    "releases(first: 10, orderBy: {field: CREATED_AT, direction: DESC}) { nodes { tagName publishedAt isDraft } }"
)
VERB = {
    "SUCCESS": "passed",
    "FAILURE": "failed",
    "TIMED_OUT": "timed out",
    "CANCELLED": "cancelled",
    "ACTION_REQUIRED": "needs action",
    "STARTUP_FAILURE": "failed",
    "ERROR": "failed",
}
REVIEW = {"APPROVED": "approved", "CHANGES_REQUESTED": "requested changes", "COMMENTED": "reviewed"}


def epoch(ts: str | None) -> int | None:
    from datetime import datetime

    return int(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()) if ts else None


def reviewer(login: str | None) -> str:
    return "Copilot" if login and login.lower().startswith("copilot") else (login or "someone")


def summarize(node: dict) -> dict:
    """Reduce a PR or issue to its state, check rollup, latest event and whether it needs attention."""
    if node.get("__typename") == "Issue":
        closed = node.get("state") == "CLOSED"
        return {
            "pr_state": node.get("state"),
            "pr_rollup": None,
            "pr_alert": "closed" if closed else None,
            "pr_event": "issue closed" if closed else None,
            "pr_event_at": epoch(node.get("closedAt")),
        }
    events, running, failed = [], [], []
    commit = ((node.get("commits") or {}).get("nodes") or [{}])[0].get("commit") or {}
    rollup = commit.get("statusCheckRollup") or {}
    for c in (rollup.get("contexts") or {}).get("nodes") or []:
        if c.get("__typename") == "CheckRun":
            if c.get("status") != "COMPLETED":
                running.append(epoch(c.get("startedAt")) or 0)
            elif c.get("conclusion") in VERB:
                events.append((epoch(c.get("completedAt")) or 0, f"{c['name']} {VERB[c['conclusion']]}"))
                if c["conclusion"] != "SUCCESS":
                    failed.append(events[-1])
        elif c.get("state") == "PENDING":
            running.append(epoch(c.get("createdAt")) or 0)
        elif c.get("state") in VERB:
            events.append((epoch(c.get("createdAt")) or 0, f"{c['context']} {VERB[c['state']]}"))
            if c["state"] != "SUCCESS":
                failed.append(events[-1])
    for r in (node.get("latestReviews") or {}).get("nodes") or []:
        if r.get("state") in REVIEW:
            events.append(
                (
                    epoch(r.get("submittedAt")) or 0,
                    f"{reviewer((r.get('author') or {}).get('login'))} {REVIEW[r['state']]}",
                )
            )
    # The line shows what matters most, not just what happened last: merged, then running, then red, then latest.
    at, text = max(events) if events else (None, None)
    if node.get("mergedAt"):
        at, text = epoch(node["mergedAt"]), "PR merged"
    elif node.get("state") == "CLOSED":
        at, text = epoch(node.get("closedAt")), "PR closed"
    elif running:
        at, text = max(running), f"{len(running)} check{'s' * (len(running) > 1)} running"
    elif failed and rollup.get("state") in ("FAILURE", "ERROR"):
        at, text = max(failed)
        if len(failed) > 1:
            text += f" (+{len(failed) - 1} more)"

    if node.get("mergedAt"):
        alert = "merged"
    elif node.get("state") == "CLOSED":
        alert = "closed"
    elif rollup.get("state") in ("FAILURE", "ERROR"):
        alert = "failed"
    elif node.get("reviewDecision") == "CHANGES_REQUESTED":
        alert = "changes"
    else:
        alert = None
    return {
        "pr_state": node.get("state"),
        "pr_rollup": rollup.get("state"),
        "pr_alert": alert,
        "pr_event": text,
        "pr_event_at": at,
    }


def split_ref(ref: str) -> tuple[str, str, int | None] | None:
    """owner, repo and number from owner/repo#123, or owner and repo alone from owner/repo; None if malformed."""
    repo, _, number = ref.partition("#")
    owner, _, name = repo.partition("/")
    if not (REPO_PART.match(owner) and REPO_PART.match(name)) or (number and not number.isdigit()):
        return None
    return owner, name, int(number) if number else None


def release_after(nodes: list, since: int, pattern: str) -> dict | None:
    """The first published release since `since` whose tag matches the pattern."""
    hits = [
        n
        for n in nodes
        if n
        and not n.get("isDraft")
        and (epoch(n.get("publishedAt")) or 0) >= since
        and fnmatchcase(n.get("tagName") or "", pattern)
    ]
    return min(hits, key=lambda n: n["publishedAt"]) if hits else None


def refresh_status(db) -> None:
    """Poll PR state for open sessions: every minute while recent, every 15 minutes otherwise, daily once final.

    Steps that wait on a PR or a release ride along in the same request.
    """
    now = int(time.time())
    rows = db.execute(
        """SELECT session_id, coalesce(pr_ref, gh_ref) AS ref FROM items
           WHERE state IN ('open', 'parked') AND coalesce(pr_ref, gh_ref) IS NOT NULL
             AND coalesce(pr_checked_at, 0) < ? - CASE
                   WHEN pr_state IN ('MERGED', 'CLOSED') THEN ?
                   WHEN live != 'detached' OR last_activity_at > ? THEN ?
                   ELSE ? END
           ORDER BY live != 'detached' DESC, last_activity_at DESC LIMIT ?""",
        (now, FINAL, now - 2 * 86400, FAST, SLOW, PRS_PER_QUERY),
    ).fetchall()
    # A release only counts once the steps before it are done, and only if it came out after them: a release cut
    # from someone else's merge while the PR is still open is not the one the next step is waiting for.
    steps = db.execute(
        f"""SELECT id, kind, cond, coalesce(
                   (SELECT max(e.done_at) FROM steps e WHERE e.thread_id = steps.thread_id AND e.pos < steps.pos),
                   created_at) AS since
            FROM steps
            WHERE done_at IS NULL AND kind IN ('pr_merged', 'release')
              AND NOT (kind = 'release' AND EXISTS (SELECT 1 FROM steps e WHERE e.thread_id = steps.thread_id
                                                    AND e.pos < steps.pos AND e.done_at IS NULL))
              AND coalesce(checked_at, 0) < ? - CASE WHEN created_at > ? THEN ? ELSE ? END
              AND EXISTS (SELECT 1 FROM items WHERE {hud.THREAD} = steps.thread_id AND state IN ('open', 'parked'))
            ORDER BY created_at DESC LIMIT ?""",
        (now, now - 2 * 86400, FAST, SLOW, STEPS_PER_QUERY),
    ).fetchall()
    prs: dict[tuple, dict] = {}
    for row in rows:
        if (ref := split_ref(row["ref"])) and ref[2] is not None:
            prs.setdefault(ref, {"sessions": [], "steps": []})["sessions"].append(row["session_id"])
    releases: dict[tuple, list] = {}
    for step in steps:
        ref = split_ref(step["cond"].partition("@")[0])
        if step["kind"] == "pr_merged" and ref and ref[2] is not None:
            prs.setdefault(ref, {"sessions": [], "steps": []})["steps"].append(step)
        elif step["kind"] == "release" and ref and ref[2] is None:
            releases.setdefault(ref[:2], []).append(step)
        else:
            db.execute(
                "UPDATE steps SET checked_at = ?, detail = 'not a valid reference' WHERE id = ?", (now, step["id"])
            )
    if not prs and not releases:
        db.commit()
        return
    aliases = [
        f'p{i}: repository(owner: "{o}", name: "{n}") {{ issueOrPullRequest(number: {num}) {{ {STATUS_FIELDS} }} }}'
        for i, (o, n, num) in enumerate(prs)
    ] + [f'r{i}: repository(owner: "{o}", name: "{n}") {{ {RELEASE_FIELDS} }}' for i, (o, n) in enumerate(releases)]
    query = "query { " + " ".join(aliases) + " }"
    out = subprocess.run(
        ["gh", "api", "graphql", "-f", f"query={query}"], capture_output=True, text=True, check=False, timeout=30
    )
    try:
        data = json.loads(out.stdout).get("data") or {}
    except ValueError:
        return
    for i, target in enumerate(prs.values()):
        node = ((data.get(f"p{i}") or {}).get("issueOrPullRequest")) or {}
        status = summarize(node) if node else {}
        for sid in target["sessions"]:
            if status:
                db.execute(
                    "UPDATE items SET pr_state = ?, pr_rollup = ?, pr_event = ?, pr_event_at = ?, pr_alert = ?, "
                    "pr_checked_at = ? WHERE session_id = ?",
                    (
                        status["pr_state"],
                        status["pr_rollup"],
                        status["pr_event"],
                        status["pr_event_at"],
                        status["pr_alert"],
                        now,
                        sid,
                    ),
                )
            else:
                db.execute("UPDATE items SET pr_checked_at = ? WHERE session_id = ?", (now, sid))
        merged = epoch(node.get("mergedAt"))
        closed = node.get("state") == "CLOSED" and not merged
        for step in target["steps"]:
            db.execute(
                "UPDATE steps SET checked_at = ?, done_at = ?, detail = ? WHERE id = ?",
                (now, merged, "closed without merging" if closed else None, step["id"]),
            )
    for i, waiting in enumerate(releases.values()):
        nodes = (((data.get(f"r{i}") or {}).get("releases")) or {}).get("nodes") or []
        for step in waiting:
            hit = release_after(nodes, step["since"] or 0, step["cond"].partition("@")[2] or "*")
            db.execute(
                "UPDATE steps SET checked_at = ?, done_at = ?, detail = ? WHERE id = ?",
                (now, epoch(hit["publishedAt"]) if hit else None, hit["tagName"] if hit else None, step["id"]),
            )
    db.commit()
