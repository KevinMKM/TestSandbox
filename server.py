"""HTTP API for the coding-agent sandbox runner (called by n8n).

Uses only the Python standard library, so the Docker image needs no pip/apt downloads.
Endpoints: GET /health, POST /prepare, /read-files, /apply-and-test, /finalize,
           DELETE /workspace/<id>
"""
import hmac
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import core

API_KEY = os.getenv("RUNNER_API_KEY", "")
PORT = int(os.getenv("PORT", "8000"))
MAX_BODY_BYTES = 20 * 1024 * 1024


class BadRequest(Exception):
    pass


def _str(body, key, default=None, required=False):
    val = body.get(key, default)
    if required and (val is None or val == ""):
        raise BadRequest(f"'{key}' is required")
    if val is not None and not isinstance(val, (str, int)):
        raise BadRequest(f"'{key}' must be a string")
    return val if val is None else str(val)


def _list(body, key):
    val = body.get(key, [])
    if not isinstance(val, list):
        raise BadRequest(f"'{key}' must be a list")
    return val


def route_prepare(body):
    return core.prepare(_str(body, "run_id", required=True), _str(body, "mode", "existing_repo"),
                        _str(body, "repo_url", ""), _str(body, "branch", "main"))


def route_read_files(body):
    paths = [p for p in _list(body, "paths") if isinstance(p, str)]
    return core.read_files(_str(body, "workspace_id", required=True), paths,
                           int(body.get("max_bytes_per_file", 20000)))


def route_apply_and_test(body):
    changes = _list(body, "changes")
    for ch in changes:
        if not isinstance(ch, dict) or not isinstance(ch.get("path"), str) \
                or not isinstance(ch.get("content", ""), str):
            raise BadRequest("each change must be {path: string, content: string}")
    cmds = [c for c in _list(body, "test_commands") if isinstance(c, str) and c.strip()]
    return core.apply_and_test(_str(body, "workspace_id", required=True), changes, cmds,
                               int(body.get("attempt", 0)))


def route_finalize(body):
    return core.finalize(_str(body, "workspace_id", required=True), bool(body.get("approved", False)),
                         _str(body, "commit_message", "AI coding agent changes"))


POST_ROUTES = {
    "/prepare": route_prepare,
    "/read-files": route_read_files,
    "/apply-and-test": route_apply_and_test,
    "/finalize": route_finalize,
}


class Handler(BaseHTTPRequestHandler):
    server_version = "SandboxRunner/1.0"

    def _send(self, status, obj):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _authorized(self):
        if not API_KEY:
            return True
        return hmac.compare_digest(self.headers.get("X-API-Key", ""), API_KEY)

    def _handle(self, fn):
        try:
            self._send(200, fn())
        except core.RunnerError as exc:
            self._send(exc.status, {"detail": exc.message})
        except BadRequest as exc:
            self._send(400, {"detail": str(exc)})
        except (ValueError, json.JSONDecodeError) as exc:
            self._send(400, {"detail": f"bad request: {exc}"})
        except Exception as exc:  # pragma: no cover - last-resort guard
            self._send(500, {"detail": f"internal error: {exc}"})

    def do_GET(self):
        if self.path == "/health":
            return self._send(200, {"status": "ok"})
        self._send(404, {"detail": "not found"})

    def do_POST(self):
        if not self._authorized():
            return self._send(401, {"detail": "invalid or missing API key"})
        fn = POST_ROUTES.get(self.path)
        if fn is None:
            return self._send(404, {"detail": "not found"})
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY_BYTES:
            return self._send(413, {"detail": "request body too large"})
        raw = self.rfile.read(length) if length else b"{}"

        def run():
            body = json.loads(raw.decode("utf-8") or "{}")
            if not isinstance(body, dict):
                raise BadRequest("JSON body must be an object")
            return fn(body)

        self._handle(run)

    def do_DELETE(self):
        if not self._authorized():
            return self._send(401, {"detail": "invalid or missing API key"})
        prefix = "/workspace/"
        if not self.path.startswith(prefix):
            return self._send(404, {"detail": "not found"})
        self._handle(lambda: core.cleanup(self.path[len(prefix):]))

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))


if __name__ == "__main__":
    print(f"Sandbox runner listening on :{PORT} (auth {'ON' if API_KEY else 'OFF'})", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
