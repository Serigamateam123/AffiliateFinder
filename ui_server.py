"""
Affiliate creator finder — local dashboard
Run: python ui_server.py
Opens at: http://localhost:7374

Creator rows come from TikTok's official creator-search API via POST /api/discover
(see discovery.py), which also upserts them via POST /api/creators. This server
owns the store, the filtering, and the ranking. See DATA_CONTRACT below for the
row shape.
"""
import csv, io, json, os, threading, webbrowser
from pathlib import Path
from flask import Flask, jsonify, request, send_from_directory
import config
import discovery
import sheet
import store
import tiktok_api
from store import load_creators, save_creators

APP_DIR      = Path(__file__).parent
SETTINGS_JSON = APP_DIR / "settings.json"
PORT          = int(os.environ.get("PORT", 7374))

# The DM draft. Tokens in {braces} are filled per creator in the browser.
# This mirrors the seller's own proven Malay outreach message (observed in their
# TikTok inbox), with a personalised greeting prepended. Edit it in the UI.
DEFAULT_TEMPLATE = (
    "Hi {nickname}! 👋\n\n"
    "🎁 Apa yang anda akan dapat:\n"
    "✨ Produk untuk dicuba & review\n"
    "✨ 5% Komisen bagi setiap jualan\n"
    "✨ Kebebasan untuk hasilkan content ikut gaya anda 🎬\n\n"
    "Kami percaya anda sangat sesuai untuk membawa mesej kempen ini 💚\n\n"
    "Boleh saya tahu jika anda terbuka untuk kolaborasi Gift Review ini?\n\n"
    "Terima kasih! 🥰"
)

# ── Data contract ─────────────────────────────────────────────────────────────
# One creator row, as scraped from Affiliate Center → Find creators (MY).
#   handle           str   unique key, used for upsert
#   nickname         str   display name
#   followers        int
#   gmv              float RM (Ringgit) — the shop is MY, not US
#   gmv_is_floor     bool  True when Seller Center showed "RM10K+" instead of an
#                          exact figure. The number is then a LOWER BOUND: real
#                          GMV is somewhere above it. ~44% of rows arrive this way.
#   items_sold       int
#   category         str
#   level            int   creator level (Lv. 1-6)
#   avg_video_views  int
#   engagement_rate  str   e.g. "1.3%"
#   profile_url      str
#   fetched_at       str   ISO8601
DATA_CONTRACT = ("handle", "nickname", "followers", "gmv", "gmv_is_floor",
                 "items_sold", "category", "level", "avg_video_views",
                 "engagement_rate", "profile_url", "fetched_at")

app = Flask(__name__, static_folder=None)


def _num(value, cast, default=0):
    """Seller Center renders '1.2K', 'RM45,000', '12,345', 'RM10K+' — flatten it."""
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return cast(value)
    text = (str(value).strip().upper()
            .replace("RM", "").replace("$", "")
            .replace(",", "").replace("+", "").strip())
    mult = 1
    if text and text[-1] in "KMB":
        mult = {"K": 1_000, "M": 1_000_000, "B": 1_000_000_000}[text[-1]]
        text = text[:-1]
    try:
        return cast(float(text) * mult)
    except ValueError:
        return default


def _is_floor(row):
    """Trust an explicit flag from the source; otherwise sniff a trailing '+'."""
    if "gmv_is_floor" in row:
        v = row["gmv_is_floor"]
        if isinstance(v, str):
            return v.strip().lower() in ("true", "1", "yes")
        return bool(v)
    return "+" in str(row.get("gmv", ""))


def normalize(row):
    handle = str(row.get("handle", "")).strip().lstrip("@")
    return {
        "handle":          handle,
        "nickname":        str(row.get("nickname", "")).strip(),
        "followers":       _num(row.get("followers"), int),
        "gmv":             _num(row.get("gmv"), float, 0.0),
        "gmv_is_floor":    _is_floor(row),
        "items_sold":      _num(row.get("items_sold"), int),
        "category":        str(row.get("category", "")).strip(),
        "level":           _num(row.get("level"), int),
        "avg_video_views": _num(row.get("avg_video_views"), int),
        "engagement_rate": str(row.get("engagement_rate", "")).strip(),
        "profile_url":     str(row.get("profile_url", "")).strip(),
        "tiktok_user_id":  str(row.get("tiktok_user_id", "")).strip(),
        "fetched_at":      str(row.get("fetched_at", "")).strip(),
    }


def gmv_per_customer(row):
    """GMV divided by units sold — average price per item, in RM.

    Two caveats ride along with this number:

    1. TikTok exposes GMV and units sold but never a distinct-customer count, so
       this is spend per *item*, not per buyer. They agree only when each buyer
       takes one unit.
    2. When gmv_is_floor is set, GMV came through as "RM10K+" and the result is
       itself only a lower bound — the true figure can be far higher.

    Creators with no sales return 0.0 rather than dividing by zero, so they sort
    last.
    """
    items = row.get("items_sold")
    if items is None:
        return None          # API rows: items filtered at source, count unknown
    if not items:
        return 0.0
    return row["gmv"] / items


def decorate(row):
    gpc = gmv_per_customer(row)
    return {**row, "gmv_per_customer": None if gpc is None else round(gpc, 2)}


SCORED_METRICS = ("gmv", "gmv_per_customer", "followers", "items_sold")


def _percentiles(values):
    """Position each value 0-1 among the distinct values present; ties tie."""
    unique = sorted(set(values))
    if len(unique) < 2:
        return [0.5] * len(values)
    at = {v: i / (len(unique) - 1) for i, v in enumerate(unique)}
    return [at[v] for v in values]


def add_balanced_score(rows):
    """Score every creator 0-1 across all four metrics at once.

    Ranking on a single column buries creators who are strong overall but not
    top of that one column — a high GMV-per-customer seller with a small
    following, say. This scores each metric by percentile rather than raw value
    and averages the four, so one creator with a huge GMV can't swamp the scale.

    Computed across the whole stored pool, not just the current matches, so a
    creator's score doesn't move around when thresholds change.
    """
    if not rows:
        return rows
    columns = {k: _percentiles([r.get(k) or 0 for r in rows]) for k in SCORED_METRICS}
    for i, row in enumerate(rows):
        row["score"] = round(
            sum(columns[k][i] for k in SCORED_METRICS) / len(SCORED_METRICS), 4
        )
    return rows


# ── Routes ────────────────────────────────────────────────────────────────────
def load_template():
    if SETTINGS_JSON.exists():
        try:
            return json.loads(SETTINGS_JSON.read_text(encoding="utf-8")).get(
                "dm_template", DEFAULT_TEMPLATE)
        except (json.JSONDecodeError, OSError):
            pass
    return DEFAULT_TEMPLATE


@app.errorhandler(store.StoreError)
def store_error(e):
    return jsonify({"error": str(e)}), 500


@app.get("/")
def index():
    return send_from_directory(APP_DIR, "ui.html")


@app.get("/api/template")
def get_template():
    return jsonify({"dm_template": load_template()})


@app.post("/api/template")
def set_template():
    text = (request.get_json(silent=True) or {}).get("dm_template", "")
    SETTINGS_JSON.write_text(json.dumps({"dm_template": text}, indent=2),
                             encoding="utf-8")
    return jsonify({"dm_template": text})


@app.get("/api/creators")
def get_creators():
    rows = [decorate(r) for r in load_creators()]
    return jsonify({"count": len(rows), "creators": rows})


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


@app.post("/api/import_csv")
def import_csv():
    """Paste-a-CSV path. Header names must match the data contract."""
    text = (request.get_json(silent=True) or {}).get("csv", "")
    if not text.strip():
        return jsonify({"error": "no csv provided"}), 400
    reader = csv.DictReader(io.StringIO(text))
    rows = [dict(r) for r in reader]
    with app.test_request_context(json={"creators": rows}):
        return put_creators()


@app.delete("/api/creators")
def clear_creators():
    save_creators([])
    return jsonify({"total": 0})


# ── Messaging ─────────────────────────────────────────────────────────────────
# One shared app-controlled browser. Clicking Message drives it to open the DM
# and pre-fill the text; the human clicks Send. Created lazily on first use.
_messenger = None
_messenger_lock = threading.Lock()


def _get_messenger():
    global _messenger
    with _messenger_lock:
        if _messenger is None:
            from messenger import MessengerService   # late import: Playwright only when needed
            _messenger = MessengerService(region="MY")
        return _messenger


def _cache_user_id(handle, user_id):
    """Persist a freshly resolved id so the next click is instant."""
    store.set_user_id(handle, user_id)


@app.post("/api/message")
def open_message():
    """Open a creator's DM in the app browser with the draft pre-filled.

    Body: {handle, message, user_id?}. Never sends — the human clicks Send.
    """
    q = request.get_json(silent=True) or {}
    handle = str(q.get("handle", "")).strip().lstrip("@")
    message = str(q.get("message", ""))
    if not handle or not message:
        return jsonify({"error": "handle and message are required"}), 400

    result = _get_messenger().message(handle, q.get("user_id") or "", message)
    if result.get("user_id"):
        _cache_user_id(handle, result["user_id"])
    return jsonify(result)


@app.get("/api/message/state")
def messenger_state():
    m = _messenger
    if m is None:
        return jsonify({"state": "stopped", "detail": ""})
    return jsonify({"state": m.state, "detail": m.detail})


@app.post("/api/tiktok_login")
def tiktok_login():
    """Open tiktok.com login in the DM browser so the user signs into the account
    they DM from. One-time; the session sticks in the DM profile."""
    return jsonify(_get_messenger().open_login())


@app.get("/api/tiktok_login/state")
def tiktok_login_state():
    return jsonify(_get_messenger().login_state())


# GMV buckets mirror TikTok's own filter: each is [min, max). None max = open top.
GMV_BUCKETS = {
    "0-100":       (0, 100),
    "100-1000":    (100, 1_000),
    "1000-10000":  (1_000, 10_000),
    "10000+":      (10_000, None),
}
# Follower tiers, the standard influencer bands.
FOLLOWER_TIERS = {
    "nano":  (1_000, 10_000),
    "micro": (10_000, 100_000),
    "macro": (100_000, 1_000_000),
    "mega":  (1_000_000, None),
}


def _in_range(value, lo, hi):
    return value >= lo and (hi is None or value < hi)


def classify(row, gmv_range, follower_range, min_items, min_gmv_pc, category):
    """Sort a creator into match / uncertain / reject against the chosen filters.

    Category, follower tier and items sold are exact, so they gate hard. GMV can
    be a floor ("RM10K+"): a floored creator's true GMV is >= 10000, so it
    belongs only in the 10000+ bucket, never a lower one. GMV-per-customer is
    also a floor for those rows, so when it's the only failing gate the creator
    lands in "uncertain" rather than being dropped.
    """
    if category and row["category"].strip().lower() != category.strip().lower():
        return "reject"
    if follower_range and not _in_range(row["followers"], *follower_range):
        return "reject"
    items = row.get("items_sold")
    if items is not None and items < min_items:
        return "reject"

    if gmv_range:
        lo, hi = gmv_range
        if row["gmv_is_floor"]:
            # True GMV >= 10000, so it fits only an open-topped 10000+ bucket.
            if hi is not None:
                return "reject"
        elif not _in_range(row["gmv"], lo, hi):
            return "reject"

    gpc = row["gmv_per_customer"]
    if gpc is None or gpc >= min_gmv_pc:
        return "match"
    if row["gmv_is_floor"]:
        return "uncertain"   # its true per-customer figure could clear the bar
    return "reject"


@app.post("/api/search")
def search():
    """Filter the stored pool by category, GMV bucket, follower tier, items and
    GMV-per-customer, then rank and cap."""
    q = request.get_json(silent=True) or {}
    category       = str(q.get("category", "")).strip()
    gmv_range      = GMV_BUCKETS.get(q.get("gmv_bucket", ""))
    follower_range = FOLLOWER_TIERS.get(q.get("follower_tier", ""))
    min_items      = _num(q.get("min_items_sold"), int)
    min_gmv_pc     = _num(q.get("min_gmv_per_customer"), float, 0.0)
    limit          = _num(q.get("creators_to_reach"), int) or 25
    show_uncertain = q.get("include_uncertain", True)
    sort_key       = q.get("sort", "balanced")
    if sort_key not in SCORED_METRICS + ("balanced",):
        sort_key = "balanced"
    sort_field = "score" if sort_key == "balanced" else sort_key

    pool = add_balanced_score([decorate(r) for r in load_creators()])
    buckets = {"match": [], "uncertain": [], "reject": []}
    for row in pool:
        buckets[classify(row, gmv_range, follower_range, min_items,
                         min_gmv_pc, category)].append(row)

    for group in ("match", "uncertain"):
        buckets[group].sort(key=lambda r: r[sort_field], reverse=True)

    ordered = buckets["match"] + (buckets["uncertain"] if show_uncertain else [])

    return jsonify({
        "matched": len(buckets["match"]),
        "uncertain": len(buckets["uncertain"]),
        "returned": min(len(ordered), limit),
        "pool": len(pool),
        "currency": "RM",
        # Distinct categories present, so the UI can populate its dropdown.
        "all_categories": sorted({r["category"] for r in pool if r["category"]}),
        "creators": ordered[:limit],
    })


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


if __name__ == "__main__":
    url = f"http://localhost:{PORT}"
    print(f"Affiliate creator finder → {url}")
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    app.run(port=PORT, debug=False, threaded=True)
