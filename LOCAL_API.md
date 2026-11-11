# Local price API

From this checkout, install Python 3.12 and dependencies using uv:

```sh
uv sync --extra api
uv run --extra api flask --app local_api --debug run --host 127.0.0.1 --port 5001
```

uv creates `.venv` and installs this checkout in editable mode. No pip commands
or environment activation are needed. `.python-version` pins Python and
`uv.lock` records resolved packages.

`--debug` enables development mode and automatically restarts Flask when Python
files change. Reloading clears both in-memory caches. Omit `--debug` to run
without automatic reloads or the interactive debugger.

In another terminal:

```sh
curl 'http://localhost:5001/history/today?symbol=AAPL'
```

The JSON includes `symbol`, `date`, `timezone`, `currency`, `interval` (`1h`),
`sector`, `industry`, `latest` (`price` and `timestamp`), and `history` (a list of `timestamp` and
`close` values). Timestamps include the exchange's UTC offset.

History contains only completed hourly bars for today's date in the exchange's
timezone, during regular trading hours. Bar timestamps mark the interval's
start; `close` is its last price, not its average. Session bars may start at
09:30, and the final bar may be shorter than an hour. The current unfinished
bar is excluded. A closed day or pre-market request returns an empty history.

`latest` uses Yahoo's regular-market quote and actual quote timestamp, which
can belong to an earlier trading day. It is null if quote data is unavailable.
The quote is not guaranteed to be real-time and excludes extended-hours prices.
On a cache miss, the API fetches a five-day hourly window to support closed-day
requests, then filters locally. Successful responses are cached per normalized
symbol for 10 minutes, including the latest quote. A new exchange-local date
invalidates the cached response immediately. Errors are not cached.

To discard a symbol's cached value and fetch it again, pass `invalidate_cache=true`:

```sh
curl 'http://localhost:5001/history/today?symbol=AAPL&invalidate_cache=true'
```

The option defaults to `false`. Only `true` and `false` are accepted (case
insensitive); other values return HTTP 400. A successful refresh starts a new
10-minute cache lifetime. If the refresh fails, the old value remains discarded.
Other symbols' cached values are unaffected.

Sector and industry come from `Ticker.info`, which needs additional Yahoo
requests. Company details are cached independently per symbol until midnight in
the exchange's timezone (up to 128 symbols). Invalidating the price cache keeps
this daily company cache. Missing fields return JSON null. If company details
fail to load, prices still return with null fields; the lookup can retry on the
next price refresh rather than caching the failure for a day. The normal
10-minute response cache still applies. Restarting or hot reloading clears both
caches.

The in-memory cache holds up to 128 symbols, evicting the least recently used
entry when full. Restarting the server clears it; separate server processes have
separate caches. Concurrent requests on a cache miss may each fetch upstream.
There is no database or background polling.

Missing or malformed symbols return JSON with HTTP 400; Yahoo rate limits return
429. Other upstream failures, including unrecognized Yahoo symbols, return 502.

Run deterministic endpoint tests:

```sh
uv run --extra api python -m unittest discover -s tests -p test_local_api.py
```

This Flask development server is intended for local use and binds to loopback.

## USD to CAD exchange rate

```sh
curl 'http://localhost:5001/currency/rate'
```

No parameters are needed. The response contains `base` (`USD`), `quote` (`CAD`),
`rate`, `rate_date`, `cached_date`, and `cache_timezone` (`America/Toronto`).
Multiply a USD amount by `rate` to get CAD. The API returns the rate only and
no longer exposes the previous `/currency/convert` endpoint.

The rate comes from yfinance's `USDCAD=X` ticker, using the latest available
unadjusted daily Close in a five-day window. The latest daily bar may still be
forming. `rate_date` is the date of that bar, which may precede today on weekends
or holidays. It is not a guaranteed live or executable exchange quote.

The first request caches the rate until midnight Toronto time. The next request
on a new day fetches it again. This cache is independent of the stock cache and
has no manual invalidation option. It is in memory: restarting the server clears
it and each process has its own cache. Rate limits return HTTP 429; other upstream
failures return HTTP 502. Failures are not cached.

Run both endpoint suites:

```sh
uv run --extra api python -m unittest discover -s tests -p 'test_*api.py'
```
