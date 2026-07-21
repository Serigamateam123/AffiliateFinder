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
