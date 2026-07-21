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
