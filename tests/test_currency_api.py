"""USD/CAD rate and daily cache tests without network requests."""

from datetime import datetime
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pandas as pd

from local_api import app, currency_cache
from yfinance.exceptions import YFRateLimitError


class CurrencyApiTests(unittest.TestCase):
    def setUp(self):
        currency_cache.clear()
        self.client = app.test_client()
        self.network = patch("local_api.yf.Ticker")
        self.ticker = self.network.start()
        self.addCleanup(self.network.stop)
        self.history = self.ticker.return_value.history
        self.history.return_value = pd.DataFrame(
            {"Close": [1.39, 1.40]},
            index=pd.DatetimeIndex(["2026-11-09", "2026-11-10"], tz="Europe/London"),
        )
        self.clock = patch("local_api.datetime")
        self.time = self.clock.start()
        self.addCleanup(self.clock.stop)
        self.time.now.return_value = datetime(2026, 11, 11, 12, tzinfo=ZoneInfo("America/Toronto"))

    def test_rate_and_source_date(self):
        response = self.client.get("/currency/rate")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {
            "base": "USD", "quote": "CAD", "rate": 1.4,
            "rate_date": "2026-11-10", "cached_date": "2026-11-11",
            "cache_timezone": "America/Toronto",
        })
        self.ticker.assert_called_once_with("USDCAD=X")
        self.history.assert_called_once_with(period="5d", interval="1d",
                                            auto_adjust=False, actions=False, timeout=10)

    def test_cache_lasts_until_midnight(self):
        original = self.client.get("/currency/rate").get_json()
        self.time.now.return_value = datetime(2026, 11, 11, 23, 59)
        self.assertEqual(self.client.get("/currency/rate").get_json(), original)
        self.assertEqual(self.history.call_count, 1)
        self.time.now.assert_called_with(ZoneInfo("America/Toronto"))

    def test_refreshes_on_next_calendar_day(self):
        self.client.get("/currency/rate")
        self.time.now.return_value = datetime(2026, 11, 12, 0, 0)
        self.history.return_value.iloc[-1, 0] = 1.42
        body = self.client.get("/currency/rate").get_json()
        self.assertEqual(body["cached_date"], "2026-11-12")
        self.assertEqual(body["rate"], 1.42)
        self.assertEqual(self.history.call_count, 2)

    def test_failed_fetch_is_not_cached(self):
        self.history.side_effect = RuntimeError("upstream")
        with self.assertLogs(app.logger, level="ERROR"):
            self.assertEqual(self.client.get("/currency/rate").status_code, 502)
        self.assertEqual(currency_cache, {})
        self.history.side_effect = None
        self.assertEqual(self.client.get("/currency/rate").status_code, 200)

    def test_rate_limit(self):
        self.history.side_effect = YFRateLimitError()
        self.assertEqual(self.client.get("/currency/rate").status_code, 429)
        self.assertEqual(currency_cache, {})

    def test_invalid_rates_are_not_cached(self):
        for value in (float("nan"), float("inf"), 0.0, -1.0):
            self.history.return_value = pd.DataFrame(
                {"Close": [value]}, index=pd.DatetimeIndex(["2026-11-11"]),
            )
            with self.assertLogs(app.logger, level="ERROR"):
                self.assertEqual(self.client.get("/currency/rate").status_code, 502)
            self.assertEqual(currency_cache, {})

    def test_old_conversion_endpoint_removed(self):
        self.assertEqual(self.client.get("/currency/convert").status_code, 404)


if __name__ == "__main__":
    unittest.main()
