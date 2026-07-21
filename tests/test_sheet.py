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
