from src.execution.exchange_constraints import bybit_linear_constraints


class FakeResponse:
    def raise_for_status(self):
        return None

    def json(self):
        return {
            "retCode": 0,
            "result": {
                "list": [
                    {
                        "symbol": "TESTUSDT",
                        "status": "Trading",
                        "contractType": "LinearPerpetual",
                        "quoteCoin": "USDT",
                        "lotSizeFilter": {
                            "minNotionalValue": "5",
                            "minOrderQty": "0.1",
                            "maxOrderQty": "1000",
                            "qtyStep": "0.1",
                        },
                        "priceFilter": {"tickSize": "0.001"},
                    }
                ]
            },
        }


def test_bybit_linear_constraints_parses_filters(monkeypatch):
    def fake_get(*args, **kwargs):
        return FakeResponse()

    monkeypatch.setattr("src.execution.exchange_constraints.requests.get", fake_get)
    result = bybit_linear_constraints("TESTUSDT")
    assert result["status"] == "Trading"
    assert result["min_notional_usdt"] == 5.0
    assert result["min_order_qty"] == 0.1
    assert result["qty_step"] == 0.1
    assert result["tick_size"] == 0.001
