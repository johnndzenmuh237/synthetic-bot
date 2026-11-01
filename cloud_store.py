"""cloud_store.py — permanent copy of accounts, settings and the full trade/activity
history in Firebase Realtime Database.

Why: the bot's working database is a local SQLite file. On Render's free plan that file is
erased on every deploy/restart, which deleted accounts and history. This module mirrors every
change to Firebase and, on start-up, restores whatever the local file is missing. The
local database is still what the bot reads/writes (nothing about trading changes);
Firebase is the permanent copy behind it.

It talks to Firebase over plain HTTPS (REST), so no extra heavy SDK is needed.

Credentials (never in code; set them in Render → Environment):
  FIREBASE_SERVICE_ACCOUNT   the service-account JSON (one line, or base64 of it)   [recommended]
  FIREBASE_DB_SECRET         legacy database secret                                  [alternative]
With neither set, cloud sync is simply OFF and the bot behaves exactly as before.

Everything is stored under one root node (FIREBASE_ROOT, default "synthbot"), never at the
top level, so other apps sharing the same Firebase project are untouched.
Data layout under that root (keys carry a letter prefix so Firebase never turns them into arrays):
  users/u<id>   settings/u<id>   bot_state/u<id>   trades/u<uid>/t<id>   activity/u<uid>/a<id>
"""
import base64
import json
import queue
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from config import FIREBASE_DATABASE_URL, FIREBASE_SERVICE_ACCOUNT, FIREBASE_DB_SECRET, FIREBASE_ROOT
from logger import log

_SCOPES = ("https://www.googleapis.com/auth/firebase.database "
           "https://www.googleapis.com/auth/userinfo.email")
TIMEOUT = 10


class CloudError(Exception):
    pass


class _Auth:
    """Gives every request its credential: a database secret, or a short-lived
    OAuth token minted from the service account (signed with its private key)."""

    def __init__(self, service_account, secret):
        self.secret = secret
        self.sa = None
        self._token = ""
        self._exp = 0.0
        self._lock = threading.Lock()
        if service_account:
            raw = service_account.strip()
            if not raw.startswith("{"):
                raw = base64.b64decode(raw).decode("utf-8")
            self.sa = json.loads(raw)
            for k in ("client_email", "private_key"):
                if not self.sa.get(k):
                    raise CloudError("service account JSON is missing '%s'" % k)

    @property
    def configured(self):
        return bool(self.sa or self.secret)

    @staticmethod
    def _b64(b):
        return base64.urlsafe_b64encode(b).rstrip(b"=")

    def make_jwt(self, now=None):
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding
        now = int(now or time.time())
        header = {"alg": "RS256", "typ": "JWT"}
        claims = {"iss": self.sa["client_email"], "scope": _SCOPES,
                  "aud": self.sa.get("token_uri") or "https://oauth2.googleapis.com/token",
                  "iat": now, "exp": now + 3600}
        signing_input = self._b64(json.dumps(header).encode()) + b"." + self._b64(json.dumps(claims).encode())
        key = serialization.load_pem_private_key(self.sa["private_key"].encode(), password=None)
        sig = key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
        return (signing_input + b"." + self._b64(sig)).decode()

    def _fetch_token(self):
        body = urllib.parse.urlencode({
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "assertion": self.make_jwt()}).encode()
        req = urllib.request.Request(self.sa.get("token_uri") or "https://oauth2.googleapis.com/token",
                                     data=body, method="POST")
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            data = json.loads(r.read().decode())
        self._token = data["access_token"]
        self._exp = time.time() + int(data.get("expires_in", 3600)) - 300

    def apply(self, url, headers):
        """Returns the url (with ?auth= for a secret) and fills in the Authorization header."""
        if self.sa:
            with self._lock:
                if not self._token or time.time() >= self._exp:
                    self._fetch_token()
                headers["Authorization"] = "Bearer " + self._token
            return url
        sep = "&" if "?" in url else "?"
        return url + sep + "auth=" + urllib.parse.quote(self.secret)


class CloudStore:
    def __init__(self, base_url, service_account="", secret="", root=FIREBASE_ROOT):
        self.base = (base_url or "").rstrip("/")
        self.root = (root or "").strip("/")
        self.auth = None
        self.error = ""
        try:
            self.auth = _Auth(service_account, secret)
        except Exception as e:                       # bad JSON / bad base64 / missing fields
            self.error = "credential unreadable: %s" % e
            log.error("Firebase credential could not be read (%s). Cloud sync is OFF.", e)
        self.enabled = bool(self.base and self.auth and self.auth.configured)
        self.restored = False                        # True once the start-up restore has succeeded
        self._q = queue.Queue()
        self._pending = set()
        self._plock = threading.Lock()
        self._reader = None                          # fn(kind, key) -> (path, value|None)
        self._thread = None
        self._wake = threading.Event()
        self.synced = 0
        self.failed = 0

    # ── transport ────────────────────────────────────────────
    def _call(self, method, path, body=None, query=""):
        full = "/".join(x for x in (self.root, path.strip("/")) if x)
        url = "%s/%s.json%s" % (self.base, full, ("?" + query) if query else "")
        headers = {"Content-Type": "application/json"}
        url = self.auth.apply(url, headers)
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                txt = r.read().decode() or "null"
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode()[:200]
            except Exception:
                pass
            raise CloudError("Firebase HTTP %s %s" % (e.code, detail))
        except Exception as e:
            raise CloudError("Firebase unreachable: %s" % e)
        return json.loads(txt)

    def get(self, path, query=""):
        return self._call("GET", path, None, query)

    def put(self, path, value):
        return self._call("PUT", path, value)

    def delete(self, path):
        return self._call("DELETE", path)

    # ── status ───────────────────────────────────────────────
    def ready(self):
        """True when it is safe to create new accounts: either cloud sync is off,
        or the restore of existing accounts has finished."""
        return (not self.enabled) or self.restored

    def status(self):
        return {"enabled": self.enabled, "restored": self.restored, "queued": len(self._pending),
                "synced": self.synced, "failed": self.failed, "error": self.error}

    # ── background sync of single rows ───────────────────────
    def set_reader(self, fn):
        self._reader = fn

    def push(self, kind, key):
        """Queue 'send the current local copy of this row to Firebase' (non-blocking)."""
        if not self.enabled:
            return
        item = (kind, key)
        with self._plock:
            if item in self._pending:
                self._wake.set()
                return
            self._pending.add(item)
        self._q.put(item)
        self._ensure_thread()

    def _ensure_thread(self):
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._run, name="cloud-sync", daemon=True)
            self._thread.start()

    def _send(self, kind, key):
        path, value = self._reader(kind, key)
        if path is None:
            return
        if value is None:
            self.delete(path)
            return
        if kind == "user":                    # never overwrite a DIFFERENT account that owns this id
            existing = self.get(path)
            if existing and existing.get("username") != value.get("username"):
                log.error("Cloud sync: account id %s already belongs to '%s' in Firebase; "
                          "not overwriting it with '%s'.", key, existing.get("username"), value.get("username"))
                return
        self.put(path, value)

    def _run(self):
        backoff = 2
        while True:
            item = self._q.get()
            while not self.restored:              # never write before the restore has read the cloud
                time.sleep(1)
            try:
                self._send(*item)
                with self._plock:
                    self._pending.discard(item)
                self.synced += 1
                backoff = 2
            except Exception as e:
                self.failed += 1
                log.warning("Cloud sync of %s failed (%s) — will retry.", item, e)
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)
                self._q.put(item)                 # stays in _pending; retried until it works

    def flush(self, timeout=10):
        """Wait until the queue is empty (used by tests and on shutdown)."""
        end = time.time() + timeout
        while time.time() < end:
            with self._plock:
                if not self._pending:
                    return True
            time.sleep(0.05)
        return False


store = CloudStore(FIREBASE_DATABASE_URL, FIREBASE_SERVICE_ACCOUNT, FIREBASE_DB_SECRET)