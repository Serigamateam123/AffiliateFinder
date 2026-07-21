"""Creator discovery: official search API + sheet/store dedupe (spec §6)."""
import re
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
# 2026-07-20) — bodies may only be built from this registry, which lists
# ONLY fields verified in the spec §5 table. Do not add a field here
# without a live probe proving the API actually applies it.
KNOWN_BODY_FIELDS = frozenset({
    "search_key", "keyword", "follower_demographics", "gmv_ranges",
    "units_sold_ranges", "category", "advanced_filters",
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


_FLOOR_RE = re.compile(r"([\d.]+)\s*([KkMm]?)")


def bucket_floor(c):
    """Best-available GMV bucket floor for a creator row, or None.

    Prefers the numeric minimum_amount; falls back to parsing the
    formatted_range display string ("RM10K+" -> 10000.0, "RM1K-RM10K" ->
    1000.0 — the FIRST number is the lower bound). None means the row
    carries no bucket evidence at all.
    """
    gr = c.get("gmv_range") or {}
    if gr.get("minimum_amount") is not None:
        try:
            return float(gr["minimum_amount"])
        except (TypeError, ValueError):
            pass
    m = _FLOOR_RE.search(str(gr.get("formatted_range") or "").replace(",", ""))
    if not m:
        return None
    return float(m.group(1)) * {"k": 1_000, "m": 1_000_000}.get(m.group(2).lower(), 1)


def check_gmv_conformance(creators, requested_buckets):
    allowed_min = min(lo for name, lo, hi in GMV_BUCKETS if name in requested_buckets)
    for c in creators:
        floor = bucket_floor(c)
        if floor is None:
            raise ConformanceError(
                "TikTok returned a creator with no GMV bucket information — cannot "
                "verify the gmv_ranges filter was applied. Aborting rather than "
                "showing possibly-unfiltered results (spec F2).")
        if floor < allowed_min:
            raise ConformanceError(
                "TikTok returned a creator below the requested GMV bucket "
                f"(bucket floor {floor} < requested {allowed_min}) — the gmv_ranges filter was "
                "not applied. Aborting rather than showing unfiltered results (spec F2).")


def norm_handle(h):
    return str(h or "").strip().lstrip("@").lower()


def to_store_row(c, now_iso):
    handle = norm_handle(c.get("username"))
    exact = (c.get("gmv") or {}).get("amount")
    # exact — the API returns the real figure for most creators even where the
    # UI shows "RM10K+", but HIDES it for some (live finding 2026-07-21): those
    # rows carry only the bucket, so we store the bucket floor with
    # gmv_is_floor=True (same convention as legacy scraped rows).
    # Currency labels are unreliable (USD label on RM values); amounts are
    # treated as RM per spec §5 Gotcha 2 (selection_region MY).
    return {
        "handle": handle,
        "nickname": str(c.get("nickname") or ""),
        "creator_open_id": str(c.get("creator_open_id") or ""),
        "followers": int(c.get("follower_count") or 0),
        "gmv": float(exact) if exact is not None else (bucket_floor(c) or 0.0),
        "gmv_is_floor": exact is None,
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
            if row["creator_open_id"]:
                known_ids.add(row["creator_open_id"])
            new_rows.append(row)
            if len(new_rows) >= want:
                break
        search_key = data.get("search_key") or search_key
        page_token = data.get("next_page_token") or ""
        if not page_token or len(new_rows) >= want:
            break
        page_sleep(0.25)

    if new_rows:
        store.upsert_creators(new_rows)
    return {"added": len(new_rows), "skipped_contacted": skipped_contacted,
            "skipped_known": skipped_known, "pages_used": pages,
            "exhausted": len(new_rows) < want}
