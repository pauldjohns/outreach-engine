#!/usr/bin/env python3
"""
gmail_auth.py - Gmail API auth + send primitive for the outreach sender.

Isolates ALL Gmail-specific concerns behind two functions so the sender (and any
swap to SMTP or a dedicated domain later) touches nothing else:
    service()                       -> an authed Gmail API client
    send(service, to, subject, body_text, from_addr=None) -> message id

Auth is OAuth "installed app" (Desktop client). One-time browser consent on first
run stores a refresh token at outreach/.gmail/token.json; subsequent runs are silent.
Scope defaults to gmail.send (least privilege). The bounce/reply scan, if enabled,
needs gmail.readonly too -- set GMAIL_SCOPES accordingly and re-consent.

Setup (one-time, the operator):
  1. Google Cloud Console -> new project.
  2. Enable Gmail API.
  3. OAuth consent screen -> add scope gmail.send -> add sender@example.com as test user.
  4. Credentials -> OAuth client ID -> Desktop app -> download JSON.
  5. Save it as ~/.config/outreach-engine/client_secret.json  (out of the repo, stable across
     branches). In-repo .gmail/ locations also work as fallbacks (see _CANDIDATE_DIRS).
"""
import base64, os, sys
from email.mime.text import MIMEText

HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
# gmail.send to send; gmail.readonly for bounce_scan.py (the circuit-breaker's bounce/opt-out
# signal). The token is granted both at the one-time go-live consent (see pipeline/go.sh).
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.send",
                "https://www.googleapis.com/auth/gmail.readonly"]

# Credentials live OUTSIDE the repo tree by default (stable across worktrees/branches, never
# committable). Searched in order; first dir containing client_secret.json wins. The token is
# written next to the secret. Set OUTREACH_GMAIL_DIR to override.
_CANDIDATE_DIRS = [
    os.environ.get("OUTREACH_GMAIL_DIR", ""),
    # OWN credential dir, deliberately not ~/.config/outreach-engine. go.sh does `rm -f $TOKEN` on
    # re-consent; sharing the dir would delete the sibling campaign's token and leave its headless
    # loop blocked forever in run_local_server(). Same client_secret.json, same address, own token.
    os.path.expanduser("~/.config/outreach-engine"),
    os.path.join(ROOT, "outreach", ".gmail"),   # in-repo fallbacks (gitignored)
    os.path.join(ROOT, ".gmail"),
]

def _gmail_dir():
    for d in _CANDIDATE_DIRS:
        if d and os.path.exists(os.path.join(d, "client_secret.json")):
            return d
    return os.path.expanduser("~/.config/outreach-engine")  # default target for first-time setup

GMAIL_DIR = _gmail_dir()
CLIENT_SECRET = os.path.join(GMAIL_DIR, "client_secret.json")
TOKEN = os.path.join(GMAIL_DIR, "token.json")


def _creds():
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from google.auth.transport.requests import Request
    creds = None
    if os.path.exists(TOKEN):
        creds = Credentials.from_authorized_user_file(TOKEN, GMAIL_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists(CLIENT_SECRET):
                sys.exit(f"missing {os.path.relpath(CLIENT_SECRET, ROOT)} - do the Google Cloud setup "
                         f"in gmail_auth.py's docstring first.")
            flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRET, GMAIL_SCOPES)
            creds = flow.run_local_server(port=0)  # opens a browser once
        os.makedirs(GMAIL_DIR, exist_ok=True)
        with open(TOKEN, "w") as f:
            f.write(creds.to_json())
        os.chmod(TOKEN, 0o600)
    return creds


def token_ok(require_scope=None):
    """True iff the stored token exists, carries require_scope (if given), AND is valid or can be
    SILENTLY refreshed. False on missing / wrong-scope / expired-and-unrefreshable (invalid_grant:
    expired or revoked). NEVER opens a browser — lets go.sh decide whether to re-consent WITHOUT
    the scope-string false-positive that let a dead-but-scoped token slip through (2026-07-16)."""
    import json
    # pure checks first (no google deps) so a missing/wrong-scope token is decidable offline:
    if not os.path.exists(TOKEN):
        return False
    try:
        scopes = json.load(open(TOKEN)).get("scopes", [])
    except Exception:
        return False
    if require_scope and require_scope not in scopes:
        return False
    import warnings; warnings.filterwarnings("ignore")
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    try:
        creds = Credentials.from_authorized_user_file(TOKEN, GMAIL_SCOPES)
    except Exception:
        return False
    if creds and creds.valid:
        return True
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())                       # raises on invalid_grant (expired/revoked)
            with open(TOKEN, "w") as f: f.write(creds.to_json())   # persist the fresh access token
            os.chmod(TOKEN, 0o600)
            return True
        except Exception:
            return False
    return False


def service():
    """Authed Gmail API client. Triggers the one-time browser consent if needed."""
    import warnings; warnings.filterwarnings("ignore")
    from googleapiclient.discovery import build
    return build("gmail", "v1", credentials=_creds(), cache_discovery=False)


def send(svc, to, subject, body_text, from_addr=None):
    """Send a plain-text email. Returns the Gmail message id. Raises on API error."""
    msg = MIMEText(body_text, "plain", "utf-8")
    msg["to"] = to
    msg["subject"] = subject
    if from_addr:
        msg["from"] = from_addr
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    sent = svc.users().messages().send(userId="me", body={"raw": raw}).execute()
    return sent.get("id")


def whoami(svc):
    """Best-effort mailbox address. Returns None under the send-only scope (getProfile needs
    gmail.readonly). Informational only — sending uses userId='me' and never needs this."""
    try:
        return svc.users().getProfile(userId="me").execute().get("emailAddress")
    except Exception:
        return None


if __name__ == "__main__":
    s = service()
    who = whoami(s)
    print(f"authed as: {who or '(send-only scope — profile read not permitted, which is fine)'}")
    print(f"scopes: {GMAIL_SCOPES}")
    print("auth OK - token stored. Run test_send.py to send yourself a test.")
