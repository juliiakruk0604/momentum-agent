import tempfile
import unittest
from pathlib import Path

from alpha_breakout_watch import TokenConfig, evaluate_setup, run


TOKEN = TokenConfig(
    symbol="TEST",
    address="0xabc",
    breakout=1.30,
    retest_low=1.27,
    retest_high=1.30,
    invalidation=1.18,
    targets=(1.47,),
)


def candle(timestamp, close, volume, low=None):
    return {
        "timestamp": timestamp,
        "open": close,
        "high": close * 1.01,
        "low": close if low is None else low,
        "close": close,
        "volume": volume,
    }


class EvaluateSetupTests(unittest.TestCase):
    def test_confirms_close_volume_and_retest(self):
        candles = [
            candle(0, 1.20, 100),
            candle(14400, 1.31, 160),
            candle(28800, 1.32, 80, low=1.28),
        ]
        signal = evaluate_setup(TOKEN, candles, current_price=1.32, now=30000)
        self.assertIsNotNone(signal)
        self.assertAlmostEqual(signal["volume_change_pct"], 60.0)

    def test_rejects_unclosed_breakout_candle(self):
        candles = [candle(0, 1.20, 100), candle(14400, 1.31, 160)]
        self.assertIsNone(evaluate_setup(TOKEN, candles, 1.32, now=20000))

    def test_rejects_breakout_without_higher_volume(self):
        candles = [
            candle(0, 1.20, 100),
            candle(14400, 1.31, 99),
            candle(28800, 1.32, 80, low=1.28),
        ]
        self.assertIsNone(evaluate_setup(TOKEN, candles, 1.32, now=30000))

    def test_rejects_retest_that_loses_zone(self):
        candles = [
            candle(0, 1.20, 100),
            candle(14400, 1.31, 160),
            candle(28800, 1.20, 80, low=1.20),
        ]
        self.assertIsNone(evaluate_setup(TOKEN, candles, 1.20, now=30000))


class DeduplicationTests(unittest.TestCase):
    def test_existing_event_is_not_written_again(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.json"
            output = Path(directory) / "latest.json"
            state.write_text('{"fired_event_keys": []}', encoding="utf-8")

            def fetcher(url):
                if "dexscreener" in url:
                    return {
                        "pairs": [
                            {
                                "chainId": "bsc",
                                "pairAddress": "0xpool",
                                "baseToken": {"address": "0x02e75d28a8aa2a0033b8cf866fcf0bb0e1ee4444"},
                                "quoteToken": {"address": "0xquote"},
                                "liquidity": {"usd": 100000},
                                "priceUsd": "0.00132",
                                "url": "https://dexscreener.com/bsc/0xpool",
                            }
                        ]
                    }
                return {
                    "data": {
                        "attributes": {
                            "ohlcv_list": [
                                [28800, 0.00132, 0.00133, 0.00128, 0.00132, 80],
                                [14400, 0.00129, 0.00134, 0.00129, 0.00131, 160],
                                [0, 0.00120, 0.00125, 0.00119, 0.00120, 100],
                            ]
                        }
                    }
                }

            first = run(state, output, fetcher=fetcher, now=30000)
            second = run(state, output, fetcher=fetcher, now=30000)
            self.assertEqual(len(first), 1)
            self.assertEqual(second, [])


if __name__ == "__main__":
    unittest.main()
