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
    range_ref = f"'{tab}'!{col}:{col}"
    url = (f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_cfg['sheet_id']}"
           f"/values/{quote(range_ref)}")
    try:
        r = requests.get(url, headers={"Authorization": f"Bearer {creds.token}"}, timeout=30)
    except requests.RequestException as e:
        raise SheetError(f"could not reach Google Sheets: {e}") from e
    if r.status_code != 200:
        raise SheetError(f"Sheets API HTTP {r.status_code}: {r.text[:200]}")
    try:
        payload = r.json()
    except ValueError as e:
        raise SheetError(f"Sheets API returned a non-JSON response: {e}") from e
    return parse_contacted(payload.get("values") or [], sheet_cfg["handle_header"], where)
