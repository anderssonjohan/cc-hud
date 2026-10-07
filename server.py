"""Local web board for the hud ledger. Binds to 127.0.0.1 only."""

import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import github
import hud

BOARD = Path(__file__).with_name("board.html")
EDITABLE = {"state", "note", "linked_to"}
STATES = {"open", "parked", "stale", "done"}


def items() -> list[dict]:
    db = hud.connect()
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
    def log_message(self, format, *args):
        pass

    def _allowed(self) -> bool:
        # Host check stops DNS rebinding; the custom header forces a CORS preflight we never answer,
        # so other sites can't drive the resume endpoint from a browser.
        host_ok = self.headers.get("Host", "").split(":")[0] in ("localhost", "127.0.0.1")
        return host_ok and (self.command == "GET" or self.headers.get("X-HUD") == "1")

    def _send(self, code: int, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
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
        if self.path == "/api/items":
            return self._send(200, items())
        if self.path.startswith("/avatars/") and (path := github.avatar_path(self.path.removeprefix("/avatars/"))):
            data = path.read_bytes()
            kind = "image/png" if data.startswith(b"\x89PNG") else "image/svg+xml" if data.startswith(b"<svg") else "image/jpeg"
            return self._send(200, data, kind)
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._allowed():
            return self._send(403, {"error": "forbidden"})
        parts = self.path.strip("/").split("/")
        if len(parts) != 3 or parts[:1] != ["api"]:
            return self._send(404, {"error": "not found"})
        _, action, sid = parts
        db = hud.connect()
        row = db.execute("SELECT * FROM items WHERE session_id = ?", (sid,)).fetchone()
        if not row:
            return self._send(404, {"error": "unknown session"})
        if action == "resume":
            return self._send(200, {"result": hud.resume(row)})
        if action == "focus":
            return self._send(200, {"result": hud.focus_tab(row)})
        if action == "items":
            length = int(self.headers.get("Content-Length", 0))
            patch = {k: v for k, v in json.loads(self.rfile.read(length) or b"{}").items() if k in EDITABLE}
            if "state" in patch:
                if patch["state"] not in STATES:
                    return self._send(400, {"error": "bad state"})
                patch["done_at"] = int(time.time()) if patch["state"] == "done" else None
            if patch:
                cols = ", ".join(f"{k} = ?" for k in patch)
                db.execute(f"UPDATE items SET {cols} WHERE session_id = ?", (*patch.values(), sid))
                db.commit()
            return self._send(200, {"ok": True})
        self._send(404, {"error": "not found"})


def serve(port: int) -> None:
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
