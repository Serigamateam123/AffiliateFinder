import pytest
import tiktok_api as t


def test_base_string_sorts_and_excludes():
    base = t.build_base_string(
        "/x", {"b": "2", "a": 1, "sign": "junk", "access_token": "junk"}, '{"k":"v"}')
    assert base == '/xa1b2{"k":"v"}'


def test_sign_matches_hand_computed_hmac():
    # Golden vector derived independently of the implementation:
    #   printf '%s' 's3cret/xa1b2{"k":"v"}s3cret' | openssl dgst -sha256 -hmac 's3cret'
    expected = "9b60fee78cebd54f14abfa2a482c0f7c73e61ffd26cc669d4ec94bec1f34bc51"
    assert t.sign("s3cret", "/x", {"b": "2", "a": 1, "sign": "junk"}, '{"k":"v"}') == expected


def test_sign_requires_secret():
    with pytest.raises(t.AuthError):
        t.sign("", "/x", {})


def test_resolve_expiry_handles_both_conventions():
    assert t.resolve_expiry(1_800_000_000) == 1_800_000_000       # absolute epoch
    assert t.resolve_expiry(3600, now=1000.0) == 4600.0           # seconds remaining
