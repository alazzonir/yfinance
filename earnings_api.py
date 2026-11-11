"""Earnings calendar and completed daily opening/closing-price windows for the local API."""

from datetime import date, datetime, timedelta
import math
import re
from zoneinfo import ZoneInfo

from flask import jsonify, request
import yfinance as yf
from yfinance.exceptions import YFRateLimitError


SYMBOL_PATTERN = re.compile(r"[A-Z0-9^][A-Z0-9.^=\-]{0,31}")


def register_earnings_routes(app):
    @app.get("/earnings/history")
    def earnings_history():
        symbol = request.args.get("symbol", "").strip().upper()
        earnings_symbol = request.args.get("earnings_symbol", symbol).strip().upper()
        try:
            if not all(SYMBOL_PATTERN.fullmatch(value) for value in (symbol, earnings_symbol)):
                raise ValueError("Provide valid symbol and earnings_symbol parameters.")
            year = int(request.args.get("year", datetime.now(ZoneInfo("America/Toronto")).year))
            if not 1971 <= year <= 9998:
                raise ValueError("year must be between 1971 and 9998.")
            excluded = set()
            for value in filter(None, request.args.get("exclude_dates", "").split(",")):
                parsed = date.fromisoformat(value)
                if parsed.isoformat() != value:
                    raise ValueError("exclude_dates must contain ISO dates.")
                excluded.add(parsed)
            if len(excluded) > 100:
                raise ValueError("Provide at most 100 excluded dates.")
        except (ValueError, TypeError) as error:
            return jsonify(error=str(error)), 400

        try:
            ticker = yf.Ticker(symbol)
            issuer = ticker if symbol == earnings_symbol else yf.Ticker(earnings_symbol)
            metadata = ticker.get_history_metadata()
            timezone = metadata["exchangeTimezoneName"]
            currency = metadata["currency"]
            zone = ZoneInfo(timezone)
            now = datetime.now(zone)
            dates = earnings_calendar(issuer, year, zone)
            selected = {day: timestamp for day, timestamp in dates.items() if day not in excluded}
            prices = {}
            invalid_dates = set()
            if selected:
                start = min(selected) - timedelta(days=14)
                end = min(max(selected) + timedelta(days=15), now.date() + timedelta(days=1))
                if start < end:
                    history = ticker.history(start=start.isoformat(), end=end.isoformat(), interval="1d",
                                             auto_adjust=False, actions=False, timeout=10, refresh=True)
                    regular_end = metadata.get("currentTradingPeriod", {}).get("regular", {}).get("end")
                    if isinstance(regular_end, (int, float)):
                        regular_end = datetime.fromtimestamp(regular_end, zone)
                    today_closed = (regular_end is not None and regular_end.date() == now.date()
                                    and regular_end <= now)
                    for timestamp, row in history.iterrows():
                        day = timestamp.tz_convert(zone).date() if timestamp.tzinfo else timestamp.date()
                        if day > now.date() or (day == now.date() and not today_closed):
                            continue
                        opening, close = float(row["Open"]), float(row["Close"])
                        if not all(math.isfinite(value) and value > 0 for value in (opening, close)):
                            invalid_dates.add(day)
                            continue
                        prices[day] = {"open": opening, "close": close}
            events = []
            for day, timestamp in sorted(selected.items(), reverse=True):
                start, end = day - timedelta(days=14), day + timedelta(days=14)
                window = [{"date": price_date.isoformat(), **values}
                          for price_date, values in sorted(prices.items(), reverse=True)
                          if start <= price_date <= end]
                complete = (end < now.date() and bool(window)
                            and not any(start <= bad <= end for bad in invalid_dates))
                events.append(dict(earnings_date=day.isoformat(), earnings_at=timestamp.isoformat(),
                                   window_start=start.isoformat(), window_end=end.isoformat(),
                                   window_complete=complete, prices=window))
            return jsonify(symbol=symbol, earnings_symbol=earnings_symbol, year=year,
                           timezone=timezone, currency=currency,
                           calendar_dates=[day.isoformat() for day in sorted(dates, reverse=True)], events=events)
        except YFRateLimitError:
            return jsonify(error="Yahoo Finance rate limit reached. Try again later."), 429
        except Exception:
            app.logger.exception("Unable to fetch earnings history for %s", symbol)
            return jsonify(error="Unable to retrieve earnings history from Yahoo Finance."), 502


def earnings_calendar(ticker, year, zone):
    """Page until the requested years are covered, rather than silently truncating."""
    result = {}
    seen = set()
    for offset in range(0, 200, 25):
        frame = ticker.get_earnings_dates(limit=25, offset=offset, refresh=True)
        if frame is None or frame.empty:
            return result
        timestamps = list(frame.index)
        identities = {timestamp.isoformat() for timestamp in timestamps}
        if identities.issubset(seen):
            raise ValueError("Earnings pagination did not advance")
        seen.update(identities)
        for timestamp in timestamps:
            day = timestamp.tz_convert(zone).date() if timestamp.tzinfo else timestamp.date()
            if day.year in (year - 1, year):
                result[day] = timestamp.to_pydatetime()
        if len(frame) < 25 or min(timestamp.year for timestamp in timestamps) < year - 1:
            return result
    raise ValueError("Earnings calendar exceeds the supported page limit")
