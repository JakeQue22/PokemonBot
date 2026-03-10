# PokemonBot – Change Log & Progress

## Working

- **Smyths Toys monitoring** – fully functional with Playwright browser + proxy rotation
- **Pokemon Center monitoring** – now uses Playwright browser with captcha/challenge handling through proxies
- **Log colouring** – successful checks (`OK (HTTP 200)`) display in green in the web UI
- **Proxy enforcement** – all traffic routed through proxies; `direct_fallback` defaults to `False`

## Changes Made

### Browser-based fetch with proxy support (`session.py`)
- `fetch_with_browser()` now accepts `proxy_pool`, `proxy_timeout`, and `max_retries` parameters
- Each browser attempt is routed through a proxy from the pool (Playwright context-level proxy)
- Proxy failures (403, connection errors) mark the proxy as failed and retry with the next proxy
- Added `_browser_fetch_once()` helper that creates a Playwright context with proxy configuration

### Captcha / challenge handling (`session.py`)
- After page loads, waits 3 seconds for challenge pages to render their interactive elements
- Scans for common challenge selectors: Akamai Bot Manager, PerimeterX, Cloudflare Turnstile
- Clicks the challenge checkbox/button if found, then waits 3 more seconds for resolution
- Falls back gracefully if no challenge element is detected

### Proxy enforcement (`config.py`, `tasks.py`)
- Changed `ProxyConfig.direct_fallback` default from `True` to `False`
- All connections now go through proxy pool – real IP is never exposed
- `fetch_with_browser()` receives `proxy_pool` from `TaskManager._check_once()`

### Log colouring (`web.py`)
- Already working: the `logClass()` JavaScript function matches `OK` and `check #N OK` patterns and applies the `log-success` CSS class (green `#2ecc71`)
- WARNING-level error messages (403, Access denied, timed out) continue to display in red

## Previous Issues (Resolved)

1. **curl_cffi getting 403 from Pokemon Center** – Akamai Bot Manager requires JavaScript execution; curl_cffi cannot handle this. Resolved by adding Playwright browser-based fetch.
2. **Browser not using proxies** – `fetch_with_browser()` originally had no proxy support. Now routes through the proxy pool using Playwright context-level proxy.
3. **Direct connection fallback exposing real IP** – `direct_fallback` now defaults to `False` so traffic always goes through proxies.

## Architecture

```
TaskManager._check_once()
  ├── pokemoncenter → fetch_with_browser(proxy_pool=...)
  │     ├── Get proxy from pool
  │     ├── Create Playwright context with proxy
  │     ├── Navigate + wait 3s for challenge
  │     ├── Click captcha if found + wait 3s
  │     ├── Extract page HTML
  │     ├── 403? → mark proxy failed, retry next
  │     └── Raise ConnectionError after max_retries
  └── other sites → fetch(proxy_pool=..., direct_fallback=False)
        ├── curl_cffi with Chrome TLS fingerprint
        ├── aiohttp fallback
        ├── Proxy rotation with smart retry budgets
        └── No direct connection fallback
```
