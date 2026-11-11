"""Deterministic HTTP contract tests; no Yahoo requests."""

from datetime import datetime
import unittest
from unittest.mock import PropertyMock, patch
from zoneinfo import ZoneInfo

import pandas as pd

from local_api import app, price_cache, profile_cache
from yfinance.exceptions import YFRateLimitError


class LocalApiTests(unittest.TestCase):
    def setUp(self):
        price_cache.clear()
        profile_cache.clear()
        self.info = {"sector": "Technology", "industry": "Consumer Electronics"}
        self.client = app.test_client()
        self.zone = ZoneInfo("America/New_York")
        self.now = datetime(2026, 11, 11, 11, 15, tzinfo=self.zone)
        self.metadata = {
            "exchangeTimezoneName": "America/New_York", "currency": "USD",
            "regularMarketPrice": 251.5, "regularMarketTime": int(self.now.timestamp()),
            "currentTradingPeriod": {"regular": {
                "end": datetime(2026, 11, 11, 16, tzinfo=self.zone),
            }},
        }
        self.frame = pd.DataFrame(
            {"Close": [248.0, 250.0, 251.0]},
            index=pd.DatetimeIndex([
                "2026-11-10 15:30", "2026-11-11 09:30", "2026-11-11 10:30",
            ], tz=self.zone),
        )

    def fetch(self, query="symbol=aapl"):
        with patch("local_api.yf.Ticker") as ticker, patch("local_api.datetime") as clock:
            clock.now.return_value = self.now
            clock.fromtimestamp.side_effect = datetime.fromtimestamp
            ticker.return_value.history.return_value = self.frame
            ticker.return_value.get_history_metadata.return_value = self.metadata
            self.info_mock = PropertyMock(return_value=self.info)
            type(ticker.return_value).info = self.info_mock
            response = self.client.get("/history/today?" + query)
            ticker.return_value.history.assert_called_once_with(
                period="5d", interval="1h", prepost=False,
                auto_adjust=False, actions=False, timeout=10,
            )
            return response

    def test_completed_bars_and_latest_quote(self):
        response = self.fetch()
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body["symbol"], "AAPL")
        self.assertEqual(body["sector"], "Technology")
        self.assertEqual(body["industry"], "Consumer Electronics")
        self.assertEqual(body["history"], [
            {"timestamp": "2026-11-11T09:30:00-05:00", "close": 250.0},
        ])
        self.assertEqual(body["latest"], {
            "price": 251.5, "timestamp": "2026-11-11T11:15:00-05:00",
        })

    def test_closed_day_keeps_original_quote_timestamp(self):
        self.now = datetime(2026, 11, 14, 12, tzinfo=self.zone)
        body = self.fetch().get_json()
        self.assertEqual(body["history"], [])
        self.assertEqual(body["latest"]["timestamp"], "2026-11-11T11:15:00-05:00")

    def test_final_short_bar_is_complete_at_session_end(self):
        self.now = datetime(2026, 11, 11, 16, tzinfo=self.zone)
        self.frame = pd.DataFrame({"Close": [252.0]}, index=pd.DatetimeIndex(
            ["2026-11-11 15:30"], tz=self.zone))
        self.assertEqual(len(self.fetch().get_json()["history"]), 1)

    def test_missing_quote_is_null(self):
        del self.metadata["regularMarketTime"]
        self.assertIsNone(self.fetch().get_json()["latest"])

    def test_formatted_yfinance_quote_timestamp(self):
        self.metadata["regularMarketTime"] = pd.Timestamp(self.now)
        self.assertEqual(self.fetch().get_json()["latest"]["timestamp"],
                         "2026-11-11T11:15:00-05:00")

    def test_symbol_validation(self):
        for query in ("", "?symbol=", "?symbol=bad/symbol"):
            self.assertEqual(self.client.get("/history/today" + query).status_code, 400)

    def test_cache_reuses_normalized_symbol_until_expiry(self):
        with patch("local_api.monotonic", return_value=100):
            original = self.fetch().get_json()
        with patch("local_api.yf.Ticker") as ticker, patch("local_api.datetime") as clock:
            clock.now.return_value = self.now
            with patch("local_api.monotonic", return_value=699):
                response = self.client.get("/history/today?symbol=AAPL")
            self.assertEqual(response.get_json(), original)
            ticker.assert_not_called()
        with patch("local_api.monotonic", return_value=700):
            self.fetch()  # fetch asserts that Yahoo history is requested again.

    def test_cache_is_separate_per_symbol(self):
        self.fetch()
        with patch("local_api.yf.Ticker", side_effect=YFRateLimitError()) as ticker:
            response = self.client.get("/history/today?symbol=MSFT")
        self.assertEqual(response.status_code, 429)
        ticker.assert_called_once_with("MSFT")

    def test_explicit_false_uses_cached_response(self):
        self.fetch()
        with patch("local_api.yf.Ticker") as ticker, patch("local_api.datetime") as clock:
            clock.now.return_value = self.now
            response = self.client.get("/history/today?symbol=AAPL&invalidate_cache=false")
        self.assertEqual(response.status_code, 200)
        ticker.assert_not_called()

    def test_invalidation_refreshes_only_requested_symbol(self):
        self.fetch("symbol=MSFT")
        self.fetch()
        self.metadata["regularMarketPrice"] = 260.0
        with patch("local_api.monotonic", return_value=1000):
            response = self.fetch("symbol=AAPL&invalidate_cache=true")
        self.assertEqual(response.get_json()["latest"]["price"], 260.0)
        self.assertEqual(price_cache["AAPL"][0], 1600)
        self.assertEqual(price_cache["AAPL"][1], response.get_json())
        self.assertEqual(price_cache["MSFT"][1]["latest"]["price"], 251.5)

    def test_invalidation_without_cached_value(self):
        self.assertEqual(self.fetch("symbol=AAPL&invalidate_cache=true").status_code, 200)

    def test_failed_refresh_removes_old_cached_value(self):
        self.fetch()
        with patch("local_api.yf.Ticker", side_effect=YFRateLimitError()):
            response = self.client.get("/history/today?symbol=AAPL&invalidate_cache=true")
        self.assertEqual(response.status_code, 429)
        self.assertNotIn("AAPL", price_cache)

    def test_invalid_cache_option_is_rejected(self):
        with patch("local_api.yf.Ticker") as ticker:
            response = self.client.get("/history/today?symbol=AAPL&invalidate_cache=yes")
        self.assertEqual(response.status_code, 400)
        ticker.assert_not_called()

    def test_cache_expires_at_exchange_midnight(self):
        self.fetch()
        self.now = datetime(2026, 11, 12, 0, 1, tzinfo=self.zone)
        self.assertEqual(self.fetch().get_json()["history"], [])

    def test_company_details_survive_price_refresh(self):
        self.fetch()
        self.info = {"sector": "Changed", "industry": "Changed"}
        body = self.fetch("symbol=AAPL&invalidate_cache=true").get_json()
        self.assertEqual(body["sector"], "Technology")
        self.info_mock.assert_not_called()

    def test_company_details_refresh_on_new_exchange_date(self):
        self.fetch()
        self.now = datetime(2026, 11, 12, 0, 1, tzinfo=self.zone)
        self.info = {"sector": "Changed", "industry": "Updated"}
        body = self.fetch().get_json()
        self.assertEqual(body["sector"], "Changed")
        self.assertEqual(body["industry"], "Updated")
        self.info_mock.assert_called_once()

    def test_missing_company_details_are_null(self):
        self.info = {}
        body = self.fetch().get_json()
        self.assertIsNone(body["sector"])
        self.assertIsNone(body["industry"])

    def test_profile_error_preserves_prices_and_can_retry(self):
        with patch("local_api.yf.Ticker") as ticker, patch("local_api.datetime") as clock:
            clock.now.return_value = self.now
            clock.fromtimestamp.side_effect = datetime.fromtimestamp
            ticker.return_value.history.return_value = self.frame
            ticker.return_value.get_history_metadata.return_value = self.metadata
            type(ticker.return_value).info = PropertyMock(side_effect=YFRateLimitError())
            with self.assertLogs(app.logger, level="WARNING"):
                response = self.client.get("/history/today?symbol=AAPL")
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.get_json()["sector"])
        self.assertIsNone(response.get_json()["industry"])
        self.assertEqual(response.get_json()["latest"]["price"], 251.5)
        self.assertNotIn("AAPL", profile_cache)
        body = self.fetch("symbol=AAPL&invalidate_cache=true").get_json()
        self.assertEqual(body["sector"], "Technology")

    def test_failed_requests_are_not_cached(self):
        with patch("local_api.yf.Ticker", side_effect=YFRateLimitError()) as ticker:
            for _ in range(2):
                self.assertEqual(self.client.get("/history/today?symbol=AAPL").status_code, 429)
        self.assertEqual(ticker.call_count, 2)

    def test_upstream_errors_are_json(self):
        for error, status in ((YFRateLimitError(), 429), (RuntimeError("upstream"), 502)):
            with patch("local_api.yf.Ticker", side_effect=error):
                response = self.client.get("/history/today?symbol=AAPL")
                self.assertEqual(response.status_code, status)
                self.assertIn("error", response.get_json())


if __name__ == "__main__":
    unittest.main()
