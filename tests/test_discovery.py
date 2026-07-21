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


def api_creator_floor_only(handle, followers=5000):
    """Shape B (live 2026-07-21): exact GMV hidden — bucket string only."""
    return {"username": handle, "nickname": "N " + handle, "creator_open_id": "id_" + handle,
            "follower_count": followers, "gmv": None,
            "gmv_range": {"formatted_range": "RM10K+"},
            "video_gmv": None, "live_gmv": None,
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


def test_known_body_fields_is_exactly_the_verified_registry():
    # Trust anchor: only fields verified live (spec §5) may appear here.
    assert d.KNOWN_BODY_FIELDS == {
        "search_key", "keyword", "follower_demographics", "gmv_ranges",
        "units_sold_ranges", "category", "advanced_filters"}


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


def test_bucket_floor_parses_all_evidence_shapes():
    assert d.bucket_floor(api_creator("a", floor_min="10000")) == 10000.0
    assert d.bucket_floor(api_creator_floor_only("a")) == 10000.0
    assert d.bucket_floor({"gmv_range": {"formatted_range": "RM1K-RM10K"}}) == 1000.0
    assert d.bucket_floor({"gmv_range": {}}) is None
    assert d.bucket_floor({}) is None


def test_conformance_accepts_floor_only_rows_and_rejects_no_evidence():
    d.check_gmv_conformance([api_creator_floor_only("a")],
                            ["GMV_RANGE_1000_10000", "GMV_RANGE_10000_AND_ABOVE"])  # no raise
    with pytest.raises(d.ConformanceError, match="no GMV bucket information"):
        d.check_gmv_conformance([{"username": "x"}],
                                ["GMV_RANGE_1000_10000", "GMV_RANGE_10000_AND_ABOVE"])


def test_floor_only_row_stored_as_floor_and_qualifies():
    row = d.to_store_row(api_creator_floor_only("Sha"), "2026-07-21T00:00:00+00:00")
    assert row["gmv"] == 10000.0 and row["gmv_is_floor"] is True
    assert d._qualifies(row, {"min_followers": 1000, "max_followers": None, "min_gmv": 1000})


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
