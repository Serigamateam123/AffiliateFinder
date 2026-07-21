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
    if not isinstance(cfg, dict):
        raise ConfigError("config.json must be a JSON object — compare with config.example.json")
    for k in REQUIRED_TOP:
        if k not in cfg:
            raise ConfigError(f"config.json is missing '{k}' — compare with config.example.json")
    if not isinstance(cfg["sheet"], dict):
        raise ConfigError("config.json 'sheet' must be an object — compare with config.example.json")
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
