"""Deterministic transport accounting, attribution and cooldown checks."""

from concurrent.futures import ThreadPoolExecutor
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from flask import Flask, jsonify

from monitoring_api import register_monitoring
from yfinance.exceptions import YFRateLimitError
from yfinance.request_monitor import RequestMonitor, instrument_session


class RequestMonitorTests(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        self.monitor = RequestMonitor(clock=lambda: self.now, sleep=self.advance)
        self.monitor.enabled = True
        self.monitor.logger = Mock()
        self.patcher = patch("yfinance.request_monitor.monitor", self.monitor)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def advance(self, seconds):
        self.now += seconds

    def session(self, responses=None):
        self.transport = Mock(side_effect=responses) if responses else Mock(return_value=self.response())
        return instrument_session(SimpleNamespace(request=self.transport))

    def response(self, status=200, redirects=0, headers=None):
        return SimpleNamespace(status_code=status, redirect_count=redirects, headers=headers or {})

    def test_counts_retries_redirects_and_authentication_calls_without_recording_secrets(self):
        session = self.session([OSError("secret timeout"), self.response(redirects=2), self.response()])
        with self.assertRaises(OSError):
            session.request("GET", "https://query1.finance.yahoo.com/v8/finance/chart/AAPL?crumb=secret")
        session.request("GET", "https://query1.finance.yahoo.com/v8/finance/chart/AAPL?crumb=secret")
        session.request("POST", "https://consent.yahoo.com/collect?sessionId=secret", data={"cookie": "secret"})
        totals = self.monitor.snapshot()["totals"]
        self.assertEqual(totals["attempts"], 3)
        self.assertEqual(totals["redirects"], 2)
        self.assertEqual(totals["http_requests"], 5)
        self.assertEqual(totals["errors"], 1)
        self.assertEqual(self.now, 102)
        self.assertNotIn("secret", str(self.monitor.logger.info.call_args_list))

    def test_cooldown_stops_outbound_calls_and_honors_retry_after(self):
        session = self.session([self.response(429, headers={"Retry-After": "120"}), self.response()])
        with self.assertRaises(YFRateLimitError):
            session.request("GET", "https://query1.finance.yahoo.com/quote")
        with self.assertRaises(YFRateLimitError):
            session.request("GET", "https://query2.finance.yahoo.com/quote")
        self.assertEqual(self.transport.call_count, 1)
        self.assertEqual(self.monitor.snapshot()["cooldown_remaining_seconds"], 120)
        self.advance(120)
        session.request("GET", "https://query2.finance.yahoo.com/quote")
        self.assertEqual(self.monitor.snapshot()["totals"]["rate_limits"], 1)
        self.assertEqual(self.monitor.snapshot()["totals"]["blocked_during_cooldown"], 1)

    def test_other_hosts_and_disabled_monitor_pass_through_and_double_wrapping_does_not_double_count(self):
        session = self.session()
        instrument_session(session)
        session.request("GET", "http://localhost:5001/test")
        session.request("GET", "https://notyahoo.com/test")
        self.assertEqual(self.monitor.snapshot()["totals"]["attempts"], 0)
        session.request("GET", "https://finance.yahoo.com/test")
        self.assertEqual(self.monitor.snapshot()["totals"]["attempts"], 1)
        self.monitor.enabled = False
        session.request("GET", "https://finance.yahoo.com/test")
        self.assertEqual(self.monitor.snapshot()["totals"]["attempts"], 1)

    def test_rolling_counts_expire_and_request_sessions_are_paced_across_threads(self):
        session = self.session()
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda _: session.request("GET", "https://finance.yahoo.com/test"), range(4)))
        self.assertEqual(self.now, 103)
        self.assertEqual(self.monitor.snapshot()["attempts_last_minute"], 4)
        self.advance(61)
        self.assertEqual(self.monitor.snapshot()["attempts_last_minute"], 0)
        self.assertEqual(self.monitor.snapshot()["attempts_last_hour"], 4)
        self.advance(3600)
        self.assertEqual(self.monitor.snapshot()["attempts_last_hour"], 0)

    def test_headers_and_logs_attribute_requests_and_monitoring_does_not_generate_yahoo_traffic(self):
        app = Flask("monitor-test")
        with patch("monitoring_api.monitor", self.monitor), patch.object(self.monitor, "configure"):
            register_monitoring(app)
        session = self.session()

        @app.get("/test")
        def test_endpoint():
            session.request("GET", "https://query1.finance.yahoo.com/test?crumb=private")
            session.request("GET", "https://query1.finance.yahoo.com/test")
            return jsonify(ok=True)

        @app.get("/cached")
        def cached():
            return jsonify(cached=True)

        with patch("monitoring_api.monitor", self.monitor):
            client = app.test_client()
            response = client.get("/test?symbol=AAPL")
            self.assertEqual(response.headers["X-Yahoo-Requests"], "2")
            self.assertTrue(response.headers["X-Yahoo-Request-Id"])
            self.assertEqual(client.get("/cached").headers["X-Yahoo-Requests"], "0")
            stats = client.get("/monitoring/yahoo").get_json()
            self.assertEqual(stats["totals"]["attempts"], 2)
            self.assertEqual(len(stats["recent_requests"]), 2)
            self.assertEqual(stats["recent_requests"][0]["symbol"], "AAPL")
            log = json.loads(self.monitor.logger.info.call_args_list[-1].args[0])
            self.assertEqual(log["type"], "api_request")
            self.assertNotIn("private", str(self.monitor.logger.info.call_args_list))

    def test_api_rate_limit_headers_distinguish_yahoo_responses_from_local_cooldown(self):
        app = Flask("cooldown-test")
        with patch("monitoring_api.monitor", self.monitor), patch.object(self.monitor, "configure"):
            register_monitoring(app)
        session = self.session([self.response(429, headers={"Retry-After": "90"})])

        @app.get("/quote")
        def quote():
            try:
                session.request("GET", "https://query1.finance.yahoo.com/test")
                return jsonify(ok=True)
            except YFRateLimitError:
                return jsonify(error="rate limited"), 429

        with patch("monitoring_api.monitor", self.monitor):
            client = app.test_client()
            first = client.get("/quote")
            self.assertEqual(first.status_code, 429)
            self.assertEqual(first.headers["X-Yahoo-Requests"], "1")
            self.assertEqual(first.headers["X-Yahoo-Rate-Limits"], "1")
            self.assertEqual(first.headers["Retry-After"], "90")
            second = client.get("/quote")
            self.assertEqual(second.status_code, 429)
            self.assertEqual(second.headers["X-Yahoo-Requests"], "0")
            self.assertEqual(second.headers["X-Yahoo-Rate-Limits"], "0")
            self.assertEqual(second.headers["Retry-After"], "90")
            self.assertEqual(self.transport.call_count, 1)

    def test_library_detected_throttling_starts_cooldown_without_extending_it_on_blocked_requests(self):
        app = Flask("body-throttle-test")
        with patch("monitoring_api.monitor", self.monitor), patch.object(self.monitor, "configure"):
            register_monitoring(app)

        @app.get("/limited")
        def limited():
            return jsonify(error="library detected throttling"), 429

        with patch("monitoring_api.monitor", self.monitor):
            client = app.test_client()
            first = client.get("/limited")
            self.assertEqual(first.headers["Retry-After"], "60")
            self.advance(10)
            second = client.get("/limited")
            self.assertEqual(second.headers["Retry-After"], "50")
            self.assertEqual(self.monitor.snapshot()["totals"]["attempts"], 0)

    def test_supported_session_captures_cookie_and_transient_retry_requests(self):
        # Exercise YfData's real retry loop and supported requests.Session wiring.
        import requests
        from yfinance.data import YfData
        session = requests.Session()
        data = YfData()
        old_session, old_crumb = data._session, data._crumb
        try:
            with patch.object(session, "request", side_effect=[self.response(), OSError("network"), self.response()]):
                data._set_session(session)
                def authentication(*args, **kwargs):
                    session.get("https://fc.yahoo.com")
                    return None, "basic"
                with patch.object(data, "_get_cookie_and_crumb", side_effect=authentication), \
                     patch("yfinance.data._time.sleep"), \
                     patch("yfinance.data.YfConfig") as config:
                    config.network.proxy = None
                    config.network.retries = 1
                    result = data._make_request("https://query1.finance.yahoo.com/test", request_method=session.get)
                self.assertEqual(result.status_code, 200)
                self.assertEqual(self.monitor.snapshot()["totals"]["attempts"], 3)
        finally:
            data._session, data._crumb = old_session, old_crumb
            session.close()
