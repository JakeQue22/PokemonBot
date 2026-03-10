# PokemonBot – Change Log & Progress

## Working

- **Smyths Toys monitoring** – fully functional with `fetch()` (curl_cffi/aiohttp). Uses proxies when available, falls back to direct connection when all proxies fail. Does NOT use browser.
- **Pokemon Center monitoring** – uses Playwright browser with captcha/challenge handling. ALWAYS routes through proxy pool; direct connection is never used.
- **Stock status reporting** – successful checks now report the actual stock status (e.g. "Out of stock", "In stock") instead of generic "no change"
- **Log colouring** – successful checks (`OK (HTTP 200)`) display in green in the web UI

## ⚠️ Proxy Policy

- **Pokemon Center**: ALWAYS uses proxy. `direct_fallback` is disabled (`_PROXY_REQUIRED_SITES`). Real IP is never exposed.
- **Smyths / other sites**: Uses proxies when available. Falls back to direct connection when all proxies fail. This is necessary because public free proxies are unreliable and Smyths does not require bot protection bypass.

## Changes Made

### Per-site proxy policy (`tasks.py`)
- Added `_PROXY_REQUIRED_SITES = frozenset({"pokemoncenter"})` – sites that MUST always use a proxy
- `_check_once()` computes `direct_fallback` per-site: `False` for pokemoncenter, `True` for others
- **Previous mistake**: Setting `direct_fallback=False` globally broke Smyths because unreliable free proxies all failed and no direct fallback was available

### Stock status reporting (`monitor.py`, `tasks.py`)
- Added `describe_status()` method to `BaseMonitor` and all subclasses (`PokemonCenterMonitor`, `SmythsToysMonitor`)
- Returns human-readable status: "Out of stock", "In stock", "Queue active", "Access denied (bot protection)", etc.
- `_check_once()` now logs the stock status: `Monitor [name] check #N OK (HTTP 200) – Out of stock`

### Browser-based fetch with proxy support (`session.py`)
- `fetch_with_browser()` accepts `proxy_pool`, `proxy_timeout`, and `max_retries` parameters
- Each browser attempt is routed through a proxy from the pool (Playwright context-level proxy)
- Proxy failures (403, connection errors) mark the proxy as failed and retry with the next proxy

### Captcha / challenge handling (`session.py`)
- After page loads, waits 3 seconds for challenge pages to render their interactive elements
- Scans for common challenge selectors: Akamai Bot Manager, PerimeterX, Cloudflare Turnstile
- Clicks the challenge checkbox/button if found, then waits 3 more seconds for resolution
- Falls back gracefully if no challenge element is detected

### Log colouring (`web.py`)
- Already working: the `logClass()` JavaScript function matches `OK` and `check #N OK` patterns and applies the `log-success` CSS class (green `#2ecc71`)

## Previous Issues (Resolved)

1. **curl_cffi getting 403 from Pokemon Center** – Akamai Bot Manager requires JavaScript; curl_cffi cannot handle this. Resolved by adding Playwright browser-based fetch.
2. **Browser not using proxies** – `fetch_with_browser()` originally had no proxy support. Now routes through the proxy pool using Playwright context-level proxy.
3. **Direct connection fallback exposing real IP for Pokemon Center** – `_PROXY_REQUIRED_SITES` ensures pokemoncenter never uses direct fallback.
4. **Global `direct_fallback=False` breaking Smyths** – Fixed by making proxy policy per-site. Smyths allows direct fallback; pokemoncenter does not.
5. **Logs showing "no change" instead of stock status** – Fixed by adding `describe_status()` method to monitors.

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
  │     └── Raise ConnectionError after max_retries (NO direct fallback)
  └── other sites → fetch(proxy_pool=..., direct_fallback=True)
        ├── curl_cffi with Chrome TLS fingerprint
        ├── aiohttp fallback
        ├── Proxy rotation with smart retry budgets
        └── Direct connection fallback when all proxies fail
```
