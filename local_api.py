"""Local HTTP access to today's hourly prices from this yfinance checkout."""

from collections import OrderedDict
from datetime import datetime, timedelta
import math
from numbers import Real
import re
from threading import Lock
from time import monotonic
from zoneinfo import ZoneInfo

from flask import Flask, jsonify, request
import yfinance as yf
from yfinance.exceptions import YFRateLimitError
from earnings_api import register_earnings_routes
from monitoring_api import register_monitoring


app = Flask(__name__)
register_monitoring(app)
# Surface upstream failures instead of turning them into empty history.
yf.config.debug.hide_exceptions = False

# Process-local, bounded cache. Restarting the server clears it.
CACHE_TTL_SECONDS = 600
CACHE_MAX_SYMBOLS = 128
price_cache = OrderedDict()
cache_lock = Lock()
profile_cache = OrderedDict()
profile_lock = Lock()
currency_cache = {}
currency_lock = Lock()
CURRENCY_TIMEZONE = ZoneInfo("America/Toronto")


@app.get("/currency/rate")
def currency_rate():
    try:
        with currency_lock:
            today = datetime.now(CURRENCY_TIMEZONE).date().isoformat()
            if currency_cache.get("cached_date") != today:
                history = yf.Ticker("USDCAD=X").history(
                    period="5d", interval="1d", auto_adjust=False,
                    actions=False, timeout=10,
                )
                closes = history["Close"].dropna()
                if closes.empty:
                    raise ValueError("Missing USD/CAD rate")
                rate = float(closes.iloc[-1])
                if not math.isfinite(rate) or rate <= 0:
                    raise ValueError("Invalid USD/CAD rate")
                payload = dict(
                    base="USD", quote="CAD", rate=rate,
                    rate_date=closes.index[-1].date().isoformat(),
                    cached_date=today, cache_timezone="America/Toronto",
                )
                currency_cache.update(payload)
            payload = dict(currency_cache)
        return jsonify(payload)
    except YFRateLimitError:
        return jsonify(error="Yahoo Finance rate limit reached. Try again later."), 429
    except Exception:
        app.logger.exception("Unable to fetch USD/CAD rate")
        return jsonify(error="Unable to retrieve USD/CAD rate from Yahoo Finance."), 502


@app.get("/history/today")
def today_history():
    symbol = request.args.get("symbol", "").strip().upper()
    if not re.fullmatch(r"[A-Z0-9^][A-Z0-9.^=\-]{0,31}", symbol):
        return jsonify(error="Provide a valid symbol, for example AAPL or SHOP.TO."), 400
    invalidate_cache = request.args.get("invalidate_cache", "false").lower()
    if invalidate_cache not in ("true", "false"):
        return jsonify(error="invalidate_cache must be true or false."), 400

    with cache_lock:
        if invalidate_cache == "true":
            price_cache.pop(symbol, None)
        cached = price_cache.get(symbol)
        if cached is not None:
            expires_at, payload = cached
            today = datetime.now(ZoneInfo(payload["timezone"])).date().isoformat()
            if monotonic() < expires_at and payload["date"] == today:
                price_cache.move_to_end(symbol)
                return jsonify(payload)
            del price_cache[symbol]

    try:
        ticker = yf.Ticker(symbol)
        # A short lookback preserves the latest quote when today's session is closed.
        history = ticker.history(
            period="5d", interval="1h", prepost=False,
            auto_adjust=False, actions=False, timeout=10,
        )
        metadata = ticker.get_history_metadata()
        timezone = metadata["exchangeTimezoneName"]
        zone = ZoneInfo(timezone)
        now = datetime.now(zone)
        regular = metadata.get("currentTradingPeriod", {}).get("regular", {})
        session_end = regular.get("end")
        prices = []
        for timestamp, row in history.iterrows():
            timestamp = timestamp.to_pydatetime().astimezone(zone)
            if timestamp.date() != now.date():
                continue
            bar_end = timestamp + timedelta(hours=1)
            # The last session bar can be shorter than an hour.
            if session_end is not None and session_end.date() == timestamp.date():
                bar_end = min(bar_end, session_end)
            if bar_end <= now and math.isfinite(float(row["Close"])):
                prices.append({"timestamp": timestamp.isoformat(), "close": float(row["Close"])})

        latest = None
        quote_price = metadata.get("regularMarketPrice")
        quote_time = metadata.get("regularMarketTime")
        if quote_price is not None and quote_time is not None and math.isfinite(float(quote_price)):
            if isinstance(quote_time, Real):
                quote_time = datetime.fromtimestamp(quote_time, zone)
            latest = {
                "price": float(quote_price),
                "timestamp": quote_time.astimezone(zone).isoformat(),
            }
        profile_date = now.date().isoformat()
        with profile_lock:
            cached_profile = profile_cache.get(symbol)
            profile = None
            if cached_profile is not None and cached_profile[0] == profile_date:
                profile = cached_profile[1]
                profile_cache.move_to_end(symbol)
        if profile is None:
            profile = {"sector": None, "industry": None}
            try:
                info = ticker.info
                profile = {
                    key: info.get(key) if isinstance(info.get(key), str) else None
                    for key in ("sector", "industry")
                }
                with profile_lock:
                    profile_cache[symbol] = (profile_date, profile)
                    profile_cache.move_to_end(symbol)
                    while len(profile_cache) > CACHE_MAX_SYMBOLS:
                        profile_cache.popitem(last=False)
            except Exception:
                app.logger.warning("Unable to fetch company details for %s", symbol, exc_info=True)
        payload = dict(
            symbol=symbol, date=now.date().isoformat(), timezone=timezone,
            currency=metadata.get("currency"), interval="1h",
            latest=latest, history=prices, **profile,
        )
        with cache_lock:
            price_cache[symbol] = (monotonic() + CACHE_TTL_SECONDS, payload)
            price_cache.move_to_end(symbol)
            while len(price_cache) > CACHE_MAX_SYMBOLS:
                price_cache.popitem(last=False)
        return jsonify(payload)
    except YFRateLimitError:
        return jsonify(error="Yahoo Finance rate limit reached. Try again later."), 429
    except Exception:
        app.logger.exception("Unable to fetch history for %s", symbol)
        return jsonify(error="Unable to retrieve market data for this symbol from Yahoo Finance."), 502


# Keep the earnings endpoint separate from quote and currency request handling.
register_earnings_routes(app)
