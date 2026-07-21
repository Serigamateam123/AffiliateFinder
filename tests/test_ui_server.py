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


def test_search_sorts_survive_mixed_pool(client):
    legacy_row = {"handle": "legacyrow", "nickname": "Legacy", "followers": 20_000,
                  "gmv": 5_000.0, "gmv_is_floor": False, "items_sold": 200,
                  "category": "", "level": 1, "avg_video_views": 500,
                  "engagement_rate": "1.0%", "profile_url": "", "tiktok_user_id": "",
                  "fetched_at": "2026-07-20"}
    store.save_creators([legacy_row, api_row()])
    for sort_value in ("balanced", "followers", "gmv", "items_sold", "gmv_per_customer"):
        r = client.post("/api/search", json={"sort": sort_value})
        assert r.status_code == 200, sort_value
        assert len(r.get_json()["creators"]) == 2, sort_value


# _is_floor truth table: explicit flags from stored rows (bool or CSV-era
# strings) always win; rows with no flag fall back to sniffing a trailing
# '+' in the GMV text ("RM10K+" style legacy CSV imports).
@pytest.mark.parametrize("row, expected", [
    ({"gmv_is_floor": True,    "gmv": "100"},    True),
    ({"gmv_is_floor": False,   "gmv": "RM10K+"}, False),  # explicit flag beats the sniff
    ({"gmv_is_floor": "true",  "gmv": "100"},    True),
    ({"gmv_is_floor": " TRUE ","gmv": "100"},    True),
    ({"gmv_is_floor": "1",     "gmv": "100"},    True),
    ({"gmv_is_floor": "yes",   "gmv": "100"},    True),
    ({"gmv_is_floor": "false", "gmv": "RM10K+"}, False),
    ({"gmv_is_floor": "0",     "gmv": "RM10K+"}, False),
    ({"gmv_is_floor": "",      "gmv": "RM10K+"}, False),
    ({"gmv_is_floor": 1},                        True),
    ({"gmv_is_floor": 0},                        False),
    ({"gmv": "RM10K+"},                          True),   # no flag: sniff the '+'
    ({"gmv": "12345"},                           False),
    ({"gmv": 12345.0},                           False),
    ({},                                         False),
])
def test_is_floor_truth_table(row, expected):
    assert ui_server._is_floor(row) is expected
