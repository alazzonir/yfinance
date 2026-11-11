"""Earnings endpoint tests using deterministic Yahoo fixtures."""

from datetime import datetime
import unittest
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pandas as pd

from local_api import app
from yfinance.exceptions import YFRateLimitError


class EarningsApiTests(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()
        self.zone = ZoneInfo("America/New_York")
        self.now = datetime(2026, 11, 20, 12, tzinfo=self.zone)
        self.ticker = MagicMock()
        self.ticker.get_history_metadata.return_value = {
            "exchangeTimezoneName": "America/New_York", "currency": "USD",
            "currentTradingPeriod": {"regular": {"end": self.now.replace(hour=16)}},
        }
        self.ticker.get_earnings_dates.return_value = pd.DataFrame(
            {"Reported EPS": [1, 2, 3, 4, 5]}, index=pd.DatetimeIndex([
                "2027-01-01 16:00", "2026-11-18 16:00", "2026-05-01 16:00",
                "2025-02-04 16:00", "2024-12-01 16:00",
            ], tz=self.zone))
        self.ticker.history.return_value = pd.DataFrame(
            {"Open": [98, 99, 103, 108, 110, 111], "Close": [100, 101, 105, 110, 112, 113]}, index=pd.DatetimeIndex([
                "2026-04-17", "2026-05-01", "2026-05-15", "2026-11-18",
                "2026-11-19", "2026-11-20",
            ], tz=self.zone))

    def fetch(self, query="symbol=AAPL&year=2026"):
        with patch("earnings_api.yf.Ticker", return_value=self.ticker), patch("earnings_api.datetime") as clock:
            clock.now.return_value = self.now
            clock.fromtimestamp.side_effect = datetime.fromtimestamp
            return self.client.get("/earnings/history?" + query)

    def test_two_years_inclusive_windows_and_completed_daily_closes(self):
        response = self.fetch()
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body["calendar_dates"], ["2026-11-18", "2026-05-01", "2025-02-04"])
        recent, historical, empty = body["events"]
        self.assertFalse(recent["window_complete"])
        self.assertEqual([row["date"] for row in recent["prices"]], ["2026-11-19", "2026-11-18"])
        self.assertEqual(historical["window_start"], "2026-04-17")
        self.assertEqual(historical["window_end"], "2026-05-15")
        self.assertEqual([row["close"] for row in historical["prices"]], [105, 101, 100])
        self.assertEqual([row["open"] for row in historical["prices"]], [103, 99, 98])
        self.assertTrue(historical["window_complete"])
        self.assertFalse(empty["window_complete"])
        self.ticker.history.assert_called_once_with(start="2025-01-21", end="2026-11-21",
                                                   interval="1d", auto_adjust=False, actions=False, timeout=10, refresh=True)

    def test_exclusions_remove_windows_but_keep_calendar_and_narrow_history_request(self):
        body = self.fetch("symbol=AAPL&year=2026&exclude_dates=2026-05-01,2025-02-04").get_json()
        self.assertEqual(len(body["events"]), 1)
        self.assertEqual(len(body["calendar_dates"]), 3)
        self.assertEqual(self.ticker.history.call_args.kwargs["start"], "2026-11-04")

    def test_all_excluded_skips_daily_history_entirely(self):
        body = self.fetch("symbol=AAPL&year=2026&exclude_dates=2026-05-01,2025-02-04,2026-11-18").get_json()
        self.assertEqual(body["events"], [])
        self.ticker.history.assert_not_called()

    def test_today_is_included_only_after_confirmed_session_close(self):
        self.now = self.now.replace(hour=17)
        body = self.fetch().get_json()
        self.assertEqual(body["events"][0]["prices"][0]["date"], "2026-11-20")

    def test_invalid_close_does_not_permanently_complete_window(self):
        self.ticker.history.return_value.loc[pd.Timestamp("2026-05-01", tz=self.zone), "Close"] = float("nan")
        event = self.fetch().get_json()["events"][1]
        self.assertFalse(event["window_complete"])
        self.assertEqual(len(event["prices"]), 2)

    def test_invalid_open_does_not_permanently_complete_window(self):
        self.ticker.history.return_value.loc[pd.Timestamp("2026-05-01", tz=self.zone), "Open"] = float("nan")
        event = self.fetch().get_json()["events"][1]
        self.assertFalse(event["window_complete"])
        self.assertEqual(len(event["prices"]), 2)

    def test_unknown_calendar_returns_empty_success(self):
        self.ticker.get_earnings_dates.return_value = None
        body = self.fetch().get_json()
        self.assertEqual(body["events"], [])
        self.ticker.history.assert_not_called()

    def test_future_only_window_has_no_history_request(self):
        self.ticker.get_earnings_dates.return_value = pd.DataFrame(index=pd.DatetimeIndex(
            ["2026-12-30 16:00"], tz=self.zone), data={"Reported EPS": [float("nan")]})
        body = self.fetch().get_json()
        self.assertFalse(body["events"][0]["window_complete"])
        self.ticker.history.assert_not_called()

    def test_calendar_paginates_until_prior_year_is_covered(self):
        pages = [pd.DataFrame({"EPS": [1] * 25}, index=pd.date_range("2026-01-01", periods=25, tz=self.zone)),
                 pd.DataFrame({"EPS": [1]}, index=pd.DatetimeIndex(["2025-01-20"], tz=self.zone))]
        self.ticker.get_earnings_dates.side_effect = pages
        body = self.fetch().get_json()
        self.assertEqual(len(body["calendar_dates"]), 26)
        self.assertEqual(self.ticker.get_earnings_dates.call_args_list[1].kwargs, {"limit": 25, "offset": 25, "refresh": True})

    def test_issuer_symbol_does_not_change_price_symbol(self):
        issuer = MagicMock()
        issuer.get_earnings_dates.return_value = self.ticker.get_earnings_dates.return_value
        with patch("earnings_api.yf.Ticker", side_effect=[self.ticker, issuer]) as factory:
            response = self.client.get("/earnings/history?symbol=AAPL.TO&earnings_symbol=AAPL&year=2026")
        self.assertEqual(response.status_code, 200)
        self.assertEqual([call.args[0] for call in factory.call_args_list], ["AAPL.TO", "AAPL"])
        issuer.history.assert_not_called()
        self.ticker.history.assert_called_once()

    def test_bad_parameters_are_rejected_before_yahoo_calls(self):
        for query in ("", "symbol=bad/symbol", "symbol=AAPL&year=no", "symbol=AAPL&year=10000",
                      "symbol=AAPL&exclude_dates=2026-02-30", "symbol=AAPL&earnings_symbol=bad/symbol"):
            with patch("earnings_api.yf.Ticker") as factory:
                self.assertEqual(self.client.get("/earnings/history?" + query).status_code, 400)
                factory.assert_not_called()

    def test_provider_failures_and_rate_limits_are_errors(self):
        for error, status in ((YFRateLimitError(), 429), (RuntimeError("offline"), 502)):
            self.ticker.get_earnings_dates.side_effect = error
            self.assertEqual(self.fetch().status_code, status)
