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

## Earnings windows

```sh
curl 'http://localhost:5001/earnings/history?symbol=AAPL&year=2026&exclude_dates=2026-02-05,2026-05-07'
# An issuer calendar can be used with CDR prices:
curl 'http://localhost:5001/earnings/history?symbol=AAPL.TO&earnings_symbol=AAPL&year=2026'
```

`year` defaults to the Toronto calendar year and includes that year and the preceding year.
The endpoint pages `get_earnings_dates(limit=25, offset=...)` (up to 200 calendar entries), then
fetches one daily-history span for the requested events. It returns `symbol`, `earnings_symbol`,
`year`, price-market `timezone` and `currency`, the full matching `calendar_dates`, and `events`.
Each event contains `earnings_date`, timezone-aware `earnings_at`, `window_start`, `window_end`,
`window_complete`, and `prices` (`date`, unadjusted `open`, and unadjusted `close`). Windows span 14 **calendar** days
on each side, inclusive. Earnings are provider-reported calendar entries, including upcoming dates;
the endpoint does not independently verify that a call occurred.

`exclude_dates` is a comma-separated list of up to 100 ISO dates. Excluded events remain in
`calendar_dates` but have no event/price payload. The calendar is still queried to discover new dates.
The response is not cached in Flask; the Rails app persists individual windows and supplies exclusions.
Errors return 400 (parameters), 429 (rate limiting), or 502 (upstream failure).

Only completed sessions are returned; today's daily bar is included only when metadata confirms
that today's regular session has closed. Weekends/holidays have no artificial prices. A window is
complete after its end date has passed, at least one close was returned, and no returned bar had an
invalid opening or closing price. Missing sessions absent from Yahoo's response cannot be independently distinguished
from market holidays; this is not an exchange-calendar completeness guarantee. Upcoming, empty,
or invalid-price windows remain incomplete and can be requested again. A forced Rails refresh
omits exclusions so corrected historical prices can be fetched.

Run endpoint tests with `uv run --extra api python -m unittest discover -s tests -p 'test_*api.py'`.

This fork supports `refresh=True` on `Ticker.get_earnings_dates` and `Ticker.history`
to bypass their in-memory/calendar and historical HTTP caches for that request.
The earnings endpoint uses it for all requested windows and calendar pages; existing
quote endpoints and other library callers retain their default caching behavior.

## Yahoo request monitoring and pacing

```sh
curl -s 'http://localhost:5001/monitoring/yahoo' | python -m json.tool
# Inspect durable JSONL records (from the yfinance repository):
tail -f logs/yahoo_requests.log
```

Monitoring covers **all local API endpoints** at the supported session's HTTP transport:
market data, cookie/crumb/consent calls, and each yfinance retry. The read-only monitoring
endpoint makes no Yahoo requests. Responses include `X-Yahoo-Requests`, `X-Yahoo-Attempts`,
`X-Yahoo-Redirects`, `X-Yahoo-Rate-Limits`, and `X-Yahoo-Request-Id`. A cached API response can
therefore report zero outbound requests. Rails persists the count from each successful earnings
response and shows it in the earnings modal, including after reopening it.

`attempts` counts calls sent to the HTTP backend, including failed attempts; `redirects` counts
additional hops reported by the backend. `http_requests` is their sum. A failed transport may not
have reached Yahoo, and redirects before a transport exception may be unobservable; this is
client-side accounting, not a server-confirmed Yahoo usage quota. Status counts describe final
responses, not each redirect status. Automatic redirects are counted after completion and cannot
be paced individually. Cached yfinance responses do not reach this layer and count as zero.

The monitor serializes Yahoo transport attempts and spaces their starts by one second by default.
After any observed HTTP 429 (or library-detected throttling in a response body) it blocks new outbound calls for at least 60 seconds, honoring a longer
`Retry-After` (seconds or HTTP date, capped at one day). Blocked calls return a local 429 without
contacting Yahoo; that response includes `Retry-After`. It does not automatically hammer Yahoo
with cooldown retries. Normal cookies and request behavior are preserved while monitoring is disabled
for library callers outside the local Flask API.

Configure the Flask process with:

- `YF_MIN_REQUEST_INTERVAL=1`: minimum seconds between outbound attempt starts (0–60).
- `YF_RATE_LIMIT_COOLDOWN=60`: minimum cooldown after an observed Yahoo 429 (1–86400 seconds).
- `YF_REQUEST_LOG_PATH=/path/yahoo_requests.log`: defaults to `logs/yahoo_requests.log` beside `local_api.py`.

Logs rotate at 5 MB with three backups. They contain sanitized host/path, method, status,
redirect count, exception type, and per-API request totals/IDs. Query strings, cookies, crumbs,
headers, request bodies, and exception messages are omitted. Counters and cooldowns are per process
and reset on restart/hot reload; logs remain on disk. Use one API process to share pacing across
all callers. `/monitoring/yahoo` exposes process totals, attempts over the last minute/hour,
current cooldown, status counts and the 50 most recent local API requests. Historical counts from
before monitoring was installed cannot be recovered. This helps control request volume but does
not guarantee that Yahoo will never rate-limit requests.

Tests:

```sh
uv run --extra api python -m unittest discover -s tests -p test_request_monitor.py
```
