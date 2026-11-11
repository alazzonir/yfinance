"""Read-only local monitoring and per-request Yahoo usage headers."""

import os
from pathlib import Path
from uuid import uuid4

from flask import g, jsonify, request

from yfinance.request_monitor import monitor, request_context


def register_monitoring(app):
    monitor.configure(log_path=os.environ.get("YF_REQUEST_LOG_PATH", Path(__file__).parent / "logs/yahoo_requests.log"),
                      interval=float(os.environ.get("YF_MIN_REQUEST_INTERVAL", "1")),
                      cooldown=float(os.environ.get("YF_RATE_LIMIT_COOLDOWN", "60")))

    @app.before_request
    def begin_usage():
        if request.path == "/monitoring/yahoo":
            return
        g.yahoo_usage = dict(request_id=uuid4().hex, endpoint=request.path,
                             symbol=request.args.get("symbol", "")[:32], attempts=0, redirects=0, rate_limits=0)
        g.yahoo_token = request_context.set(g.yahoo_usage)

    @app.after_request
    def report_usage(response):
        if hasattr(g, "yahoo_usage"):
            summary = monitor.finish_request(g.yahoo_usage, response.status_code)
            response.headers["X-Yahoo-Requests"] = str(summary["http_requests"])
            response.headers["X-Yahoo-Attempts"] = str(summary["attempts"])
            response.headers["X-Yahoo-Redirects"] = str(summary["redirects"])
            response.headers["X-Yahoo-Rate-Limits"] = str(summary["rate_limits"])
            response.headers["X-Yahoo-Request-Id"] = summary["request_id"]
            if response.status_code == 429:
                monitor.ensure_cooldown()
            remaining = monitor.snapshot()["cooldown_remaining_seconds"]
            if response.status_code == 429 and remaining:
                response.headers["Retry-After"] = str(remaining)
        return response

    @app.teardown_request
    def end_usage(error):
        token = g.pop("yahoo_token", None)
        if token is not None:
            request_context.reset(token)

    @app.get("/monitoring/yahoo")
    def yahoo_usage():
        return jsonify(monitor.snapshot())
