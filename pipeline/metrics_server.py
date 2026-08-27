#!/usr/bin/env python3
"""metrics_server.py - a localhost wrapper so the dashboard's Refresh button has something to hit.

`outreach/metrics.html` is a static file. A button inside a page opened as file:// cannot run
anything, so "refresh" needs a process to POST to. This is that process, and nothing more:

  GET  /            serve outreach/metrics.html (the same file metrics.py writes)
  POST /refresh     re-run metrics.py, then report ok/err as JSON; the page reloads itself

Refresh is RECOMPUTE ONLY. It runs metrics.py, which reads the send log / worklist / bounces and
scans Gmail read-only. It deliberately cannot source, send, or spend an Apollo credit -- refresh_cmd()
builds one fixed argv and the send chain is not reachable from it (test_metrics_server pins this).
Bound to 127.0.0.1 so it is never exposed off the machine.

  python3 pipeline/metrics_server.py                 # http://127.0.0.1:8787
  python3 pipeline/metrics_server.py --port 9000 --days 14 --no-gmail
"""
import argparse, json, os, subprocess, sys, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
OUT_HTML = os.path.join(ROOT, "outreach", "metrics.html")
METRICS = os.path.join(HERE, "metrics.py")
HOST = "127.0.0.1"

# One Gmail scan at a time. A double-click (or two open tabs) must not launch two overlapping
# metrics runs that race on the same output file.
_REFRESH_LOCK = threading.Lock()


def refresh_cmd(days, use_gmail):
    """The exact argv a refresh runs. Pure and inspectable ON PURPOSE: the safety property that a
    browser button can never send mail or spend a credit is only worth having if it is testable,
    so this returns the command instead of hiding it inside run_metrics()."""
    cmd = [sys.executable, METRICS, "--days", str(int(days))]
    if not use_gmail:
        cmd.append("--no-gmail")
    return cmd


def run_metrics(days, use_gmail, timeout=180):
    """Re-run metrics.py in a subprocess. Returns (ok, message)."""
    try:
        p = subprocess.run(refresh_cmd(days, use_gmail), cwd=ROOT,
                           capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, f"metrics.py timed out after {timeout}s"
    except Exception as e:                                    # pragma: no cover - defensive
        return False, f"could not run metrics.py: {str(e)[:200]}"
    if p.returncode != 0:
        return False, (p.stderr or p.stdout or "metrics.py exited non-zero").strip()[:300]
    # metrics.py's last stdout line is a one-glance status; hand it back to the page.
    lines = [ln for ln in p.stdout.strip().splitlines() if ln.strip()]
    return True, lines[-1] if lines else "refreshed"


class Handler(BaseHTTPRequestHandler):
    # set from main(); class attributes so every request thread sees the same config
    days = 30
    use_gmail = True

    def _reply(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, (bytes, bytearray)) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _json(self, code, obj):
        self._reply(code, json.dumps(obj), "application/json; charset=utf-8")

    def do_GET(self):
        if self.path.split("?")[0] not in ("/", "/index.html", "/metrics.html"):
            return self._json(404, {"error": "not found"})
        try:
            with open(OUT_HTML, "rb") as f:
                html = f.read()
        except FileNotFoundError:
            return self._reply(404, "<h1>No dashboard yet</h1><p>Run <code>python3 "
                               "pipeline/metrics.py</code> first.</p>", "text/html; charset=utf-8")
        self._reply(200, html, "text/html; charset=utf-8")

    def do_POST(self):
        if self.path.split("?")[0] != "/refresh":
            return self._json(404, {"error": "not found"})
        if not _REFRESH_LOCK.acquire(blocking=False):
            return self._json(409, {"ok": False, "message": "a refresh is already running"})
        try:
            ok, msg = run_metrics(self.days, self.use_gmail)
        finally:
            _REFRESH_LOCK.release()
        self._json(200 if ok else 500, {"ok": ok, "message": msg})

    def log_message(self, *a):     # keep the terminal quiet; this is a personal dashboard
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--no-gmail", action="store_true")
    a = ap.parse_args()
    Handler.days = a.days
    Handler.use_gmail = not a.no_gmail
    # Guarantee there is a page to serve on first load.
    if not os.path.exists(OUT_HTML):
        print("[metrics-server] no dashboard yet - building one…")
        ok, msg = run_metrics(a.days, not a.no_gmail)
        print(f"[metrics-server] {'built' if ok else 'build failed: ' + msg}")
    srv = ThreadingHTTPServer((HOST, a.port), Handler)
    print(f"[metrics-server] http://{HOST}:{a.port}  ·  Refresh re-runs metrics.py "
          f"(recompute only — never sends).  Ctrl-C to stop.")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n[metrics-server] stopped.")
        srv.shutdown()


if __name__ == "__main__":
    main()
