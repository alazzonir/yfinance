"""Process-local Yahoo transport accounting, pacing, and 429 cooldowns.

Only the local Flask API enables this monitor. Never record query strings,
request headers, cookies, crumbs, bodies, or exception messages.
"""

from collections import Counter, deque
from contextvars import ContextVar
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import functools
import json
import logging
from logging.handlers import RotatingFileHandler
import math
import os
from pathlib import Path
from threading import Lock
import time
from urllib.parse import urlsplit

from .exceptions import YFRateLimitError


request_context = ContextVar("yahoo_request_context", default=None)


class RequestMonitor:
    def __init__(self, interval=1.0, cooldown=60.0, clock=time.monotonic, sleep=time.sleep):
        self.enabled = False
        self.interval, self.cooldown = interval, cooldown
        self.clock, self.sleep = clock, sleep
        self.lock = Lock()
        self.outbound_lock = Lock()
        self.last_started = None
        self.cooldown_until = 0
        self.totals = Counter(attempts=0, redirects=0, errors=0, rate_limits=0, blocked_during_cooldown=0)
        self.statuses = Counter()
        self.starts = deque()
        self.recent = deque(maxlen=50)
        self.logger = logging.getLogger("yfinance.request_monitor")
        self.started_at = datetime.now(timezone.utc).isoformat()

    def configure(self, *, log_path, interval=1.0, cooldown=60.0):
        if not (math.isfinite(interval) and 0 <= interval <= 60):
            raise ValueError("YF_MIN_REQUEST_INTERVAL must be between 0 and 60 seconds")
        if not (math.isfinite(cooldown) and 1 <= cooldown <= 86400):
            raise ValueError("YF_RATE_LIMIT_COOLDOWN must be between 1 and 86400 seconds")
        path = Path(log_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(path, maxBytes=5_000_000, backupCount=3)
        handler.setFormatter(logging.Formatter("%(message)s"))
        self.logger.handlers = [handler]
        self.logger.setLevel(logging.INFO)
        self.logger.propagate = False
        self.interval, self.cooldown, self.enabled = interval, cooldown, True

    def reserve(self):
        # The caller holds outbound_lock; release the statistics lock while pacing.
        with self.lock:
            now = self.clock()
            if now < self.cooldown_until:
                self.totals["blocked_during_cooldown"] += 1
                raise YFRateLimitError()
            delay = 0 if self.last_started is None else max(0, self.interval - (now - self.last_started))
        if delay:
            self.sleep(delay)
        with self.lock:
            self.last_started = self.clock()
            self.starts.append(self.last_started)
            self.totals["attempts"] += 1
            context = request_context.get()
            if context is not None:
                context["attempts"] += 1

    def record(self, method, url, *, response=None, error=None):
        status = getattr(response, "status_code", None)
        status = status if isinstance(status, int) else None
        # curl_cffi exposes redirect_count; requests exposes response.history.
        redirects = getattr(response, "redirect_count", None)
        if not isinstance(redirects, int):
            redirects = len(getattr(response, "history", []) or []) if response is not None else 0
        redirects = max(redirects, 0)
        context = request_context.get()
        with self.lock:
            self.totals["redirects"] += redirects
            if status is not None:
                self.statuses[str(status)] += 1
            if error is not None:
                self.totals["errors"] += 1
            if status == 429:
                self.totals["rate_limits"] += 1
                delay = self.retry_delay(getattr(response, "headers", {}).get("Retry-After"))
                self.cooldown_until = max(self.cooldown_until, self.clock() + delay)
            if context is not None:
                context["redirects"] += redirects
                context["rate_limits"] += int(status == 429)
        parsed = urlsplit(url)
        self.log(dict(type="yahoo_http", request_id=context["request_id"] if context else None,
                      method=method.upper(), host=parsed.hostname, path=parsed.path,
                      status=status, redirects=redirects, error=type(error).__name__ if error else None))

    def ensure_cooldown(self):
        # Library-detected throttling may arrive in a 200 response body. Repeated
        # locally blocked API requests must not keep extending an active cooldown.
        with self.lock:
            now = self.clock()
            if self.cooldown_until <= now:
                self.cooldown_until = now + self.cooldown

    def retry_delay(self, value):
        try:
            delay = float(value)
            if not math.isfinite(delay):
                return self.cooldown
        except (TypeError, ValueError):
            try:
                date = parsedate_to_datetime(value)
                delay = (date - datetime.now(timezone.utc)).total_seconds()
            except (TypeError, ValueError, OverflowError):
                return self.cooldown
        return max(self.cooldown, min(delay, 86400))

    def log(self, event):
        self.logger.info(json.dumps(dict(timestamp=datetime.now(timezone.utc).isoformat(), pid=os.getpid(), **event)))

    def finish_request(self, context, status):
        summary = dict(type="api_request", **context, status=status,
                       http_requests=context["attempts"] + context["redirects"])
        with self.lock:
            self.recent.append(summary)
        self.log(summary)
        return summary

    def snapshot(self):
        with self.lock:
            now = self.clock()
            while self.starts and self.starts[0] < now - 3600:
                self.starts.popleft()
            totals = dict(self.totals)
            totals["http_requests"] = totals.get("attempts", 0) + totals.get("redirects", 0)
            return dict(started_at=self.started_at, scope="current_process", totals=totals,
                        status_codes=dict(self.statuses), attempts_last_minute=sum(t >= now - 60 for t in self.starts),
                        attempts_last_hour=len(self.starts), min_request_interval_seconds=self.interval,
                        cooldown_remaining_seconds=math.ceil(max(0, self.cooldown_until - now)),
                        recent_requests=list(self.recent))


monitor = RequestMonitor()


def instrument_session(session):
    """Preserve the real session type and catch every GET/POST/retry through request()."""
    if getattr(session.request, "_yf_instrumented", False) is True:
        return session
    original = session.request

    @functools.wraps(original)
    def measured_request(method, url, *args, **kwargs):
        host = urlsplit(url).hostname or ""
        if not monitor.enabled or not (host == "yahoo.com" or host.endswith(".yahoo.com")):
            return original(method, url, *args, **kwargs)
        with monitor.outbound_lock:
            monitor.reserve()
            try:
                response = original(method, url, *args, **kwargs)
            except Exception as error:
                monitor.record(method, url, error=error)
                raise
            monitor.record(method, url, response=response)
            # Stop cookie-strategy retries after a Yahoo 429.
            if response.status_code == 429:
                raise YFRateLimitError()
            return response

    measured_request._yf_instrumented = True
    session.request = measured_request
    return session
