"""Cache bypass regressions for the local fork; no network requests."""

import unittest
from unittest.mock import MagicMock, patch

import pandas as pd
import yfinance as yf
from yfinance.scrapers.history import PriceHistory


class EarningsRefreshTests(unittest.TestCase):
    def test_calendar_cache_remains_default_but_refresh_replaces_the_value(self):
        ticker = yf.Ticker("AAPL")
        cached = pd.DataFrame({"EPS": [1]})
        refreshed = pd.DataFrame({"EPS": [2]})
        ticker._earnings_dates[(25, 0)] = cached
        with patch.object(ticker, "_get_earnings_dates_using_scrape", return_value=refreshed) as scrape:
            self.assertIs(ticker.get_earnings_dates(limit=25), cached)
            scrape.assert_not_called()
            self.assertIs(ticker.get_earnings_dates(limit=25, refresh=True), refreshed)
            scrape.assert_called_once_with(25, 0, refresh=True)
            self.assertIs(ticker.get_earnings_dates(limit=25), refreshed)

    def test_fresh_calendar_bypasses_the_http_cache_without_clearing_other_keys(self):
        ticker = yf.Ticker("AAPL")
        response = MagicMock(text="<html></html>")
        with patch.object(ticker._data, "get", return_value=response) as direct, \
             patch.object(ticker._data, "cache_get", return_value=response) as cached:
            ticker._get_earnings_dates_using_scrape(limit=25, refresh=True)
            direct.assert_called_once()
            cached.assert_not_called()
            ticker._get_earnings_dates_using_scrape(limit=25)
            cached.assert_called_once()

    def test_history_refresh_uses_direct_transport_but_default_uses_the_historical_cache(self):
        for refresh, expected in ((False, "cache_get"), (True, "get")):
            data = MagicMock()
            # Stop at the selected transport; no Yahoo payload/parser behavior is under test.
            data.get.side_effect = RuntimeError("transport reached")
            data.cache_get.side_effect = RuntimeError("transport reached")
            history = PriceHistory(data, "AAPL", "America/New_York")
            original = yf.config.debug.hide_exceptions
            try:
                yf.config.debug.hide_exceptions = False
                with self.assertRaisesRegex(RuntimeError, "transport reached"):
                    history.history(start="2020-05-01", end="2020-05-15", refresh=refresh)
            finally:
                yf.config.debug.hide_exceptions = original
            getattr(data, expected).assert_called_once()
            getattr(data, "get" if expected == "cache_get" else "cache_get").assert_not_called()
