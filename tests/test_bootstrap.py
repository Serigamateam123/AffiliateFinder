import pytest
import bootstrap_auth as b
import tiktok_api as t


class FakeResp:
    def __init__(self, payload, status=200):
        self._p, self.status_code = payload, status
    def json(self):
        return self._p


def ok_payload(scopes):
    return {"code": 0, "data": {"access_token": "a", "refresh_token": "r",
            "access_token_expire_in": 7200, "refresh_token_expire_in": 3.15e7,
            "granted_scopes": scopes}}


def test_exchange_aborts_without_required_scope():
    with pytest.raises(t.AuthError, match="seller.creator_marketplace.read"):
        b.exchange("code", "k", "s",
                   http_get=lambda *a, **kw: FakeResp(ok_payload(["seller.order.info"])))


def test_exchange_returns_token_record_with_scope():
    rec = b.exchange("code", "k", "s",
                     http_get=lambda *a, **kw: FakeResp(
                         ok_payload(["seller.creator_marketplace.read"])))
    assert rec["access_token"] == "a" and rec["refresh_token"] == "r"
    assert rec["app_key"] == "k" and rec["app_secret"] == "s"


def test_exchange_rejects_envelope_error():
    with pytest.raises(t.AuthError, match="single-use"):
        b.exchange("code", "k", "s",
                   http_get=lambda *a, **kw: FakeResp({"code": 36004004, "message": "auth_code expired"}))


def test_auth_url():
    assert b.auth_url("777") == "https://services.tiktokshop.com/open/authorize?service_id=777"


class FakeClock:
    """Drives probe()'s time.time()/time.sleep() so retries cost no wall-clock."""
    def __init__(self):
        self.now = 0.0
        self.sleeps = []
    def time(self):
        return self.now
    def sleep(self, sec):
        self.sleeps.append(sec)
        self.now += sec


@pytest.fixture()
def clock(monkeypatch):
    c = FakeClock()
    monkeypatch.setattr(b.time, "time", c.time)
    monkeypatch.setattr(b.time, "sleep", c.sleep)
    return c


def test_probe_retries_105005_until_scope_propagates(clock, monkeypatch):
    calls = []
    def search(_):
        calls.append(1)
        if len(calls) < 3:
            raise t.ApiError(105005, "scope not propagated")
        return {"creators": [{}]}
    monkeypatch.setattr(t, "search_creators_page", search)
    b.probe(deadline_sec=90)
    assert len(calls) == 3 and clock.sleeps == [10, 10]


def test_probe_gives_up_at_deadline(clock, monkeypatch):
    monkeypatch.setattr(t, "search_creators_page",
                        lambda _: (_ for _ in ()).throw(t.ApiError(105005, "still propagating")))
    with pytest.raises(t.ApiError) as ei:
        b.probe(deadline_sec=90)
    assert ei.value.code == 105005
    # 9 sleeps × 10s reaches the 90s deadline; the 10th failure must raise, not wait forever
    assert clock.sleeps == [10] * 9


def test_probe_raises_other_api_errors_immediately(clock, monkeypatch):
    monkeypatch.setattr(t, "search_creators_page",
                        lambda _: (_ for _ in ()).throw(t.ApiError(36009001, "unauthorized")))
    with pytest.raises(t.ApiError) as ei:
        b.probe(deadline_sec=90)
    assert ei.value.code == 36009001 and clock.sleeps == []
