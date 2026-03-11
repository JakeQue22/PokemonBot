# PokemonBot – Change Log & Progress

## Working

- **Smyths Toys monitoring** – fully functional with `fetch()` (curl_cffi/aiohttp). Uses proxies when available. Does NOT use browser. Does NOT fall back to direct connection.
- **Pokemon Center monitoring** – uses Playwright browser with captcha/challenge handling. ALWAYS routes through proxy pool; direct connection is never used.
- **Sticky proxy** – when a proxy succeeds, it is reused for subsequent requests until it fails, then the next available proxy is tried.
- **Stock status reporting** – successful checks now report the actual stock status (e.g. "Out of stock", "In stock") instead of generic "no change"
- **Log colouring** – successful checks (`OK (HTTP 200)`) display in green in the web UI

## ⚠️ Proxy Policy

- **ALL sites**: ALWAYS use proxy. `direct_fallback` is **always** `False`. Real IP is **never** exposed.
- Direct connections (no proxy) are **never** attempted, for **any** site.
- If all proxies fail, the monitor reports a connection error and retries on the next cycle.

## Changes Made

### Sticky proxy (`proxy.py`)
- Added `mark_success(proxy)` method to `ProxyPool` – records a proxy as the preferred (sticky) proxy
- `next_available()` now checks the preferred proxy **first** before falling back to round-robin
- When a preferred proxy is marked as failed via `mark_failed()`, the preference is cleared and round-robin resumes
- Fixed `next_available()` cooldown check: proxies that were **never** failed are no longer treated as if they failed at time 0

### Sticky proxy in fetch (`session.py`)
- `fetch()` calls `proxy_pool.mark_success(proxy)` after a successful proxied response
- `fetch_with_browser()` calls `proxy_pool.mark_success(proxy)` after a successful proxied response
- This means a working proxy is automatically reused until it fails

### No direct fallback for ANY site (`tasks.py`)
- Changed `_check_once()` to set `direct_fallback = False` unconditionally for ALL sites
- Previously only `pokemoncenter` disabled direct fallback; now `smythstoys` and `generic` sites also never use direct connections
- `_PROXY_REQUIRED_SITES` is kept for reference but the policy is now: **ALL traffic must go through a proxy**

### Per-site proxy policy (`tasks.py`)
- `_BROWSER_SITES = frozenset({"pokemoncenter"})` – only pokemoncenter uses Playwright browser
- `_PROXY_REQUIRED_SITES = frozenset({"pokemoncenter"})` – historical; all sites now enforce proxy-only
- `_SITE_RETRY_STATUSES = {"pokemoncenter": frozenset({403})}` – 403 from pokemoncenter triggers proxy rotation

### Stock status reporting (`monitor.py`, `tasks.py`)
- `describe_status()` method on `BaseMonitor` and all subclasses
- Returns human-readable status: "Out of stock", "In stock", "Queue active", "Access denied (bot protection)", etc.
- `_check_once()` logs the stock status: `Monitor [name] check #N OK (HTTP 200) – Out of stock`

### Browser-based fetch with proxy support (`session.py`)
- `fetch_with_browser()` accepts `proxy_pool`, `proxy_timeout`, `max_retries`, and `direct_fallback` parameters
- Each browser attempt is routed through a proxy from the pool (Playwright context-level proxy)
- Proxy failures (403, connection errors) mark the proxy as failed and retry with the next proxy
- `direct_fallback=False` means no direct connection is ever attempted

### HTTP fetch with proxy support (`session.py`)
- `fetch()` uses curl_cffi (Chrome TLS fingerprint) and aiohttp backends, alternating on each retry
- Smart retry budgets: timeout errors capped at `max_retries`; fast failures (SOCKS, TLS) get a separate larger budget
- `retry_on_status` parameter treats specific HTTP status codes (e.g. 403) as proxy failures
- `direct_fallback=False` means no direct connection is ever attempted

### Captcha / challenge handling (`session.py`)
- After page loads, waits 3 seconds for challenge pages to render their interactive elements
- Scans for common challenge selectors: Akamai Bot Manager, PerimeterX, Cloudflare Turnstile
- Clicks the challenge checkbox/button if found, then waits 3 more seconds for resolution

### Log colouring (`web.py`)
- `logClass()` JavaScript function matches `OK` and `check #N OK` patterns and applies the `log-success` CSS class (green `#2ecc71`)

## Parsing Methods

### Pokemon Center (`PokemonCenterMonitor`)
- Parses HTML for product cards matching keywords
- Checks `add-to-cart` button presence for in-stock status
- Detects queue pages (waiting room) via `queue-it` markers
- Uses Playwright browser to execute JavaScript (Akamai Bot Manager)

### Smyths Toys (`SmythsToysMonitor`)
- **Product pages** (`/p/`): parses `availability` span, `Add to Cart` button
- **Category pages** (`/c/`): parses product listing cards
- **Store stock JSON**: parses API responses for store-level availability
- Uses HTTP fetch (curl_cffi/aiohttp) – does NOT need browser

### Generic (`GenericMonitor`)
- Keyword matching in page body
- Checks for common "Add to Cart" / "Buy Now" button patterns
- Uses HTTP fetch (curl_cffi/aiohttp)

## Architecture

```
TaskManager._check_once()
  ├── pokemoncenter → fetch_with_browser(proxy_pool=..., direct_fallback=False)
  │     ├── Get preferred (sticky) proxy, or next available from pool
  │     ├── Create Playwright context with proxy
  │     ├── Navigate + wait 3s for challenge
  │     ├── Click captcha if found + wait 3s
  │     ├── Extract page HTML
  │     ├── Success? → mark_success(proxy) → sticky for next request
  │     ├── 403? → mark_failed(proxy), retry next proxy
  │     └── All failed? → raise ConnectionError (NO direct fallback)
  └── other sites → fetch(proxy_pool=..., direct_fallback=False)
        ├── curl_cffi with Chrome TLS fingerprint
        ├── aiohttp fallback
        ├── Proxy rotation with smart retry budgets
        ├── Success? → mark_success(proxy) → sticky for next request
        └── All failed? → raise ConnectionError (NO direct fallback)
```

## Previous Issues (Resolved)

1. **curl_cffi getting 403 from Pokemon Center** – Akamai Bot Manager requires JavaScript; curl_cffi cannot handle this. Resolved by adding Playwright browser-based fetch.
2. **Browser not using proxies** – `fetch_with_browser()` originally had no proxy support. Now routes through the proxy pool.
3. **Direct connection fallback exposing real IP** – `direct_fallback=False` is now set for ALL sites. Real IP is never exposed.
4. **Proxies not sticky** – Added `mark_success()` to `ProxyPool`. Working proxies are reused until they fail.
5. **Smyths going through browser unnecessarily** – `_BROWSER_SITES` only includes `pokemoncenter`. Smyths uses HTTP fetch.
6. **Logs showing "no change" instead of stock status** – Fixed by adding `describe_status()` method to monitors.
