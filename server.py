"""Local web board for the hud ledger. Binds to 127.0.0.1 only."""

import hmac
import json
import time
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import github
import hud

BOARD = Path(__file__).with_name("board.html")
EDITABLE = {"state", "note", "linked_to"}
STATES = {"open", "parked", "stale", "done"}
MAX_BODY = 64 * 1024
NO_TOKEN = "Open the board from the menu bar, or with `hud.py board`, to sign it in."


def items() -> list[dict]:
    # Close explicitly: a sqlite3 connection's statement cache refers back to it, so dropping the last reference
    # leaves it to the cycle collector and a long-running server runs out of file descriptors first.
    with closing(hud.connect()) as db:
        rows = db.execute(
            "SELECT * FROM items WHERE state != 'done' OR done_at > ? ORDER BY last_activity_at DESC",
            (int(time.time()) - 7 * 86400,),
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["title"] = hud.title(r)
        d["repo"] = Path(r["cwd"] or "?").name
        d["resume_cmd"] = hud.resume_command(r) if r["cwd"] else None
        d["avatar"] = f"/avatars/{r['gh_login']}" if r["gh_login"] and github.avatar_path(r["gh_login"]) else None
        out.append(d)
    return out


class Handler(BaseHTTPRequestHandler):
    token = ""

    def log_message(self, format, *args):
        pass

    def _allowed(self) -> bool:
        # Host check stops DNS rebinding; the custom header forces a CORS preflight we never answer,
        # so other sites can't drive the resume endpoint from a browser.
        host_ok = self.headers.get("Host", "").split(":")[0] in ("localhost", "127.0.0.1")
        return host_ok and (self.command == "GET" or self.headers.get("X-HUD") == "1")

    def _signed_in(self) -> bool:
        # The page itself holds no data; everything behind it needs the per-install token, which only this user
        # can read. Anything else that reaches the port, like a container, gets nothing.
        return hmac.compare_digest(self.headers.get("X-HUD-Token", ""), self.token)

    def _send(self, code: int, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "frame-ancestors 'none'")
        if ctype == "image/svg+xml":
            self.send_header("Content-Security-Policy", "script-src 'none'")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if not self._allowed():
            return self._send(403, {"error": "forbidden"})
        if self.path == "/":
            return self._send(200, BOARD.read_bytes(), "text/html; charset=utf-8")
        if not self._signed_in():
            return self._send(401, {"error": NO_TOKEN})
        if self.path == "/api/items":
            return self._send(200, items())
        if self.path.startswith("/avatars/") and (path := github.avatar_path(self.path.removeprefix("/avatars/"))):
            data = path.read_bytes()
            kind = (
                "image/png"
                if data.startswith(b"\x89PNG")
                else "image/svg+xml"
                if data.startswith(b"<svg")
                else "image/jpeg"
            )
            return self._send(200, data, kind)
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._allowed():
            return self._send(403, {"error": "forbidden"})
        if not self._signed_in():
            return self._send(401, {"error": NO_TOKEN})
        parts = self.path.strip("/").split("/")
        if len(parts) != 3 or parts[:1] != ["api"]:
            return self._send(404, {"error": "not found"})
        _, action, sid = parts
        with closing(hud.connect()) as db:
            return self._post(db, action, sid)

    def _body(self) -> dict | None:
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            return None
        if not 0 <= length <= MAX_BODY:
            return None
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return None
        return body if isinstance(body, dict) else None

    def _post(self, db, action: str, sid: str):
        row = db.execute("SELECT * FROM items WHERE session_id = ?", (sid,)).fetchone()
        if not row:
            return self._send(404, {"error": "unknown session"})
        if action == "resume":
            return self._send(200, {"result": hud.resume(row)})
        if action == "focus":
            return self._send(200, {"result": hud.focus_tab(row)})
        if action == "items":
            body = self._body()
            if body is None:
                return self._send(400, {"error": "expected a JSON object of at most 64 KiB"})
            patch = {k: v for k, v in body.items() if k in EDITABLE}
            if any(v is not None and not isinstance(v, str) for v in patch.values()):
                return self._send(400, {"error": "values must be strings or null"})
            if "state" in patch:
                if patch["state"] not in STATES:
                    return self._send(400, {"error": "bad state"})
                patch["done_at"] = int(time.time()) if patch["state"] == "done" else None
            if patch:
                cols = ", ".join(f"{k} = ?" for k in patch)
                db.execute(f"UPDATE items SET {cols} WHERE session_id = ?", (*patch.values(), sid))
                db.commit()
            return self._send(200, {"ok": True})
        return self._send(404, {"error": "not found"})


def serve(port: int) -> None:
    Handler.token = hud.token()
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
