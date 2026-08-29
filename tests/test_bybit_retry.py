from src.providers.bybit_public import BybitPublicProvider


class FakeResponse:
    def __init__(self, payload, status_code=200, headers=None):
        self._payload = payload
        self.status_code = status_code
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(f"status={self.status_code}")

    def json(self):
        return self._payload


def test_bybit_retries_retcode_10006(monkeypatch):
    calls = []
    responses = [
        FakeResponse({"retCode": 10006, "retMsg": "Too many visits", "result": {}}),
        FakeResponse({"retCode": 0, "retMsg": "OK", "result": {"list": [{"symbol": "BTCUSDT"}]}}),
    ]

    def fake_get(*args, **kwargs):
        calls.append((args, kwargs))
        return responses.pop(0)

    sleeps = []
    monkeypatch.setattr("src.providers.bybit_public.requests.get", fake_get)
    monkeypatch.setattr("src.providers.bybit_public.time.sleep", lambda seconds: sleeps.append(seconds))

    provider = BybitPublicProvider(pause=0, max_retries=2, backoff_base=0.01)
    result = provider._get("/v5/market/tickers", {"category": "linear"})

    assert result["list"][0]["symbol"] == "BTCUSDT"
    assert len(calls) == 2
    assert sleeps[0] >= 0.01


def test_bybit_retries_http_429(monkeypatch):
    calls = []
    responses = [
        FakeResponse({}, status_code=429),
        FakeResponse({"retCode": 0, "retMsg": "OK", "result": {"list": []}}),
    ]

    def fake_get(*args, **kwargs):
        calls.append((args, kwargs))
        return responses.pop(0)

    monkeypatch.setattr("src.providers.bybit_public.requests.get", fake_get)
    monkeypatch.setattr("src.providers.bybit_public.time.sleep", lambda seconds: None)

    provider = BybitPublicProvider(pause=0, max_retries=2, backoff_base=0)
    result = provider._get("/v5/market/tickers", {"category": "linear"})

    assert result == {"list": []}
    assert len(calls) == 2
