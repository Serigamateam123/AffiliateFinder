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


def test_non_dict_config_is_loud():
    config.CONFIG_JSON.write_text("[1, 2, 3]")
    with pytest.raises(config.ConfigError, match="JSON object"):
        config.load()


def test_null_sheet_is_loud():
    cfg = good()
    cfg["sheet"] = None
    config.CONFIG_JSON.write_text(json.dumps(cfg))
    with pytest.raises(config.ConfigError, match="sheet"):
        config.load()
