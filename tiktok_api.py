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
