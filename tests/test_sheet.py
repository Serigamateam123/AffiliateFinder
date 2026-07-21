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


def test_non_json_response_is_sheet_error(monkeypatch, tmp_path):
    import requests as req

    class FakeResp:
        status_code = 200
        text = "<html>gateway error</html>"
        def json(self):
            raise ValueError("No JSON object could be decoded")

    sa = tmp_path / "sa.json"
    sa.write_text("{}")
    monkeypatch.setattr(sheet, "APP_DIR", tmp_path)

    class FakeCreds:
        token = "tok"
        def refresh(self, _):
            return None

    import google.oauth2.service_account as gsa
    monkeypatch.setattr(gsa.Credentials, "from_service_account_file",
                        classmethod(lambda cls, *a, **kw: FakeCreds()))
    monkeypatch.setattr(req, "get", lambda *a, **kw: FakeResp())
    cfg = {"service_account_file": "sa.json", "outreach_tab": "Outreach",
           "handle_column": "B", "handle_header": "Handle", "sheet_id": "x"}
    with pytest.raises(sheet.SheetError, match="non-JSON"):
        sheet.contacted_handles(cfg)


# --- header-scan + xlsx-path coverage (Office-file support) -----------------

def test_parse_header_below_banner_rows():
    values = [["Working Year"], ["Creator Username"], ["abc"], [None], ["def "]]
    assert sheet.parse_contacted(values, "Creator Username", "KOC!D") == ["abc", "def"]


def test_parse_none_cells_never_become_handles():
    values = [["Handle"], [None], ["abc"], [None]]
    assert sheet.parse_contacted(values, "Handle", "Outreach!B") == ["abc"]


def test_parse_header_beyond_scan_window_is_blocking():
    values = [["banner"]] * sheet.HEADER_SCAN_ROWS + [["Handle"], ["abc"]]
    with pytest.raises(sheet.SheetError, match="Wrong tab/column"):
        sheet.parse_contacted(values, "Handle", "Outreach!B")


@pytest.fixture()
def fake_creds(monkeypatch, tmp_path):
    (tmp_path / "sa.json").write_text("{}")
    monkeypatch.setattr(sheet, "APP_DIR", tmp_path)

    class FakeCreds:
        token = "tok"
        def refresh(self, _):
            return None

    import google.oauth2.service_account as gsa
    monkeypatch.setattr(gsa.Credentials, "from_service_account_file",
                        classmethod(lambda cls, *a, **kw: FakeCreds()))


def _cfg():
    return {"service_account_file": "sa.json", "outreach_tab": "KOC Summary 2026",
            "handle_column": "D", "handle_header": "Creator Username", "sheet_id": "x"}


class JsonResp:
    status_code = 200
    text = ""
    def __init__(self, payload):
        self._p = payload
    def json(self):
        return self._p


class BytesResp:
    status_code = 200
    text = ""
    def __init__(self, content):
        self.content = content
    def json(self):
        raise ValueError("binary")


def _xlsx_bytes(tab, rows):
    import io
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = tab
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_contacted_handles_reads_xlsx_via_drive(fake_creds, monkeypatch):
    content = _xlsx_bytes("KOC Summary 2026", [
        ["Working Year", 2026, "Working Month", "July", 0, ""],
        ["Month", "Date", "Sent", "Creator Username", "Followers", "Status"],
        ["July", "", "", "sarasulaiman03", 4547, "Sent Out"],
        ["July", "", "", None, None, ""],
        ["July", "", "", "ftmaaisyh", 1407, "Sent Out"],
    ])
    def fake_get(url, **kw):
        if "fields=mimeType" in url:
            return JsonResp({"mimeType": sheet._XLSX_MIME})
        assert "alt=media" in url
        return BytesResp(content)
    monkeypatch.setattr(sheet.requests, "get", fake_get)
    assert sheet.contacted_handles(_cfg()) == ["sarasulaiman03", "ftmaaisyh"]


def test_contacted_handles_xlsx_missing_tab_is_blocking(fake_creds, monkeypatch):
    content = _xlsx_bytes("Wrong Tab", [["Creator Username"], ["abc"]])
    def fake_get(url, **kw):
        if "fields=mimeType" in url:
            return JsonResp({"mimeType": sheet._XLSX_MIME})
        return BytesResp(content)
    monkeypatch.setattr(sheet.requests, "get", fake_get)
    with pytest.raises(sheet.SheetError, match="tab 'KOC Summary 2026' not found"):
        sheet.contacted_handles(_cfg())


def test_contacted_handles_native_sheet_uses_sheets_api(fake_creds, monkeypatch):
    def fake_get(url, **kw):
        if "fields=mimeType" in url:
            return JsonResp({"mimeType": sheet._GOOGLE_SHEET_MIME})
        assert "sheets.googleapis.com" in url
        return JsonResp({"values": [["Creator Username"], ["abc"], ["def"]]})
    monkeypatch.setattr(sheet.requests, "get", fake_get)
    assert sheet.contacted_handles(_cfg()) == ["abc", "def"]


def test_contacted_handles_unsupported_mime_is_blocking(fake_creds, monkeypatch):
    monkeypatch.setattr(sheet.requests, "get",
                        lambda url, **kw: JsonResp({"mimeType": "application/pdf"}))
    with pytest.raises(sheet.SheetError, match="unsupported outreach file type"):
        sheet.contacted_handles(_cfg())
