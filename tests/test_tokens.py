import json, threading, time
import pytest
import tiktok_api as t


def seed(tmp_path, monkeypatch, expires_in=-10):
    monkeypatch.setattr(t, "TOKENS_JSON", tmp_path / "tokens.json")
    t.save_tokens({"app_key": "k", "app_secret": "s",
                   "access_token": "old", "access_token_expires_at": time.time() + expires_in,
                   "refresh_token": "r0", "refresh_token_expires_at": time.time() + 9e7,
                   "shop_cipher": "cip", "region": "MY"})


class FakeResp:
    def __init__(self, payload, status=200):
        self._p, self.status_code = payload, status
    def json(self):
        return self._p


def test_missing_tokens_is_loud(tmp_path, monkeypatch):
    monkeypatch.setattr(t, "TOKENS_JSON", tmp_path / "nope.json")
    with pytest.raises(t.AuthError, match="bootstrap"):
        t.load_tokens()


def test_refresh_rotates_and_persists_before_returning(tmp_path, monkeypatch):
    seed(tmp_path, monkeypatch)
    monkeypatch.setattr(t.requests, "get", lambda *a, **kw: FakeResp(
        {"code": 0, "data": {"access_token": "new", "access_token_expire_in": 7200,
                             "refresh_token": "r1", "refresh_token_expire_in": 3.15e7}}))
    tok = t.get_access_token()
    assert tok["access_token"] == "new"
    on_disk = json.loads(t.TOKENS_JSON.read_text())
    assert on_disk["refresh_token"] == "r1"          # rotation persisted


def test_refresh_persist_failure_is_blocking(tmp_path, monkeypatch):
    seed(tmp_path, monkeypatch)
    monkeypatch.setattr(t.requests, "get", lambda *a, **kw: FakeResp(
        {"code": 0, "data": {"access_token": "new", "access_token_expire_in": 7200,
                             "refresh_token": "r1"}}))
    def boom(_):
        raise t.AuthError("PERSISTING ROTATED TOKENS FAILED")
    monkeypatch.setattr(t, "save_tokens", boom)
    with pytest.raises(t.AuthError, match="PERSISTING"):
        t.get_access_token()


def test_refresh_rejection_is_loud(tmp_path, monkeypatch):
    seed(tmp_path, monkeypatch)
    monkeypatch.setattr(t.requests, "get", lambda *a, **kw: FakeResp(
        {"code": 105002, "message": "refresh token expired"}))
    with pytest.raises(t.AuthError, match="bootstrap"):
        t.get_access_token()


def test_refresh_is_single_flight(tmp_path, monkeypatch):
    seed(tmp_path, monkeypatch)
    calls = []
    def slow_refresh(*a, **kw):
        calls.append(1)
        time.sleep(0.2)
        return FakeResp({"code": 0, "data": {"access_token": "new",
                         "access_token_expire_in": 7200, "refresh_token": "r1"}})
    monkeypatch.setattr(t.requests, "get", slow_refresh)
    threads = [threading.Thread(target=t.get_access_token) for _ in range(4)]
    [x.start() for x in threads]; [x.join() for x in threads]
    assert len(calls) == 1


def test_call_tiktok_attaches_cipher_and_checks_envelope(tmp_path, monkeypatch):
    seed(tmp_path, monkeypatch, expires_in=9999)
    seen = {}
    def fake_request(method, url, params=None, headers=None, data=None, timeout=None):
        seen.update(params=params, headers=headers, url=url)
        return FakeResp({"code": 0, "data": {"creators": []}})
    monkeypatch.setattr(t.requests, "request", fake_request)
    out = t.call_tiktok("POST", t.SEARCH_PATH, query={"page_size": 20}, body={})
    assert out == {"creators": []}
    assert seen["params"]["shop_cipher"] == "cip" and "sign" in seen["params"]
    assert seen["headers"]["x-tts-access-token"] == "old"


def test_call_tiktok_raises_apierror_with_code(tmp_path, monkeypatch):
    seed(tmp_path, monkeypatch, expires_in=9999)
    monkeypatch.setattr(t.requests, "request", lambda *a, **kw: FakeResp(
        {"code": 45101004, "message": "quota reached", "request_id": "rid"}))
    with pytest.raises(t.ApiError) as ei:
        t.call_tiktok("POST", t.SEARCH_PATH, body={})
    assert ei.value.code == 45101004
