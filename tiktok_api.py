"""TikTok Shop Open Platform client for AffiliateFinder's own dedicated app.

Mirrors serigama-command-center lib/ingestion/tiktok/{signer,oauth,client}.ts.
This app holds ONLY seller.creator_marketplace.read — its credentials live in
tokens.json (gitignored, 0600) and its refresh-token chain is owned solely by
this process. TikTok ROTATES AND CONSUMES the refresh token on every refresh:
refresh is single-flight, and rotated tokens are persisted atomically BEFORE
the new access token is used.
"""
import hashlib, hmac, json, os, threading, time
from pathlib import Path

import requests

APP_DIR = Path(__file__).parent
TOKENS_JSON = APP_DIR / "tokens.json"
BASE_URL = "https://open-api.tiktokglobalshop.com"
AUTH_HOST = "https://auth.tiktok-shops.com"
SEARCH_PATH = "/affiliate_seller/202508/marketplace_creators/search"
REFRESH_WINDOW_SEC = 300
BOOTSTRAP_HINT = "run ./venv/bin/python bootstrap_auth.py --help and follow the README's Auth section"

_EXCLUDED_KEYS = {"sign", "access_token", "x-tts-access-token"}
_ABS_EPOCH_THRESHOLD = 1e9  # *_expire_in ships as seconds-remaining OR absolute epoch


class AuthError(RuntimeError):
    pass


class ApiError(RuntimeError):
    def __init__(self, code, message, request_id=""):
        super().__init__(f"TikTok error {code}: {message} (request_id={request_id or 'n/a'})")
        self.code = code


def build_base_string(path, query, body=""):
    s = path
    for k in sorted(k for k in query if k not in _EXCLUDED_KEYS):
        s += f"{k}{query[k]}"
    return s + (body or "")


def sign(app_secret, path, query, body=""):
    if not app_secret:
        raise AuthError("app_secret is empty")
    wrapped = f"{app_secret}{build_base_string(path, query, body)}{app_secret}"
    return hmac.new(app_secret.encode(), wrapped.encode(), hashlib.sha256).hexdigest()


def resolve_expiry(expire_in, now=None):
    now = time.time() if now is None else now
    return float(expire_in) if expire_in >= _ABS_EPOCH_THRESHOLD else now + float(expire_in)


_refresh_lock = threading.Lock()


def load_tokens():
    if not TOKENS_JSON.exists():
        raise AuthError(f"tokens.json is missing — {BOOTSTRAP_HINT}")
    try:
        return json.loads(TOKENS_JSON.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        raise AuthError(f"tokens.json is unreadable ({e}) — {BOOTSTRAP_HINT}") from e


def save_tokens(t):
    tmp = TOKENS_JSON.with_suffix(".json.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(json.dumps(t, indent=2))
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, TOKENS_JSON)
    except OSError as e:
        raise AuthError(
            "PERSISTING ROTATED TOKENS FAILED — the credential TikTok just issued is "
            "single-use and is now the ONLY valid one. Fix the disk problem, then "
            f"re-bootstrap: {BOOTSTRAP_HINT}. ({e})") from e


def _fresh(t):
    return t.get("access_token") and t.get("access_token_expires_at", 0) > time.time() + REFRESH_WINDOW_SEC


def get_access_token():
    t = load_tokens()
    if _fresh(t):
        return t
    with _refresh_lock:
        t = load_tokens()          # another thread may have refreshed while we waited
        if _fresh(t):
            return t
        return _refresh(t)


def _refresh(t):
    try:
        r = requests.get(f"{AUTH_HOST}/api/v2/token/refresh", params={
            "app_key": t["app_key"], "app_secret": t["app_secret"],
            "refresh_token": t["refresh_token"], "grant_type": "refresh_token",
        }, timeout=30)
    except requests.RequestException as e:
        raise AuthError(f"token refresh failed: network error ({e})") from e
    if r.status_code != 200:
        raise AuthError(f"token refresh failed: HTTP {r.status_code}")
    try:
        j = r.json()
    except ValueError as e:
        raise AuthError(f"token refresh returned a non-JSON response: {e}") from e
    d = j.get("data") or {}
    if j.get("code") != 0 or not d.get("access_token") or not d.get("refresh_token"):
        raise AuthError(
            f"token refresh rejected (code={j.get('code')}: {j.get('message', '')}) — "
            f"if the refresh token expired or was consumed elsewhere, {BOOTSTRAP_HINT}")
    t = {**t,
         "access_token": d["access_token"],
         "access_token_expires_at": resolve_expiry(d.get("access_token_expire_in", 6 * 3600)),
         "refresh_token": d["refresh_token"],
         "refresh_token_expires_at": resolve_expiry(d.get("refresh_token_expire_in", 365 * 24 * 3600))}
    save_tokens(t)                 # atomic, BEFORE the new token is used (spec F7)
    return t


def call_tiktok(method, path, query=None, body=None, _retry_auth=True):
    t = get_access_token()
    q = {"app_key": t["app_key"], "timestamp": int(time.time()), **(query or {})}
    if t.get("shop_cipher") and not path.startswith("/authorization"):
        q["shop_cipher"] = t["shop_cipher"]
    body_str = json.dumps(body) if (method != "GET" and body is not None) else ""
    q["sign"] = sign(t["app_secret"], path, q, body_str)

    resp = None
    network_err = None
    for attempt in range(3):
        try:
            resp = requests.request(method, f"{BASE_URL}{path}", params=q,
                                    headers={"x-tts-access-token": t["access_token"],
                                             "content-type": "application/json"},
                                    data=body_str or None, timeout=30)
        except requests.RequestException as e:
            network_err = e
            resp = None
            if attempt < 2:
                time.sleep(1.5 * (attempt + 1))
                continue
            raise ApiError(-1, f"network error after retries: {e}") from e
        network_err = None
        if resp.status_code == 429 or resp.status_code >= 500:
            if attempt < 2:
                time.sleep(1.5 * (attempt + 1))
            continue
        try:
            j = resp.json()
        except ValueError as e:
            raise ApiError(-1, f"non-JSON response (HTTP {resp.status_code}): "
                                f"{resp.text[:200]}") from e
        if j.get("code") == 0:
            return j.get("data") or {}
        code = j.get("code", -1)
        # 105xxx = auth-error domain: token rejected server-side. Force ONE
        # refresh + retry. 105005 is a SCOPE gap — refreshing can't fix it.
        if 105000 <= code < 106000 and code != 105005 and _retry_auth:
            with _refresh_lock:
                current = load_tokens()
                # Only rotate if nobody else already did: the refresh token is
                # single-use, and N concurrent 105xxx failures need ONE rotation.
                if current.get("access_token") == t["access_token"]:
                    _refresh(current)
            return call_tiktok(method, path, query, body, _retry_auth=False)
        raise ApiError(code, j.get("message", ""), j.get("request_id", ""))
    # Only reachable via the 429/5xx branch on the final attempt (network
    # errors raise immediately above), but stay defensive: resp could in
    # principle be None here, so never assume it has a status_code.
    if resp is None:
        raise ApiError(-1, f"network error after retries: {network_err}")
    raise ApiError(-1, f"gave up after retries (last HTTP {resp.status_code})")


def search_creators_page(body, page_token=""):
    query = {"page_size": 20}
    if page_token:
        query["page_token"] = page_token
    return call_tiktok("POST", SEARCH_PATH, query=query, body=body)
