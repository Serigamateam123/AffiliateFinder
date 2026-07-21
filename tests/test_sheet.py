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
