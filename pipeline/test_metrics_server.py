#!/usr/bin/env python3
"""Tests for metrics_server.py - the localhost wrapper behind the dashboard Refresh button.

Offline and network-free: it talks only to a loopback server it starts itself, and the actual
metrics.py subprocess is patched out (run_metrics is replaced with a fake), so no Gmail, no Apollo,
no send log needed. Run: python3 pipeline/test_metrics_server.py

The load-bearing test is the first one: a browser button must never be able to source, send, or
spend a credit, so refresh_cmd() is pinned to metrics.py with the send chain unreachable from it.
"""
import json, os, sys, tempfile, threading, urllib.error, urllib.request
from http.server import ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
import metrics_server as M

PASS = 0; FAIL = 0
def check(name, cond):
    global PASS, FAIL
    if cond: PASS += 1
    else: FAIL += 1; print(f"FAIL: {name}")


# ---------- refresh can ONLY recompute: the send chain is not reachable from the button ----------
cmd = M.refresh_cmd(30, use_gmail=True)
joined = " ".join(cmd)
check("refresh runs metrics.py", "metrics.py" in joined)
check("refresh never sends", "send_outreach" not in joined)
check("refresh never sources / spends Apollo credits", "apollo" not in joined.lower())
check("refresh never scans bounces or validates", "bounce" not in joined and "validate" not in joined)
check("gmail on by default -> no --no-gmail flag", "--no-gmail" not in cmd)
check("--no-gmail passes through when asked", "--no-gmail" in M.refresh_cmd(30, use_gmail=False))
check("days is coerced to int (no shell-y strings reach argv)", M.refresh_cmd("14", True)[3] == "14")


# ---------- HTTP behaviour, against a real loopback server with metrics.py stubbed out ----------
def request(method, path, port):
    """Return (status, body_text). Never raises on 4xx/5xx."""
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method)
    try:
        r = urllib.request.urlopen(req, timeout=5)
        return r.status, r.read().decode("utf-8", "ignore")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "ignore")

# a temp dashboard file so GET / has something to serve
tmp = tempfile.NamedTemporaryFile("w", suffix=".html", delete=False)
tmp.write("<!doctype html><title>Review campaign</title>DASHBOARD-MARKER")
tmp.close()
M.OUT_HTML = tmp.name

# control the "subprocess" from the test: no real metrics.py run
_refresh_gate = threading.Event(); _refresh_entered = threading.Event()
_fake = {"ok": True, "msg": "refreshed", "block": False}
def fake_run_metrics(days, use_gmail, timeout=180):
    _refresh_entered.set()
    if _fake["block"]:
        _refresh_gate.wait(5)
    return _fake["ok"], _fake["msg"]
M.run_metrics = fake_run_metrics

srv = ThreadingHTTPServer(("127.0.0.1", 0), M.Handler)
PORT = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()

try:
    # GET the dashboard
    st, body = request("GET", "/", PORT)
    check("GET / returns 200", st == 200)
    check("GET / serves the dashboard file", "DASHBOARD-MARKER" in body)

    st, _ = request("GET", "/anything-else", PORT)
    check("GET unknown path returns 404", st == 404)

    # POST /refresh, success
    _refresh_entered.clear(); _fake.update(ok=True, msg="12 sent · 1 signup", block=False)
    st, body = request("POST", "/refresh", PORT)
    j = json.loads(body)
    check("POST /refresh returns 200 on success", st == 200)
    check("success payload is ok:true", j.get("ok") is True)
    check("success payload carries metrics.py's status line", "signup" in j.get("message", ""))

    # POST /refresh, metrics.py failed
    _fake.update(ok=False, msg="metrics.py exited non-zero")
    st, body = request("POST", "/refresh", PORT)
    j = json.loads(body)
    check("POST /refresh returns 500 when metrics.py fails", st == 500)
    check("failure payload is ok:false", j.get("ok") is False)

    # POST to a bad path
    st, _ = request("POST", "/send-everything", PORT)
    check("POST to any path but /refresh is 404", st == 404)

    # single-flight: a refresh in progress makes a second one 409, not a second scan
    _fake.update(ok=True, msg="slow", block=True)
    _refresh_entered.clear()
    holder = {}
    t = threading.Thread(target=lambda: holder.__setitem__("r", request("POST", "/refresh", PORT)))
    t.start()
    check("first refresh actually started", _refresh_entered.wait(5))
    st2, body2 = request("POST", "/refresh", PORT)          # second, while first still holds the lock
    check("concurrent refresh is rejected with 409", st2 == 409)
    check("409 explains why", "already running" in json.loads(body2).get("message", ""))
    _refresh_gate.set(); t.join(5)
    check("the first refresh still completed 200", holder.get("r", (0,))[0] == 200)
finally:
    srv.shutdown()
    os.unlink(tmp.name)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
