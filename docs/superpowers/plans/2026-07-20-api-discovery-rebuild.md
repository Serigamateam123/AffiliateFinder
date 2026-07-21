# API Discovery Rebuild Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace AffiliateFinder's scraper-based harvest with direct calls to the official TikTok Creator Marketplace search API via a dedicated minimal-scope app, deduped against Nurin's outreach Google Sheet.

**Architecture:** A new `tiktok_api.py` (HMAC signing + self-owned token chain with single-flight refresh and atomic rotation persistence) feeds `discovery.py` (bucket mapping, conformance checks, sheet+store dedupe), surfaced through a synchronous `/api/discover` endpoint and a Discover button in the existing dashboard. The Playwright scraper retires; the Playwright DM pre-fill (`messenger.py`) is untouched. Spec: `docs/superpowers/specs/2026-07-20-api-discovery-rebuild.md` (rev 3, approved).

**Tech Stack:** Python 3.10+ (macOS), Flask, `requests`, `google-auth` (service-account token only; Sheets is called via plain REST), pytest for tests. No new infrastructure.

## Global Constraints

- Repo: `/Users/justin/Documents/Claude/Projects/AffiliateFinder`, work on branch `api-discovery`, commit at the end of every task.
- All failures are **loud and fail-closed** (spec §9): no silent empty results, no guessed defaults, no unfiltered results shown as filtered.
- The search API **silently ignores unknown body fields** (verified live 2026-07-20). Request bodies may only be built from the verified-filter registry, and GMV results must be conformance-checked every page.
- TikTok **rotates and consumes the refresh token on every refresh**: refresh must be single-flight, and rotated tokens must be persisted atomically **before** the new access token is used.
- Secrets (`tokens.json`, `service_account.json`, `config.json`) are gitignored, written `0600`, and never printed unredacted.
- API constants: host `https://open-api.tiktokglobalshop.com`, auth host `https://auth.tiktok-shops.com`, search path `/affiliate_seller/202508/marketplace_creators/search`, `page_size` 20, region MY, amounts treated as RM.
- `messenger.py` and the DM flow must not change in any task.
- Placeholder criteria defaults (min_followers 5000, min_gmv 1000, min_units_sold 100) are **seed values pending O2** — marked as such in `config.example.json`; server-side `category` filtering is **deferred pending O4** (unverified body shape → would be silently ignored; criteria with non-empty `category_ids` are rejected, fail-closed).
- Tests run with `./venv/bin/python -m pytest -q` from the repo root; every task's tests must pass before its commit.

---

### Task 1: Test scaffolding + fail-closed store (`store.py`)

**Files:**
- Create: `store.py`, `tests/test_store.py`, `requirements-dev.txt`, `.gitignore`
- Modify: `ui_server.py` (store wiring, CORS hook removal), `requirements.txt`

**Interfaces:**
- Produces: `store.StoreError(RuntimeError)`; `store.load_creators() -> list[dict]`; `store.save_creators(rows) -> None` (atomic); `store.upsert_creators(rows: list[dict]) -> dict` returning `{"added": int, "updated": int, "total": int}` — rows are already in final shape, keyed by `handle`; on update, values that are `None`, `""`, or numeric `0` (bools exempt) never overwrite; `store.set_user_id(handle, user_id) -> None`. Module global `store.DATA_FILE: Path` (monkeypatchable in tests).

- [ ] **Step 1: Branch + venv + deps**

```bash
cd /Users/justin/Documents/Claude/Projects/AffiliateFinder
git checkout -b api-discovery
printf 'flask\nplaywright\nrequests\ngoogle-auth\n' > requirements.txt
printf 'pytest\n' > requirements-dev.txt
python3 -m venv venv 2>/dev/null; ./venv/bin/pip install -q -r requirements.txt -r requirements-dev.txt
```

- [ ] **Step 2: .gitignore**

```bash
cat > .gitignore <<'EOF'
venv/
__pycache__/
*.pyc
browser_profile/
browser_profile_dm/
tokens.json
service_account.json
config.json
EOF
```

- [ ] **Step 3: Write the failing tests**

```python
# tests/test_store.py
import json, threading
import pytest
import store


@pytest.fixture(autouse=True)
def tmp_store(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DATA_FILE", tmp_path / "creators.json")


def test_load_missing_file_is_empty():
    assert store.load_creators() == []


def test_load_corrupt_file_fails_closed():
    store.DATA_FILE.write_text("{not json", encoding="utf-8")
    with pytest.raises(store.StoreError, match="refusing"):
        store.load_creators()


def test_save_is_atomic_and_roundtrips():
    store.save_creators([{"handle": "a"}])
    assert store.load_creators() == [{"handle": "a"}]
    assert not store.DATA_FILE.with_suffix(".json.tmp").exists()


def test_upsert_adds_and_updates():
    r = store.upsert_creators([{"handle": "a", "gmv": 5.0}])
    assert (r["added"], r["total"]) == (1, 1)
    r = store.upsert_creators([{"handle": "a", "gmv": 9.0}, {"handle": "b", "gmv": 1.0}])
    assert (r["added"], r["updated"]) == (1, 1)
    rows = {x["handle"]: x for x in store.load_creators()}
    assert rows["a"]["gmv"] == 9.0


def test_update_never_clobbers_with_empty_or_zero():
    store.upsert_creators([{"handle": "a", "gmv": 9.0, "nickname": "Sha",
                            "tiktok_user_id": "u1", "gmv_is_floor": True}])
    store.upsert_creators([{"handle": "a", "gmv": 0, "nickname": "",
                            "tiktok_user_id": "", "gmv_is_floor": False,
                            "items_sold": None}])
    row = store.load_creators()[0]
    assert row["gmv"] == 9.0 and row["nickname"] == "Sha"
    assert row["tiktok_user_id"] == "u1"
    assert row["gmv_is_floor"] is False        # bools are meaningful, always applied
    assert "items_sold" not in row             # None never lands


def test_set_user_id():
    store.upsert_creators([{"handle": "a"}])
    store.set_user_id("@a", "12345")
    assert store.load_creators()[0]["tiktok_user_id"] == "12345"
```

- [ ] **Step 4: Run to verify failure**

Run: `./venv/bin/python -m pytest tests/test_store.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'store'`

- [ ] **Step 5: Implement `store.py`**

```python
"""creators.json store: locked, atomic, fail-closed.

Every read/write goes through here. A corrupt file is an ERROR, never an
empty list (the old behavior silently wiped the pool). Saves are atomic
(temp + os.replace) so a crash can never truncate the store.
"""
import json, os, threading
from pathlib import Path

DATA_FILE = Path(__file__).parent / "creators.json"
_lock = threading.Lock()


class StoreError(RuntimeError):
    pass


def _load():
    if not DATA_FILE.exists():
        return []
    try:
        return json.loads(DATA_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        raise StoreError(
            f"{DATA_FILE.name} is unreadable ({e}) — refusing to treat it as empty. "
            "Fix or move the file, then reload."
        ) from e


def _save(rows):
    tmp = DATA_FILE.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, DATA_FILE)
    except OSError as e:
        raise StoreError(f"could not write {DATA_FILE.name}: {e}") from e


def load_creators():
    with _lock:
        return _load()


def save_creators(rows):
    with _lock:
        _save(rows)


def _keep(value):
    """On update: empty strings, None and numeric zero are parse/absence
    defaults, never real data — they must not overwrite. Bools are real."""
    if value is None or value == "":
        return False
    if isinstance(value, bool):
        return True
    if isinstance(value, (int, float)) and value == 0:
        return False
    return True


def upsert_creators(incoming):
    with _lock:
        existing = {r["handle"]: r for r in _load()}
        added = updated = 0
        for row in incoming:
            handle = row.get("handle", "")
            if not handle:
                continue
            if handle in existing:
                existing[handle].update({k: v for k, v in row.items() if _keep(v)})
                updated += 1
            else:
                existing[handle] = {k: v for k, v in row.items() if v is not None}
                added += 1
        _save(list(existing.values()))
        return {"added": added, "updated": updated, "total": len(existing)}


def set_user_id(handle, user_id):
    if not user_id:
        return
    handle = str(handle).strip().lstrip("@")
    with _lock:
        rows = _load()
        for row in rows:
            if row.get("handle") == handle:
                row["tiktok_user_id"] = user_id
                _save(rows)
                return
```

- [ ] **Step 6: Run to verify pass**

Run: `./venv/bin/python -m pytest tests/test_store.py -q`
Expected: 6 passed

- [ ] **Step 7: Wire `ui_server.py` to the store and remove the CORS hook**

In `ui_server.py`:

1. Delete the whole `allow_scraper_origin` after_request function (lines 57–68) — the harvest posts in-process; the grant only let scripts on `affiliate.tiktok.com` wipe the store.
2. Delete the local `load_creators` / `save_creators` definitions (lines 72–82) and add `import store` plus `from store import load_creators, save_creators` below the existing imports.
3. Replace the body of `put_creators()` (keep `normalize()` as-is — it serves the CSV path):

```python
@app.post("/api/creators")
def put_creators():
    """Upsert by handle. Accepts a bare list or {"creators": [...]}."""
    payload = request.get_json(silent=True) or {}
    incoming = payload if isinstance(payload, list) else payload.get("creators", [])
    if not isinstance(incoming, list):
        return jsonify({"error": "expected a list of creator rows"}), 400
    rows = [normalize(r) for r in incoming]
    skipped = sum(1 for r in rows if not r["handle"])
    result = store.upsert_creators([r for r in rows if r["handle"]])
    return jsonify({"added": result["added"], "updated": result["updated"],
                    "skipped": skipped, "total": result["total"]})
```

4. Replace the body of `_cache_user_id(handle, user_id)` with `store.set_user_id(handle, user_id)`.
5. Add a store error handler next to the routes:

```python
@app.errorhandler(store.StoreError)
def store_error(e):
    return jsonify({"error": str(e)}), 500
```

- [ ] **Step 8: Full test run + smoke**

Run: `./venv/bin/python -m pytest -q` → all pass.
Run: `./venv/bin/python -c "import ui_server"` → no output (imports clean).

- [ ] **Step 9: Commit**

```bash
git add -A && git commit -m "feat: fail-closed atomic creator store; remove tiktok.com CORS grant"
```

---

### Task 2: XSS escaping + duplicate clearAll fix (`ui.html`)

**Files:**
- Modify: `ui.html:316-372` (syncCategories + render), `ui.html:540-550` (clearAll)

**Interfaces:**
- Produces: JS helper `esc(s)` — HTML-escapes `& < > " '`; used by all later UI tasks.

- [ ] **Step 1: Add the escaper and use it everywhere scraped text hits the DOM**

At the top of the `<script>` block (after `let current = [];`):

```js
const esc = s => String(s ?? "").replace(/[&<>"']/g,
  ch => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[ch]));
```

In `syncCategories`, replace the `sel.innerHTML` assignment:

```js
  sel.innerHTML = '<option value="">All categories</option>' +
    cats.map(c => `<option value="${esc(c)}">${esc(c)}</option>`).join("");
```

In `render`, replace the `name` construction and the `tr.innerHTML` template so every creator-controlled value passes through `esc()`:

```js
    const name = c.profile_url
      ? `<a href="${esc(c.profile_url)}" target="_blank" rel="noopener">@${esc(c.handle)}</a>`
      : "@" + esc(c.handle);
```

and inside the template literal: `${name}${c.nickname ? ` <span class="nick">${esc(c.nickname)}</span>` : ""}`, `${esc(c.category) || "—"}`, and `data-handle="${esc(c.handle)}"`.

- [ ] **Step 2: Delete the second `clearAll` definition**

Remove the duplicate block (the second of the two identical `async function clearAll()` definitions at lines 540–550), keeping exactly one.

- [ ] **Step 3: Verify**

Run: `grep -c "function clearAll" ui.html` → `1`
Run: `grep -n 'esc(c.handle)' ui.html` → at least 2 matches.
Manual: `./venv/bin/python ui_server.py`, load http://localhost:7374, confirm the table renders and Filter loaded works, then stop the server.

- [ ] **Step 4: Commit**

```bash
git add ui.html && git commit -m "fix: escape scraped text in DOM rendering; drop duplicate clearAll"
```

---

### Task 3: Request signing (`tiktok_api.py`, pure part)

**Files:**
- Create: `tiktok_api.py`, `tests/test_signing.py`

**Interfaces:**
- Produces: `tiktok_api.build_base_string(path, query: dict, body: str) -> str`; `tiktok_api.sign(app_secret, path, query, body="") -> str` (lowercase hex); `tiktok_api.resolve_expiry(expire_in, now=None) -> float` (epoch seconds; values ≥ 1e9 are absolute epochs, else seconds-remaining); exceptions `AuthError`, `ApiError` (with `.code`).
- Algorithm (mirrors command-center `signer.ts`): drop `sign`/`access_token`/`x-tts-access-token` from query → sort keys → concat `{key}{value}` → prepend path → append raw JSON body → wrap `secret + base + secret` → HMAC-SHA256 keyed by secret, hex.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_signing.py
import hashlib, hmac
import pytest
import tiktok_api as t


def test_base_string_sorts_and_excludes():
    base = t.build_base_string(
        "/x", {"b": "2", "a": 1, "sign": "junk", "access_token": "junk"}, '{"k":"v"}')
    assert base == '/xa1b2{"k":"v"}'


def test_sign_matches_hand_computed_hmac():
    secret = "s3cret"
    base = '/xa1b2{"k":"v"}'                      # same inputs as above, derived by hand
    expected = hmac.new(secret.encode(), f"{secret}{base}{secret}".encode(),
                        hashlib.sha256).hexdigest()
    assert t.sign(secret, "/x", {"b": "2", "a": 1, "sign": "junk"}, '{"k":"v"}') == expected


def test_sign_requires_secret():
    with pytest.raises(t.AuthError):
        t.sign("", "/x", {})


def test_resolve_expiry_handles_both_conventions():
    assert t.resolve_expiry(1_800_000_000) == 1_800_000_000       # absolute epoch
    assert t.resolve_expiry(3600, now=1000.0) == 4600.0           # seconds remaining
```

- [ ] **Step 2: Run to verify failure**

Run: `./venv/bin/python -m pytest tests/test_signing.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'tiktok_api'`

- [ ] **Step 3: Implement the pure part of `tiktok_api.py`**

```python
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
```

- [ ] **Step 4: Run to verify pass**

Run: `./venv/bin/python -m pytest tests/test_signing.py -q`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add tiktok_api.py tests/test_signing.py && git commit -m "feat: TikTok request signing (mirrors command-center signer)"
```

---

### Task 4: Token store, single-flight refresh, API caller (`tiktok_api.py`)

**Files:**
- Modify: `tiktok_api.py` (append)
- Test: `tests/test_tokens.py`

**Interfaces:**
- Consumes: Task 3's `sign`, `resolve_expiry`, `AuthError`, `ApiError`.
- Produces: `load_tokens() -> dict`; `save_tokens(t) -> None` (atomic, 0600); `get_access_token() -> dict` (full token record, refreshed if within 5 min of expiry, single-flight); `call_tiktok(method, path, query=None, body=None) -> dict` (signed call, envelope-checked, one forced refresh+retry on 105xxx except 105005); `search_creators_page(body, page_token="") -> dict`. Token record keys: `app_key, app_secret, access_token, access_token_expires_at, refresh_token, refresh_token_expires_at, shop_cipher, region`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_tokens.py
import json, threading, time
import pytest
import tiktok_api as t


def seed(tmp_path, monkeypatch, expires_in=-10):
    monkeypatch.setattr(t, "TOKENS_JSON", tmp_path / "tokens.json")
    t.save_tokens({"app_key": "k", "app_secret": "s",
                   "access_token": "old", "access_token_expires_at": time.time() + expires_in,
                   "refresh_token": "r0", "refresh_token_expires_at": time.time() + 9e7,
                   "shop_cipher": "cip", "region": "MY"})


class FakeResp:
    def __init__(self, payload, status=200):
        self._p, self.status_code = payload, status
    def json(self):
        return self._p


def test_missing_tokens_is_loud(tmp_path, monkeypatch):
    monkeypatch.setattr(t, "TOKENS_JSON", tmp_path / "nope.json")
    with pytest.raises(t.AuthError, match="bootstrap"):
        t.load_tokens()


def test_refresh_rotates_and_persists_before_returning(tmp_path, monkeypatch):
    seed(tmp_path, monkeypatch)
    monkeypatch.setattr(t.requests, "get", lambda *a, **kw: FakeResp(
        {"code": 0, "data": {"access_token": "new", "access_token_expire_in": 7200,
                             "refresh_token": "r1", "refresh_token_expire_in": 3.15e7}}))
    tok = t.get_access_token()
    assert tok["access_token"] == "new"
    on_disk = json.loads(t.TOKENS_JSON.read_text())
    assert on_disk["refresh_token"] == "r1"          # rotation persisted


def test_refresh_persist_failure_is_blocking(tmp_path, monkeypatch):
    seed(tmp_path, monkeypatch)
    monkeypatch.setattr(t.requests, "get", lambda *a, **kw: FakeResp(
        {"code": 0, "data": {"access_token": "new", "access_token_expire_in": 7200,
                             "refresh_token": "r1"}}))
    def boom(_):
        raise t.AuthError("PERSISTING ROTATED TOKENS FAILED")
    monkeypatch.setattr(t, "save_tokens", boom)
    with pytest.raises(t.AuthError, match="PERSISTING"):
        t.get_access_token()


def test_refresh_rejection_is_loud(tmp_path, monkeypatch):
    seed(tmp_path, monkeypatch)
    monkeypatch.setattr(t.requests, "get", lambda *a, **kw: FakeResp(
        {"code": 105002, "message": "refresh token expired"}))
    with pytest.raises(t.AuthError, match="bootstrap"):
        t.get_access_token()


def test_refresh_is_single_flight(tmp_path, monkeypatch):
    seed(tmp_path, monkeypatch)
    calls = []
    def slow_refresh(*a, **kw):
        calls.append(1)
        time.sleep(0.2)
        return FakeResp({"code": 0, "data": {"access_token": "new",
                         "access_token_expire_in": 7200, "refresh_token": "r1"}})
    monkeypatch.setattr(t.requests, "get", slow_refresh)
    threads = [threading.Thread(target=t.get_access_token) for _ in range(4)]
    [x.start() for x in threads]; [x.join() for x in threads]
    assert len(calls) == 1


def test_call_tiktok_attaches_cipher_and_checks_envelope(tmp_path, monkeypatch):
    seed(tmp_path, monkeypatch, expires_in=9999)
    seen = {}
    def fake_request(method, url, params=None, headers=None, data=None, timeout=None):
        seen.update(params=params, headers=headers, url=url)
        return FakeResp({"code": 0, "data": {"creators": []}})
    monkeypatch.setattr(t.requests, "request", fake_request)
    out = t.call_tiktok("POST", t.SEARCH_PATH, query={"page_size": 20}, body={})
    assert out == {"creators": []}
    assert seen["params"]["shop_cipher"] == "cip" and "sign" in seen["params"]
    assert seen["headers"]["x-tts-access-token"] == "old"


def test_call_tiktok_raises_apierror_with_code(tmp_path, monkeypatch):
    seed(tmp_path, monkeypatch, expires_in=9999)
    monkeypatch.setattr(t.requests, "request", lambda *a, **kw: FakeResp(
        {"code": 45101004, "message": "quota reached", "request_id": "rid"}))
    with pytest.raises(t.ApiError) as ei:
        t.call_tiktok("POST", t.SEARCH_PATH, body={})
    assert ei.value.code == 45101004
```

- [ ] **Step 2: Run to verify failure**

Run: `./venv/bin/python -m pytest tests/test_tokens.py -q`
Expected: FAIL — `AttributeError: module 'tiktok_api' has no attribute 'save_tokens'` (or similar)

- [ ] **Step 3: Append the implementation to `tiktok_api.py`**

```python
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
        tmp.write_text(json.dumps(t, indent=2), encoding="utf-8")
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
    r = requests.get(f"{AUTH_HOST}/api/v2/token/refresh", params={
        "app_key": t["app_key"], "app_secret": t["app_secret"],
        "refresh_token": t["refresh_token"], "grant_type": "refresh_token",
    }, timeout=30)
    if r.status_code != 200:
        raise AuthError(f"token refresh failed: HTTP {r.status_code}")
    j = r.json()
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
    for attempt in range(3):
        resp = requests.request(method, f"{BASE_URL}{path}", params=q,
                                headers={"x-tts-access-token": t["access_token"],
                                         "content-type": "application/json"},
                                data=body_str or None, timeout=30)
        if resp.status_code == 429 or resp.status_code >= 500:
            time.sleep(1.5 * (attempt + 1))
            continue
        j = resp.json()
        if j.get("code") == 0:
            return j.get("data") or {}
        code = j.get("code", -1)
        # 105xxx = auth-error domain: token rejected server-side. Force ONE
        # refresh + retry. 105005 is a SCOPE gap — refreshing can't fix it.
        if 105000 <= code < 106000 and code != 105005 and _retry_auth:
            with _refresh_lock:
                _refresh(load_tokens())
            return call_tiktok(method, path, query, body, _retry_auth=False)
        raise ApiError(code, j.get("message", ""), j.get("request_id", ""))
    raise ApiError(-1, f"gave up after retries (last HTTP {resp.status_code})")


def search_creators_page(body, page_token=""):
    query = {"page_size": 20}
    if page_token:
        query["page_token"] = page_token
    return call_tiktok("POST", SEARCH_PATH, query=query, body=body)
```

- [ ] **Step 4: Run to verify pass**

Run: `./venv/bin/python -m pytest tests/test_tokens.py tests/test_signing.py -q`
Expected: 11 passed

- [ ] **Step 5: Commit**

```bash
git add tiktok_api.py tests/test_tokens.py && git commit -m "feat: token chain (single-flight refresh, atomic rotation persist) + signed API caller"
```

---

### Task 5: One-time auth bootstrap (`bootstrap_auth.py`)

**Files:**
- Create: `bootstrap_auth.py`
- Test: `tests/test_bootstrap.py`

**Interfaces:**
- Consumes: `tiktok_api.save_tokens`, `load_tokens`, `resolve_expiry`, `call_tiktok`, `AuthError`, `AUTH_HOST`.
- Produces: CLI — `bootstrap_auth.py --print-auth-url <service_id>`; `bootstrap_auth.py <auth_code> --app-key K --app-secret S [--shop-cipher C]`; `bootstrap_auth.py --set-shop-cipher C`. Testable function `exchange(auth_code, app_key, app_secret, http_get=requests.get) -> dict` that **raises AuthError without saving when `granted_scopes` is present and lacks `seller.creator_marketplace.read`** (spec F8).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_bootstrap.py
import pytest
import bootstrap_auth as b
import tiktok_api as t


class FakeResp:
    def __init__(self, payload, status=200):
        self._p, self.status_code = payload, status
    def json(self):
        return self._p


def ok_payload(scopes):
    return {"code": 0, "data": {"access_token": "a", "refresh_token": "r",
            "access_token_expire_in": 7200, "refresh_token_expire_in": 3.15e7,
            "granted_scopes": scopes}}


def test_exchange_aborts_without_required_scope():
    with pytest.raises(t.AuthError, match="seller.creator_marketplace.read"):
        b.exchange("code", "k", "s",
                   http_get=lambda *a, **kw: FakeResp(ok_payload(["seller.order.info"])))


def test_exchange_returns_token_record_with_scope():
    rec = b.exchange("code", "k", "s",
                     http_get=lambda *a, **kw: FakeResp(
                         ok_payload(["seller.creator_marketplace.read"])))
    assert rec["access_token"] == "a" and rec["refresh_token"] == "r"
    assert rec["app_key"] == "k" and rec["app_secret"] == "s"


def test_exchange_rejects_envelope_error():
    with pytest.raises(t.AuthError, match="single-use"):
        b.exchange("code", "k", "s",
                   http_get=lambda *a, **kw: FakeResp({"code": 36004004, "message": "auth_code expired"}))


def test_auth_url():
    assert b.auth_url("777") == "https://services.tiktokshop.com/open/authorize?service_id=777"
```

- [ ] **Step 2: Run to verify failure**

Run: `./venv/bin/python -m pytest tests/test_bootstrap.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'bootstrap_auth'`

- [ ] **Step 3: Implement `bootstrap_auth.py`**

```python
"""One-time auth for AffiliateFinder's dedicated TikTok app.

Ceremony (J-owned, spec §4/O5):
  1. Partner Center: create the app, request ONLY seller.creator_marketplace.read.
  2. ./venv/bin/python bootstrap_auth.py --print-auth-url <service_id>
     (service_id is on the app's detail page — it is NOT the App Key)
  3. Open the link as the shop's Seller Center account, approve, copy `code=` from
     the redirect URL (single-use, expires ~30 min).
  4. ./venv/bin/python bootstrap_auth.py <code> --app-key K --app-secret S \
         [--shop-cipher C]
     shop_cipher: affiliate-scoped apps usually CANNOT discover it themselves
     (105005 on /authorization/*). The cipher is portable across apps for the
     same shop (verified live 2026-07-06) — copy it from the command center's
     oauth_credentials row and pass it here, or add it later with
     --set-shop-cipher.

Scope gate (fail-closed): if TikTok reports granted_scopes without
seller.creator_marketplace.read, NOTHING is saved — the tokens would be
useless, and a re-auth is needed after fixing scopes anyway (a token refresh
never picks up scopes added later).
"""
import argparse, sys, time

import requests

import tiktok_api as t

REQUIRED_SCOPE = "seller.creator_marketplace.read"
SELLER_AUTH_HOST = "https://services.tiktokshop.com"


def auth_url(service_id):
    return f"{SELLER_AUTH_HOST}/open/authorize?service_id={service_id}"


def exchange(auth_code, app_key, app_secret, http_get=requests.get):
    r = http_get(f"{t.AUTH_HOST}/api/v2/token/get", params={
        "app_key": app_key, "app_secret": app_secret,
        "auth_code": auth_code, "grant_type": "authorized_code"}, timeout=30)
    if r.status_code != 200:
        raise t.AuthError(f"token exchange failed: HTTP {r.status_code}")
    j = r.json()
    d = j.get("data") or {}
    if j.get("code") != 0 or not d.get("access_token") or not d.get("refresh_token"):
        raise t.AuthError(
            f"token exchange rejected (code={j.get('code')}: {j.get('message', '')}). "
            "The auth_code is single-use and short-lived — generate a fresh "
            "authorization link and retry.")
    scopes = d.get("granted_scopes")
    if scopes is not None and REQUIRED_SCOPE not in scopes:
        raise t.AuthError(
            f"granted scopes {scopes} do not include {REQUIRED_SCOPE} — NOT saving these "
            "tokens (they would be useless). Add the scope in Partner Center, then "
            "re-authorize from step 2 (a refresh never picks up new scopes).")
    now = time.time()
    return {
        "app_key": app_key, "app_secret": app_secret,
        "access_token": d["access_token"],
        "access_token_expires_at": t.resolve_expiry(d.get("access_token_expire_in", 24 * 3600), now),
        "refresh_token": d["refresh_token"],
        "refresh_token_expires_at": t.resolve_expiry(d.get("refresh_token_expire_in", 365 * 24 * 3600), now),
        "shop_cipher": "", "region": "MY",
        "granted_scopes": scopes or [],
    }


def probe(deadline_sec=90):
    """Live search probe. Retries 105005 briefly: scope propagation after a fresh
    authorization is per-scope and not instant (observed ~45s on 2026-07-17)."""
    start = time.time()
    while True:
        try:
            data = t.search_creators_page({})
            n = len(data.get("creators") or [])
            print(f"✔ live probe OK — search returned {n} creators")
            return
        except t.ApiError as e:
            if e.code == 105005 and time.time() - start < deadline_sec:
                print("  … scope not propagated yet (105005), retrying in 10s")
                time.sleep(10)
                continue
            raise


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("auth_code", nargs="?")
    p.add_argument("--print-auth-url", metavar="SERVICE_ID")
    p.add_argument("--app-key")
    p.add_argument("--app-secret")
    p.add_argument("--shop-cipher")
    p.add_argument("--set-shop-cipher", metavar="CIPHER")
    a = p.parse_args(argv)

    if a.print_auth_url:
        print(f"Open as the shop's Seller Center account:\n\n  {auth_url(a.print_auth_url)}\n")
        print("After approving, copy the `code=` value from the redirect URL "
              "(a 404 page there is normal) and run:\n"
              "  ./venv/bin/python bootstrap_auth.py <code> --app-key K --app-secret S --shop-cipher C")
        return

    if a.set_shop_cipher:
        rec = t.load_tokens()
        rec["shop_cipher"] = a.set_shop_cipher
        t.save_tokens(rec)
        print("✔ shop_cipher saved")
        probe()
        return

    if not (a.auth_code and a.app_key and a.app_secret):
        p.error("need <auth_code> --app-key --app-secret (or --print-auth-url / --set-shop-cipher)")

    rec = exchange(a.auth_code, a.app_key, a.app_secret)
    if a.shop_cipher:
        rec["shop_cipher"] = a.shop_cipher
    t.save_tokens(rec)
    print(f"✔ tokens saved to {t.TOKENS_JSON} (scopes: {', '.join(rec['granted_scopes']) or 'not reported'})")

    if not rec["shop_cipher"]:
        print("⚠ no shop_cipher yet — affiliate_seller calls REQUIRE it (error 106013).\n"
              "  Copy it from the command center's oauth_credentials row (it is portable\n"
              "  across apps for the same shop) and run:\n"
              "  ./venv/bin/python bootstrap_auth.py --set-shop-cipher <cipher>")
        sys.exit(1)
    probe()


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run to verify pass**

Run: `./venv/bin/python -m pytest tests/test_bootstrap.py -q`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add bootstrap_auth.py tests/test_bootstrap.py && git commit -m "feat: one-time auth bootstrap with fail-closed scope gate"
```

---

### Task 6: App config (`config.py` + `config.example.json`)

**Files:**
- Create: `config.py`, `config.example.json`, `tests/test_config.py`

**Interfaces:**
- Produces: `config.ConfigError(RuntimeError)`; `config.load() -> dict` (fail-closed: missing file/keys are errors, never defaults); `config.save(cfg) -> None` (atomic); module global `config.CONFIG_JSON: Path`. Shape: `{"criteria": {...}, "want": int, "sheet": {...}, "region": "MY"}`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_config.py
import json
import pytest
import config


@pytest.fixture(autouse=True)
def tmp_cfg(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONFIG_JSON", tmp_path / "config.json")


def good():
    return {"criteria": {"min_followers": 5000, "max_followers": None, "min_gmv": 1000,
                         "min_units_sold": 100, "category_ids": []},
            "want": 60, "region": "MY",
            "sheet": {"sheet_id": "abc", "outreach_tab": "Outreach", "handle_column": "B",
                      "handle_header": "Handle", "service_account_file": "service_account.json"}}


def test_missing_file_is_loud():
    with pytest.raises(config.ConfigError, match="config.example.json"):
        config.load()


def test_missing_sheet_key_is_loud():
    cfg = good()
    del cfg["sheet"]["handle_column"]
    config.CONFIG_JSON.write_text(json.dumps(cfg))
    with pytest.raises(config.ConfigError, match="handle_column"):
        config.load()


def test_roundtrip():
    config.CONFIG_JSON.write_text(json.dumps(good()))
    cfg = config.load()
    cfg["want"] = 100
    config.save(cfg)
    assert config.load()["want"] == 100
```

- [ ] **Step 2: Run to verify failure**

Run: `./venv/bin/python -m pytest tests/test_config.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'config'`

- [ ] **Step 3: Implement `config.py` and the example file**

```python
"""config.json loader — fail-closed: a missing or incomplete config is an
error with instructions, never a silent default (spec §9-F3)."""
import json, os
from pathlib import Path

CONFIG_JSON = Path(__file__).parent / "config.json"

REQUIRED_TOP = ("criteria", "want", "sheet", "region")
REQUIRED_SHEET = ("sheet_id", "outreach_tab", "handle_column", "handle_header",
                  "service_account_file")


class ConfigError(RuntimeError):
    pass


def load():
    if not CONFIG_JSON.exists():
        raise ConfigError(
            "config.json is missing — copy config.example.json to config.json and "
            "fill in the sheet settings (see README, 'Setup').")
    try:
        cfg = json.loads(CONFIG_JSON.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        raise ConfigError(f"config.json is unreadable: {e}") from e
    for k in REQUIRED_TOP:
        if k not in cfg:
            raise ConfigError(f"config.json is missing '{k}' — compare with config.example.json")
    for k in REQUIRED_SHEET:
        if not cfg["sheet"].get(k):
            raise ConfigError(f"config.json sheet.{k} is missing or empty")
    return cfg


def save(cfg):
    tmp = CONFIG_JSON.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        os.replace(tmp, CONFIG_JSON)
    except OSError as e:
        raise ConfigError(f"could not write config.json: {e}") from e
```

```json
{
  "_comment": "Copy to config.json. criteria values are SEED VALUES pending Nurin's real thresholds (spec O2). category_ids is reserved (spec O4) — leave empty.",
  "criteria": {
    "min_followers": 5000,
    "max_followers": null,
    "min_gmv": 1000,
    "min_units_sold": 100,
    "category_ids": []
  },
  "want": 60,
  "region": "MY",
  "sheet": {
    "sheet_id": "PASTE_SPREADSHEET_ID_FROM_URL",
    "outreach_tab": "Outreach",
    "handle_column": "B",
    "handle_header": "Handle",
    "service_account_file": "service_account.json"
  }
}
```

- [ ] **Step 4: Run to verify pass**

Run: `./venv/bin/python -m pytest tests/test_config.py -q`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add config.py config.example.json tests/test_config.py && git commit -m "feat: fail-closed app config"
```

---

### Task 7: Sheet dedupe source (`sheet.py`)

**Files:**
- Create: `sheet.py`, `tests/test_sheet.py`

**Interfaces:**
- Consumes: `config` shape (`cfg["sheet"]`).
- Produces: `sheet.SheetError(RuntimeError)`; `sheet.parse_contacted(values, expected_header, where) -> list[str]` (pure, fail-closed); `sheet.contacted_handles(sheet_cfg) -> list[str]` (live fetch via service account).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_sheet.py
import pytest
import sheet


def test_parse_happy_path():
    values = [["Handle"], ["@abc "], [""], ["def"], []]
    assert sheet.parse_contacted(values, "Handle", "Outreach!B") == ["@abc", "def"]


def test_parse_wrong_header_is_blocking():
    with pytest.raises(sheet.SheetError, match="Wrong tab/column"):
        sheet.parse_contacted([["GMV"], ["9"]], "Handle", "Outreach!B")


def test_parse_empty_column_is_blocking():
    with pytest.raises(sheet.SheetError, match="dedupe source"):
        sheet.parse_contacted([["Handle"]], "Handle", "Outreach!B")


def test_parse_no_values_is_blocking():
    with pytest.raises(sheet.SheetError):
        sheet.parse_contacted([], "Handle", "Outreach!B")
```

- [ ] **Step 2: Run to verify failure**

Run: `./venv/bin/python -m pytest tests/test_sheet.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'sheet'`

- [ ] **Step 3: Implement `sheet.py`**

```python
"""Read-only access to Nurin's outreach sheet — the dedupe source.

Fail-closed (spec §9-F3): any doubt about the tab/column/credentials BLOCKS
discovery. Surfacing an already-contacted creator is the one thing this tool
must never do, so there is no 'proceed anyway'.
"""
from pathlib import Path
from urllib.parse import quote

import requests

APP_DIR = Path(__file__).parent
_SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]


class SheetError(RuntimeError):
    pass


def parse_contacted(values, expected_header, where):
    if not values or not values[0] or not str(values[0][0]).strip():
        raise SheetError(f"nothing found at {where} — wrong tab/column in config.json?")
    header = str(values[0][0]).strip()
    if header.lower() != expected_header.strip().lower():
        raise SheetError(
            f"expected header '{expected_header}' at the top of {where}, found '{header}'. "
            "Wrong tab/column in config.json?")
    handles = [str(v[0]).strip() for v in values[1:] if v and str(v[0]).strip()]
    if not handles:
        raise SheetError(
            f"0 contacted handles under {where} — almost certainly the wrong column; "
            "refusing to run discovery without a dedupe source.")
    return handles


def contacted_handles(sheet_cfg):
    sa_path = APP_DIR / sheet_cfg["service_account_file"]
    if not sa_path.exists():
        raise SheetError(
            f"service account file missing: {sa_path.name} — see README 'Sheet access' "
            "for how to create it and share the sheet with it.")
    try:
        from google.oauth2 import service_account
        from google.auth.transport.requests import Request as GoogleRequest
        creds = service_account.Credentials.from_service_account_file(str(sa_path), scopes=_SCOPES)
        creds.refresh(GoogleRequest())
    except Exception as e:
        raise SheetError(f"Google auth failed: {e}") from e

    tab, col = sheet_cfg["outreach_tab"], sheet_cfg["handle_column"]
    where = f"{tab}!{col}"
    url = (f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_cfg['sheet_id']}"
           f"/values/{quote(where + ':' + col)}")
    try:
        r = requests.get(url, headers={"Authorization": f"Bearer {creds.token}"}, timeout=30)
    except requests.RequestException as e:
        raise SheetError(f"could not reach Google Sheets: {e}") from e
    if r.status_code != 200:
        raise SheetError(f"Sheets API HTTP {r.status_code}: {r.text[:200]}")
    return parse_contacted(r.json().get("values") or [], sheet_cfg["handle_header"], where)
```

- [ ] **Step 4: Run to verify pass**

Run: `./venv/bin/python -m pytest tests/test_sheet.py -q`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add sheet.py tests/test_sheet.py && git commit -m "feat: fail-closed sheet dedupe source"
```

---

### Task 8: Discovery engine (`discovery.py`)

**Files:**
- Create: `discovery.py`, `tests/test_discovery.py`

**Interfaces:**
- Consumes: `tiktok_api.search_creators_page`, `sheet.contacted_handles`, `store.upsert_creators` / `store.load_creators` (all injectable for tests), `config.load`.
- Produces: `CriteriaError(ValueError)`, `ConformanceError(RuntimeError)`; `buckets_at_least(buckets, minimum) -> list[str]`; `validate_criteria(c) -> dict`; `build_search_body(criteria, search_key="") -> dict`; `check_gmv_conformance(creators, requested_buckets) -> None`; `norm_handle(h) -> str`; `to_store_row(c, now_iso) -> dict`; `run_discovery(criteria, want, *, page_budget=50, search_page=None, contacted=None, page_sleep=time.sleep) -> dict` returning `{"added", "skipped_contacted", "skipped_known", "pages_used", "exhausted"}`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_discovery.py
import pytest
import discovery as d
import sheet, store


@pytest.fixture(autouse=True)
def tmp_store(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DATA_FILE", tmp_path / "creators.json")


def crit(**over):
    base = {"min_followers": 1000, "max_followers": None, "min_gmv": 1000,
            "min_units_sold": 100, "category_ids": []}
    base.update(over)
    return base


def api_creator(handle, followers=5000, gmv=2000.0, floor_min="1000"):
    return {"username": handle, "nickname": "N " + handle, "creator_open_id": "id_" + handle,
            "follower_count": followers, "gmv": {"amount": str(gmv), "currency": "USD"},
            "gmv_range": {"minimum_amount": floor_min, "currency": "USD"},
            "video_gmv": {"amount": "1"}, "live_gmv": {"amount": "1"},
            "avg_ec_video_view_count": 60, "avg_ec_live_uv": 10,
            "category_ids": ["601450"], "selection_region": "MY"}


def test_buckets_at_least():
    assert d.buckets_at_least(d.GMV_BUCKETS, 0) == [
        "GMV_RANGE_0_100", "GMV_RANGE_100_1000", "GMV_RANGE_1000_10000",
        "GMV_RANGE_10000_AND_ABOVE"]
    assert d.buckets_at_least(d.GMV_BUCKETS, 1000) == [
        "GMV_RANGE_1000_10000", "GMV_RANGE_10000_AND_ABOVE"]
    assert d.buckets_at_least(d.GMV_BUCKETS, 500) == [
        "GMV_RANGE_100_1000", "GMV_RANGE_1000_10000", "GMV_RANGE_10000_AND_ABOVE"]
    assert d.buckets_at_least(d.UNITS_BUCKETS, 20000) == ["UNITS_SOLD_RANGE_1000_AND_ABOVE"]


def test_criteria_validation_is_fail_closed():
    with pytest.raises(d.CriteriaError, match="min_gmv"):
        d.validate_criteria(crit(min_gmv="1,000"))
    with pytest.raises(d.CriteriaError, match="category"):
        d.validate_criteria(crit(category_ids=["601450"]))   # deferred until O4 verifies the shape


def test_body_only_uses_verified_fields():
    body = d.build_search_body(d.validate_criteria(crit()), search_key="sk")
    assert set(body) <= d.KNOWN_BODY_FIELDS
    assert body["gmv_ranges"] == ["GMV_RANGE_1000_10000", "GMV_RANGE_10000_AND_ABOVE"]
    assert body["units_sold_ranges"] == ["UNITS_SOLD_RANGE_100_1000", "UNITS_SOLD_RANGE_1000_AND_ABOVE"]
    assert body["search_key"] == "sk"


def test_gmv_conformance_catches_ignored_filter():
    rows = [api_creator("a", floor_min="0")]
    with pytest.raises(d.ConformanceError, match="not applied"):
        d.check_gmv_conformance(rows, ["GMV_RANGE_1000_10000", "GMV_RANGE_10000_AND_ABOVE"])
    d.check_gmv_conformance([api_creator("a", floor_min="10000")],
                            ["GMV_RANGE_1000_10000", "GMV_RANGE_10000_AND_ABOVE"])  # no raise


def test_to_store_row_shape():
    row = d.to_store_row(api_creator("Sha"), "2026-07-20T00:00:00+00:00")
    assert row["handle"] == "sha" and row["gmv"] == 2000.0
    assert row["gmv_is_floor"] is False and row["items_sold"] is None
    assert row["source"] == "api" and row["creator_open_id"] == "id_Sha"
    assert row["profile_url"] == "https://www.tiktok.com/@sha"


def fake_pages(pages):
    """pages: list of creator-lists; returns (search_page fn, calls list)."""
    calls = []
    def search_page(body, page_token=""):
        calls.append(body)
        i = len(calls) - 1
        creators = pages[i] if i < len(pages) else []
        nxt = "tok" if i + 1 < len(pages) else ""
        return {"creators": creators, "next_page_token": nxt, "search_key": "sk"}
    return search_page, calls


def test_run_discovery_dedupes_and_counts():
    store.upsert_creators([{"handle": "known", "creator_open_id": "id_known"}])
    sp, _ = fake_pages([[api_creator("new1"), api_creator("contacted1"),
                         api_creator("known"), api_creator("small", followers=10)]])
    out = d.run_discovery(crit(), 10, search_page=sp,
                          contacted=lambda: ["@Contacted1"], page_sleep=lambda s: None)
    assert out["added"] == 1 and out["skipped_contacted"] == 1 and out["skipped_known"] == 1
    assert out["exhausted"] is True                     # wanted 10, got 1
    handles = {r["handle"] for r in store.load_creators()}
    assert handles == {"known", "new1"}                 # 'small' failed the exact cut


def test_run_discovery_stops_at_want():
    sp, calls = fake_pages([[api_creator(f"c{i}") for i in range(20)]] * 3)
    out = d.run_discovery(crit(), 5, search_page=sp,
                          contacted=lambda: ["@x"], page_sleep=lambda s: None)
    assert out["added"] == 5 and out["exhausted"] is False and len(calls) == 1


def test_sheet_error_blocks_before_any_api_call():
    sp, calls = fake_pages([[api_creator("a")]])
    def broken():
        raise sheet.SheetError("sheet down")
    with pytest.raises(sheet.SheetError):
        d.run_discovery(crit(), 5, search_page=sp, contacted=broken, page_sleep=lambda s: None)
    assert calls == []                                   # F3: no API call happened
```

- [ ] **Step 2: Run to verify failure**

Run: `./venv/bin/python -m pytest tests/test_discovery.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'discovery'`

- [ ] **Step 3: Implement `discovery.py`**

```python
"""Creator discovery: official search API + sheet/store dedupe (spec §6)."""
import time
from datetime import datetime, timezone

import config as app_config
import sheet
import store
import tiktok_api


class CriteriaError(ValueError):
    pass


class ConformanceError(RuntimeError):
    pass


GMV_BUCKETS = (
    ("GMV_RANGE_0_100", 0, 100),
    ("GMV_RANGE_100_1000", 100, 1_000),
    ("GMV_RANGE_1000_10000", 1_000, 10_000),
    ("GMV_RANGE_10000_AND_ABOVE", 10_000, None),
)
UNITS_BUCKETS = (
    ("UNITS_SOLD_RANGE_0_10", 0, 10),
    ("UNITS_SOLD_RANGE_10_100", 10, 100),
    ("UNITS_SOLD_RANGE_100_1000", 100, 1_000),
    ("UNITS_SOLD_RANGE_1000_AND_ABOVE", 1_000, None),
)
# The search API SILENTLY IGNORES unknown body fields (verified live
# 2026-07-20) — bodies may only be built from this verified registry.
KNOWN_BODY_FIELDS = frozenset({
    "search_key", "keyword", "follower_demographics", "gmv_ranges",
    "units_sold_ranges", "category", "content_performance",
    "affiliate_data", "advanced_filters",
})


def buckets_at_least(buckets, minimum):
    chosen = [name for name, lo, hi in buckets if hi is None or hi > minimum]
    if not chosen:
        raise CriteriaError(f"minimum {minimum} is above every TikTok bucket")
    return chosen


def _require_number(criteria, key):
    v = criteria.get(key)
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
        raise CriteriaError(f"criteria.{key} must be a number >= 0 (got {v!r})")
    return v


def validate_criteria(c):
    out = {k: _require_number(c, k) for k in ("min_followers", "min_gmv", "min_units_sold")}
    mf = c.get("max_followers")
    if mf is not None:
        if isinstance(mf, bool) or not isinstance(mf, (int, float)) or mf <= out["min_followers"]:
            raise CriteriaError(f"criteria.max_followers must be > min_followers or null (got {mf!r})")
    out["max_followers"] = mf
    if c.get("category_ids"):
        # The category filter's body shape is UNVERIFIED (spec O4). Sending a
        # guessed shape would be silently ignored — reject instead (fail closed).
        raise CriteriaError(
            "category filtering is not supported yet (spec O4) — leave category_ids empty")
    out["category_ids"] = []
    return out


def build_search_body(criteria, search_key=""):
    body = {
        "gmv_ranges": buckets_at_least(GMV_BUCKETS, criteria["min_gmv"]),
        "units_sold_ranges": buckets_at_least(UNITS_BUCKETS, criteria["min_units_sold"]),
    }
    if search_key:
        body["search_key"] = search_key
    unknown = set(body) - KNOWN_BODY_FIELDS
    if unknown:  # belt-and-braces against future edits reintroducing the silent-ignore bug
        raise ConformanceError(f"unverified body fields: {sorted(unknown)}")
    return body


def check_gmv_conformance(creators, requested_buckets):
    allowed_min = min(lo for name, lo, hi in GMV_BUCKETS if name in requested_buckets)
    for c in creators:
        got = float((c.get("gmv_range") or {}).get("minimum_amount") or 0)
        if got < allowed_min:
            raise ConformanceError(
                "TikTok returned a creator below the requested GMV bucket "
                f"(bucket floor {got} < requested {allowed_min}) — the gmv_ranges filter was "
                "not applied. Aborting rather than showing unfiltered results (spec F2).")


def norm_handle(h):
    return str(h or "").strip().lstrip("@").lower()


def to_store_row(c, now_iso):
    handle = norm_handle(c.get("username"))
    return {
        "handle": handle,
        "nickname": str(c.get("nickname") or ""),
        "creator_open_id": str(c.get("creator_open_id") or ""),
        "followers": int(c.get("follower_count") or 0),
        # exact — the API returns the real figure even where the UI shows "RM10K+"
        "gmv": float((c.get("gmv") or {}).get("amount") or 0),
        "gmv_is_floor": False,
        "video_gmv": float((c.get("video_gmv") or {}).get("amount") or 0),
        "live_gmv": float((c.get("live_gmv") or {}).get("amount") or 0),
        "items_sold": None,          # not in the search response; filtered at source
        "category": "",
        "category_ids": list(c.get("category_ids") or []),
        "level": 0,
        "avg_video_views": int(c.get("avg_ec_video_view_count") or 0),
        "avg_live_uv": int(c.get("avg_ec_live_uv") or 0),
        "engagement_rate": "",
        "profile_url": f"https://www.tiktok.com/@{handle}" if handle else "",
        "tiktok_user_id": "",
        "source": "api",
        "discovered_at": now_iso,
        "fetched_at": now_iso,
    }


def _qualifies(row, criteria):
    if row["followers"] < criteria["min_followers"]:
        return False
    if criteria["max_followers"] is not None and row["followers"] > criteria["max_followers"]:
        return False
    return row["gmv"] >= criteria["min_gmv"]


def run_discovery(criteria, want, *, page_budget=50,
                  search_page=None, contacted=None, page_sleep=time.sleep):
    search_page = search_page or tiktok_api.search_creators_page
    if contacted is None:
        sheet_cfg = app_config.load()["sheet"]
        contacted = lambda: sheet.contacted_handles(sheet_cfg)
    criteria = validate_criteria(criteria)
    if isinstance(want, bool) or not isinstance(want, int) or not 1 <= want <= 200:
        raise CriteriaError(f"want must be an integer 1-200 (got {want!r})")

    contacted_set = {norm_handle(h) for h in contacted()}   # SheetError propagates: F3
    known_handles, known_ids = set(), set()
    for r in store.load_creators():
        known_handles.add(norm_handle(r.get("handle")))
        if r.get("creator_open_id"):
            known_ids.add(r["creator_open_id"])

    gmv_buckets = buckets_at_least(GMV_BUCKETS, criteria["min_gmv"])
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")

    new_rows, skipped_contacted, skipped_known = [], 0, 0
    page_token, search_key, pages = "", "", 0
    while len(new_rows) < want and pages < page_budget:
        data = search_page(build_search_body(criteria, search_key), page_token)
        pages += 1
        creators = data.get("creators") or []
        check_gmv_conformance(creators, gmv_buckets)
        for c in creators:
            row = to_store_row(c, now_iso)
            if not row["handle"]:
                continue
            if row["handle"] in contacted_set:
                skipped_contacted += 1
                continue
            if row["handle"] in known_handles or (
                    row["creator_open_id"] and row["creator_open_id"] in known_ids):
                skipped_known += 1
                continue
            if not _qualifies(row, criteria):
                continue
            known_handles.add(row["handle"])
            new_rows.append(row)
            if len(new_rows) >= want:
                break
        search_key = data.get("search_key") or search_key
        page_token = data.get("next_page_token") or ""
        if not page_token:
            break
        page_sleep(0.25)

    if new_rows:
        store.upsert_creators(new_rows)
    return {"added": len(new_rows), "skipped_contacted": skipped_contacted,
            "skipped_known": skipped_known, "pages_used": pages,
            "exhausted": len(new_rows) < want}
```

- [ ] **Step 4: Run to verify pass**

Run: `./venv/bin/python -m pytest tests/test_discovery.py -q`
Expected: 9 passed

- [ ] **Step 5: Full suite + commit**

Run: `./venv/bin/python -m pytest -q` → all pass.

```bash
git add discovery.py tests/test_discovery.py && git commit -m "feat: discovery engine — bucket mapping, conformance checks, sheet+store dedupe"
```

---

### Task 9: Server endpoints + None-safe classify (`ui_server.py`)

**Files:**
- Modify: `ui_server.py` (new endpoints; `gmv_per_customer`, `add_balanced_score`, `classify`, `_is_floor` adjustments)
- Test: `tests/test_ui_server.py`

**Interfaces:**
- Consumes: `discovery.run_discovery`, `config.load/save`, `discovery.validate_criteria`, exceptions from `discovery`/`sheet`/`tiktok_api`/`config`/`store`.
- Produces: `POST /api/discover` (body `{"want": int?, "criteria": {...}?}` → discovery result JSON or `{"error"}` with 400 for `CriteriaError`, 502 for `SheetError`/`ConformanceError`/`AuthError`/`ApiError`/`ConfigError`); `GET /api/discovery_config` → `{"criteria", "want"}`; `POST /api/discovery_config` (validated, 400 on bad input). API rows (`items_sold: None`) classify without the items/GPC gates (those were applied at source) and score with `None → 0`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_ui_server.py
import json
import pytest
import config, discovery, sheet, store, ui_server


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DATA_FILE", tmp_path / "creators.json")
    monkeypatch.setattr(config, "CONFIG_JSON", tmp_path / "config.json")
    config.CONFIG_JSON.write_text(json.dumps({
        "criteria": {"min_followers": 1000, "max_followers": None, "min_gmv": 1000,
                     "min_units_sold": 100, "category_ids": []},
        "want": 60, "region": "MY",
        "sheet": {"sheet_id": "x", "outreach_tab": "Outreach", "handle_column": "B",
                  "handle_header": "Handle", "service_account_file": "sa.json"}}))
    return ui_server.app.test_client()


def api_row(handle="apirow"):
    return {"handle": handle, "nickname": "", "creator_open_id": "id1", "followers": 50_000,
            "gmv": 12_345.0, "gmv_is_floor": False, "items_sold": None, "category": "",
            "level": 0, "avg_video_views": 100, "engagement_rate": "",
            "profile_url": "", "tiktok_user_id": "", "source": "api",
            "discovered_at": "2026-07-20", "fetched_at": "2026-07-20"}


def test_discover_maps_sheet_error_to_502(client, monkeypatch):
    def boom(criteria, want, **kw):
        raise sheet.SheetError("sheet down")
    monkeypatch.setattr(discovery, "run_discovery", boom)
    r = client.post("/api/discover", json={})
    assert r.status_code == 502 and "sheet down" in r.get_json()["error"]


def test_discover_maps_criteria_error_to_400(client, monkeypatch):
    def bad(criteria, want, **kw):
        raise discovery.CriteriaError("want must be an integer")
    monkeypatch.setattr(discovery, "run_discovery", bad)
    assert client.post("/api/discover", json={}).status_code == 400


def test_discover_happy_path_uses_config_defaults(client, monkeypatch):
    seen = {}
    def ok(criteria, want, **kw):
        seen.update(criteria=criteria, want=want)
        return {"added": 3, "skipped_contacted": 1, "skipped_known": 0,
                "pages_used": 2, "exhausted": False}
    monkeypatch.setattr(discovery, "run_discovery", ok)
    r = client.post("/api/discover", json={"want": 20})
    assert r.status_code == 200 and r.get_json()["added"] == 3
    assert seen["want"] == 20 and seen["criteria"]["min_gmv"] == 1000


def test_discovery_config_roundtrip(client):
    r = client.get("/api/discovery_config")
    assert r.status_code == 200 and r.get_json()["want"] == 60
    r = client.post("/api/discovery_config", json={
        "criteria": {"min_followers": 2000, "max_followers": None, "min_gmv": 500,
                     "min_units_sold": 10, "category_ids": []}, "want": 100})
    assert r.status_code == 200
    assert client.get("/api/discovery_config").get_json()["want"] == 100


def test_discovery_config_rejects_garbage(client):
    r = client.post("/api/discovery_config", json={
        "criteria": {"min_followers": "lots"}, "want": 60})
    assert r.status_code == 400


def test_api_rows_classify_without_items_gate(client):
    store.save_creators([api_row()])
    r = client.post("/api/search", json={"min_items_sold": 500,
                                         "min_gmv_per_customer": 25})
    data = r.get_json()
    assert data["matched"] == 1                       # gates applied at source, not here
    creator = data["creators"][0]
    assert creator["items_sold"] is None and creator["gmv_per_customer"] is None


def test_corrupt_store_returns_500_not_empty(client):
    store.DATA_FILE.write_text("{broken", encoding="utf-8")
    r = client.get("/api/creators")
    assert r.status_code == 500 and "refusing" in r.get_json()["error"]
```

- [ ] **Step 2: Run to verify failure**

Run: `./venv/bin/python -m pytest tests/test_ui_server.py -q`
Expected: FAIL (404s on /api/discover, AttributeError on items_sold None, etc.)

- [ ] **Step 3: Implement in `ui_server.py`**

1. Add imports: `import config`, `import discovery`, `import sheet`, `import tiktok_api`.
2. Fix `_is_floor` (the `bool("False")` bug — CSV strings):

```python
def _is_floor(row):
    """Trust an explicit flag from the source; otherwise sniff a trailing '+'."""
    if "gmv_is_floor" in row:
        v = row["gmv_is_floor"]
        if isinstance(v, str):
            return v.strip().lower() in ("true", "1", "yes")
        return bool(v)
    return "+" in str(row.get("gmv", ""))
```

3. Make `gmv_per_customer` / `decorate` / `add_balanced_score` None-safe:

```python
def gmv_per_customer(row):
    # ... (keep the existing docstring)
    items = row.get("items_sold")
    if items is None:
        return None          # API rows: items filtered at source, count unknown
    if not items:
        return 0.0
    return row["gmv"] / items


def decorate(row):
    gpc = gmv_per_customer(row)
    return {**row, "gmv_per_customer": None if gpc is None else round(gpc, 2)}
```

and in `add_balanced_score`, build columns with `columns = {k: _percentiles([r.get(k) or 0 for r in rows]) for k in SCORED_METRICS}`.

4. In `classify`, replace the items and GPC gates so `None` passes (source-filtered):

```python
    items = row.get("items_sold")
    if items is not None and items < min_items:
        return "reject"
```

and

```python
    gpc = row["gmv_per_customer"]
    if gpc is None or gpc >= min_gmv_pc:
        return "match"
```

5. Add the discovery endpoints (below the `/api/search` route):

```python
# ── Discovery (official creator-search API — spec §6/§9) ────────────────────
@app.get("/api/discovery_config")
def get_discovery_config():
    try:
        cfg = config.load()
    except config.ConfigError as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({"criteria": cfg["criteria"], "want": cfg["want"]})


@app.post("/api/discovery_config")
def set_discovery_config():
    body = request.get_json(silent=True) or {}
    try:
        cfg = config.load()
        criteria = discovery.validate_criteria(body.get("criteria") or {})
        want = body.get("want")
        if isinstance(want, bool) or not isinstance(want, int) or not 1 <= want <= 200:
            raise discovery.CriteriaError(f"want must be an integer 1-200 (got {want!r})")
        cfg["criteria"], cfg["want"] = {**criteria, "category_ids": []}, want
        config.save(cfg)
    except (config.ConfigError, discovery.CriteriaError) as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"criteria": cfg["criteria"], "want": want})


@app.post("/api/discover")
def api_discover():
    body = request.get_json(silent=True) or {}
    try:
        cfg = config.load()
        criteria = {**cfg["criteria"], **(body.get("criteria") or {})}
        want = body.get("want", cfg["want"])
        result = discovery.run_discovery(criteria, want)
    except discovery.CriteriaError as e:
        return jsonify({"error": str(e)}), 400
    except (config.ConfigError, sheet.SheetError, discovery.ConformanceError,
            tiktok_api.AuthError, tiktok_api.ApiError, store.StoreError) as e:
        return jsonify({"error": str(e)}), 502
    return jsonify(result)
```

- [ ] **Step 4: Run to verify pass**

Run: `./venv/bin/python -m pytest -q`
Expected: all tests pass (including earlier suites).

- [ ] **Step 5: Commit**

```bash
git add ui_server.py tests/test_ui_server.py && git commit -m "feat: /api/discover + discovery config endpoints; None-safe classify for API rows"
```

---

### Task 10: Dashboard Discover flow; retire the scraper (`ui.html`, `ui_server.py`, `scraper.py`)

**Files:**
- Modify: `ui.html` (Discover button + criteria form replace harvest UI; None-safe cells)
- Modify: `ui_server.py` (delete harvest section + routes)
- Delete: `scraper.py`

**Interfaces:**
- Consumes: `POST /api/discover`, `GET/POST /api/discovery_config` (Task 9 shapes), `esc()` (Task 2).

- [ ] **Step 1: Replace the harvest action row in `ui.html`**

Replace the `action-row` block (the `find_btn` + `target`/`region` sub-controls) with:

```html
    <div class="row action-row">
      <button class="btn-primary" id="find_btn" onclick="findCreators()">🔎 Discover creators</button>
      <span class="sub-controls">
        <label for="want" style="margin:0">How many</label>
        <select id="want" style="width:auto">
          <option value="20">20</option>
          <option value="60" selected>60</option>
          <option value="100">100</option>
        </select>
      </span>
      <span class="status" id="status">—</span>
    </div>
```

Delete the `first_run_note` div. Replace the whole `findCreators()` function and delete the harvest `labels` map:

```js
// Primary flow: ask TikTok's creator-search API, dedupe against the outreach
// sheet + local list, then apply the display filters.
async function findCreators() {
  const btn = document.getElementById("find_btn");
  const prog = document.getElementById("progress");
  btn.disabled = true; btn.textContent = "Discovering…";
  showProgress(prog, "Searching TikTok's creator marketplace… (up to a minute)", "run");
  try {
    const res = await fetch("/api/discover", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ want: parseInt(document.getElementById("want").value, 10) }),
    });
    const r = await res.json();
    if (r.error) { showProgress(prog, "Couldn't discover: " + esc(r.error), "err"); return; }
    const data = await search();
    const summary = `✓ ${r.added} new · ${r.skipped_contacted} already contacted · ` +
                    `${r.skipped_known} already in list · ${r.pages_used} page${r.pages_used === 1 ? "" : "s"}`;
    if (r.added === 0 && r.exhausted) {
      showProgress(prog, `0 creators matched your criteria (${r.pages_used} pages searched) — ` +
                         `open “Discovery criteria” below and loosen them`, "warn");
    } else if (r.exhausted) {
      showProgress(prog, summary + " — stopped early: criteria may be too narrow", "warn");
    } else {
      showProgress(prog, summary, "ok");
    }
  } catch (e) {
    showProgress(prog, "Couldn't reach the server — is it running from the launcher?", "err");
  } finally {
    btn.disabled = false; btn.textContent = "🔎 Discover creators";
  }
}
```

- [ ] **Step 2: Add the criteria form**

Insert after the filters `.sec` (before the table `.sec`):

```html
  <details class="sec" id="crit_sec">
    <summary class="sec-title" style="margin:0">Discovery criteria — what “Discover creators” asks TikTok for</summary>
    <div style="margin-top:14px">
      <div class="fields">
        <div><label for="c_minf">Min followers</label><input id="c_minf" type="text"></div>
        <div><label for="c_maxf">Max followers (blank = no cap)</label><input id="c_maxf" type="text"></div>
        <div><label for="c_gmv">Min GMV (RM)</label><input id="c_gmv" type="text"></div>
        <div><label for="c_units">Min items sold</label><input id="c_units" type="text"></div>
      </div>
      <div class="row">
        <button class="btn" onclick="saveCriteria()">Save criteria</button>
        <span class="status" id="crit_status"></span>
      </div>
      <div class="note">These are applied by TikTok at the source (bucket filters) plus an exact
        cut here. GMV and items minimums map to TikTok's buckets; followers are cut exactly.</div>
    </div>
  </details>
```

And the script functions (near `loadTemplate`):

```js
async function loadCriteria() {
  const res = await fetch("/api/discovery_config");
  const cfg = await res.json();
  if (cfg.error) { document.getElementById("crit_status").textContent = cfg.error; return; }
  document.getElementById("c_minf").value = cfg.criteria.min_followers;
  document.getElementById("c_maxf").value = cfg.criteria.max_followers ?? "";
  document.getElementById("c_gmv").value = cfg.criteria.min_gmv;
  document.getElementById("c_units").value = cfg.criteria.min_units_sold;
  document.getElementById("want").value = String(cfg.want);
}

async function saveCriteria() {
  const num = v => v.trim() === "" ? null : Number(v.replace(/,/g, ""));
  const body = {
    criteria: {
      min_followers: num(document.getElementById("c_minf").value),
      max_followers: num(document.getElementById("c_maxf").value),
      min_gmv: num(document.getElementById("c_gmv").value),
      min_units_sold: num(document.getElementById("c_units").value),
      category_ids: [],
    },
    want: parseInt(document.getElementById("want").value, 10),
  };
  const res = await fetch("/api/discovery_config", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const r = await res.json();
  document.getElementById("crit_status").textContent = r.error ? ("✗ " + r.error) : "Saved";
}
```

Add `loadCriteria();` next to the existing `loadTemplate();` bootstrapping calls.

- [ ] **Step 3: None-safe table cells and DM tokens**

In `render()`, replace the items / GPC cells:

```js
    const gpc = c.gmv_per_customer == null ? "—"
      : (c.gmv_is_floor ? "≥" + fmtRm(c.gmv_per_customer) : fmtRm(c.gmv_per_customer));
```

and `<td class="num">${c.items_sold == null ? "—" : c.items_sold.toLocaleString("en-MY")}</td>`.

In `fmtToken`, guard the two numeric cases:

```js
    case "items_sold":       return c.items_sold == null ? "—" : c.items_sold.toLocaleString("en-MY");
    case "gmv_per_customer": return c.gmv_per_customer == null ? "—"
                                    : (c.gmv_is_floor ? "over " : "") + fmtRm(c.gmv_per_customer);
```

- [ ] **Step 4: Delete the harvest path server-side and the scraper**

In `ui_server.py`: delete the whole `# ── Harvest ──` section (`_harvest` dict, `_harvest_lock`, `_run_harvest`, `start_harvest`, `harvest_status` routes) and update the module docstring's first paragraph to say rows come from the official creator-search API via `/api/discover`.

```bash
git rm scraper.py
```

- [ ] **Step 5: Verify**

Run: `./venv/bin/python -m pytest -q` → all pass.
Run: `grep -c "api/harvest" ui_server.py ui.html` → `0` in both (grep exits non-zero — that's the pass condition).
Manual smoke: `./venv/bin/python ui_server.py`, open http://localhost:7374 — with no `config.json` present, clicking **Discover creators** must show the red error telling you to copy `config.example.json` (fail-closed proof), and the criteria form shows the config error. Stop the server.

- [ ] **Step 6: Commit**

```bash
git add -A && git commit -m "feat: Discover flow replaces scraper harvest; retire scraper.py"
```

---

### Task 11: README rewrite + runbook

**Files:**
- Modify: `README.md` (full rewrite)

**Interfaces:**
- Consumes: every command and error message defined in Tasks 1–10.

- [ ] **Step 1: Rewrite `README.md`** with exactly these sections (real content, not stubs — pull the exact commands/messages from the tasks above):

1. **What this is** — discover qualified TikTok affiliate creators via the official API, dedupe against the outreach sheet, one-button DM pre-fill (human always clicks Send). One paragraph on what changed vs. the scraper version and why (source filtering, exact GMV, no DOM fragility).
2. **Setup** — clone; `Start Affiliate Finder.command` (creates venv, installs deps); copy `config.example.json` → `config.json` and fill in sheet settings; criteria defaults are seed values (update with Nurin's real thresholds).
3. **One-time TikTok auth (J does this)** — the four-step ceremony from `bootstrap_auth.py`'s docstring verbatim, including: service_id ≠ App Key; auth code is single-use/~30 min; shop_cipher is copied from the command center (portable across apps, error 106013 without it); scope-propagation note (105005 for up to ~1 min after auth is normal — the bootstrap probe retries).
4. **Sheet access** — create a Google Cloud service account, download its JSON key as `service_account.json` into the app folder, share the outreach sheet with the service account's email as **Viewer**. The tool reads one column (configured tab/column/header) and refuses to run if the header doesn't match.
5. **Daily use (Nurin)** — Discover → review → Message → log in the sheet. Note that anyone already in the sheet's handle column will never be surfaced again.
6. **Failure-message glossary** — a table mapping every error string to what-to-do: `config.json is missing…` → copy the example; `tokens.json is missing…` / `token refresh rejected…` → re-run the auth ceremony (J); `PERSISTING ROTATED TOKENS FAILED` → disk problem, then re-bootstrap (J); `expected header…` / `0 contacted handles…` → fix sheet tab/column in config; `TikTok error 45101004` → daily quota, try tomorrow; `…filter was not applied…` → TikTok changed the API, stop and tell J; `creators.json is unreadable…` → the store is corrupt, do not clear it, tell J.
7. **DM template policy** — the template is external-facing copy for a MAB-regulated product; changes require J's sign-off (no banned superlatives in any language, no condition claims).
8. **Credential revocation (runbook)** — deauthorize the app in Seller Center or rotate its secret in Partner Center; delete `tokens.json`; re-bootstrap when needed.
9. **Handover note** — ownership re-decision due September 2026; everything an operator needs is sections 2–8.

- [ ] **Step 2: Verify**

Run: `grep -c "bootstrap_auth" README.md` → ≥ 2. Read the file top to bottom once; every command must be copy-pasteable and match the actual filenames.

- [ ] **Step 3: Commit**

```bash
git add README.md && git commit -m "docs: rewrite README — API discovery setup, runbook, failure glossary"
```

---

### Task 12: Live verification (blocked on O5 — J's Partner Center ceremony — and O3 sheet setup)

**Files:** none created — this is the spec's M1/M2 acceptance run. Requires: the dedicated app exists with the scope, `config.json` + `service_account.json` are filled in on the machine running it.

- [ ] **Step 1: Bootstrap** — run the ceremony from README §3. Expected: `✔ tokens saved … ✔ live probe OK — search returned 12 creators` (the probe retries 105005 for up to 90s first).
- [ ] **Step 2: Scope gate check (F8)** — before adding the scope-correct auth code, if a scope-less code is available, verify bootstrap aborts with the scope message and `tokens.json` does not exist. If not practical, skip — the unit test covers it.
- [ ] **Step 3: Live discovery** — start the server, click Discover (want=20). Expected: summary line with real counts; every row in the table satisfies the criteria (spot-check 5 rows: followers ≥ min, GMV ≥ min, exact GMV shown — no "≥" prefix on API rows).
- [ ] **Step 4: Dedupe proof (M2)** — add one just-discovered handle to the outreach sheet's handle column, clear it from the local list (or note its absence), rerun Discover. Expected: that handle never reappears and `skipped (already contacted)` increments.
- [ ] **Step 5: Fail-closed proofs** — (a) temporarily rename the sheet's header cell → Discover must show the header error and make **no** TikTok call (verify by count staying flat in the summary); restore it. (b) Kill the network mid-discover → red error, `creators.json` intact.
- [ ] **Step 6: DM flow regression** — click Message on one API-discovered creator → the DM browser opens the chat pre-filled exactly as before.
- [ ] **Step 7: Ship** — push the branch and open the PR:

```bash
git push -u origin api-discovery
gh pr create --repo Serigamateam123/AffiliateFinder --title "Discovery rebuilt on the official creator-search API" \
  --body "Implements docs/superpowers/specs/2026-07-20-api-discovery-rebuild.md (rev 3, approved). See README for the one-time auth ceremony and runbook.

🤖 Generated with [Claude Code](https://claude.com/claude-code)"
```

---

## Self-review notes (already applied)

- **Spec coverage:** G1→Tasks 8–10; G2→Tasks 7–8 (+ Task 12 §4); G3→Task 8 `to_store_row` (exact `gmv`, `gmv_is_floor: False`); G4→F1/F2 (Task 8), F3 (Tasks 7–8), F4/F5 (Task 10 UI), F6 (Task 1), F7 (Task 4), F8 (Task 5); G5→Task 11; Batch-1 code-review fixes→Tasks 1–2; scraper retirement→Task 10; compliance note→Task 11 §7.
- **Deliberate deviations from the spec, called out:** (1) API rows reuse the existing `gmv` field for the exact figure instead of adding a parallel `gmv_exact` — one field drives the existing UI/classify unchanged; `video_gmv`/`live_gmv` are stored as spec'd. (2) Server-side `category` filtering is deferred (O4): the body shape is unverified and would be silently ignored — criteria with `category_ids` are rejected, fail-closed, rather than sent as a guess. (3) `items_sold` is `None` on API rows (not in the search response); the items/GPC display gates pass those rows because the equivalent filter already ran server-side via `units_sold_ranges`.
- **Type consistency check:** `store.upsert_creators` return keys (`added/updated/total`) match Task 1 tests and Task 9 usage; `run_discovery` result keys match Task 9's endpoint passthrough and Task 10's JS (`added`, `skipped_contacted`, `skipped_known`, `pages_used`, `exhausted`); token-record keys match between Tasks 4 and 5.
