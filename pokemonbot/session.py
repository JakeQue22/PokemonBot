"""HTTP session management with proxy rotation and fingerprint diversity."""

from __future__ import annotations

import asyncio
import itertools
import logging
import random
import ssl
from collections.abc import Callable, Coroutine
from typing import Any
from urllib.parse import urlparse

import aiohttp
from aiohttp_socks import ProxyConnector

from pokemonbot.proxy import Proxy, ProxyPool

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# curl_cffi – optional but strongly preferred for anti-bot bypass
# ---------------------------------------------------------------------------
try:
    from curl_cffi.requests import AsyncSession as CffiAsyncSession

    _HAS_CURL_CFFI = True
except ImportError:  # pragma: no cover
    _HAS_CURL_CFFI = False

# ---------------------------------------------------------------------------
# Playwright – browser-based fetch for sites requiring JS execution
# ---------------------------------------------------------------------------
try:
    from playwright.async_api import async_playwright

    _HAS_PLAYWRIGHT = True
except ImportError:  # pragma: no cover
    _HAS_PLAYWRIGHT = False

# Browser versions to impersonate.  curl_cffi supports strings like
# ``"chrome131"`` which reproduce the *exact* TLS/JA3/HTTP2 fingerprint
# of that browser, defeating Cloudflare & Akamai bot detection.
_IMPERSONATE_BROWSERS: list[str] = [
    "chrome136",
    "chrome131",
    "chrome124",
]

# Maximum number of fast-fail (non-timeout) proxy skips per fetch() call.
# Fast failures (SOCKS, TLS, CONNECT errors) return in < 1 s so trying
# many in a row adds negligible wall-clock time compared to timeouts.
_MAX_FAST_PROXY_SKIPS: int = 100

# Type alias for fetcher backend functions (curl_cffi / aiohttp).
_Fetcher = Callable[..., Coroutine[Any, Any, dict[str, Any]]]


def _default_ssl_context() -> ssl.SSLContext:
    """Return an SSL context that behaves like a real browser.

    Uses the system CA bundle for certificate verification so that CDN/WAF
    servers (Akamai, Cloudflare, etc.) see a normal TLS handshake instead
    of the ``ssl=False`` fingerprint that many bot-detection systems flag.
    """
    ctx = ssl.create_default_context()
    # Broad protocol/cipher support matching modern browsers
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return ctx


# Per-domain cookie / header overrides.  When a URL matches one of these
# domains the extra headers and cookies are injected automatically so
# that locale selection is handled without requiring a captcha click.
_DOMAIN_OVERRIDES: dict[str, dict[str, Any]] = {
    "www.pokemoncenter.com": {
        "headers": {
            "Accept-Language": "en-GB,en;q=0.9",
            # Sec-CH-UA client hints – these are sent by real Chrome and
            # checked by Akamai Bot Manager.  Without them the request
            # looks like a headless/automated client.
            "Sec-CH-UA": '"Chromium";v="136", "Google Chrome";v="136", "Not.A/Brand";v="99"',
            "Sec-CH-UA-Mobile": "?0",
            "Sec-CH-UA-Platform": '"Windows"',
        },
        "cookies": {
            "pokemon-website-language": "en-gb",
            "pokemon-website-country": "gb",
        },
    },
}


def _format_error(exc: Exception) -> str:
    """Return a human-readable description of *exc*.

    Some exceptions (e.g. ``asyncio.TimeoutError()``) have an empty
    ``str()`` representation, making retry log lines unreadable.  This
    helper falls back to the class name so there is always useful
    context in the log.
    """
    msg = str(exc)
    if msg:
        return msg
    return type(exc).__qualname__


def _is_timeout_error(exc: Exception) -> bool:
    """Return ``True`` if *exc* indicates a connection/proxy timeout.

    Timeout errors are *expensive* (each waits for the full proxy timeout,
    e.g. 10 s).  Other proxy errors – SOCKS failures, TLS errors, CONNECT
    tunnel rejections – typically resolve in < 1 s and are much cheaper.
    By distinguishing the two the retry loop can try many more proxies
    without increasing wall-clock time.
    """
    # Check concrete exception types first.
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return True
    # curl_cffi wraps libcurl errors in generic exceptions; fall back to
    # string matching for the curl error-28 ("Connection timed out") case.
    msg = str(exc).lower()
    return "timed out" in msg or "timeout" in msg


# Human-readable reason phrases for retryable status codes.
_STATUS_REASONS: dict[int, str] = {
    403: "Forbidden (bot protection)",
    429: "Too Many Requests",
}


def _is_challenge_page(body: str) -> bool:
    """Return ``True`` if *body* looks like a bot-protection challenge page.

    Akamai Bot Manager, PerimeterX, and similar WAFs sometimes respond
    with HTTP 200 but serve a JavaScript challenge page instead of real
    content.  Treating these as successful responses inflates the
    dashboard "Successes" counter and prevents the retry loop from trying
    another proxy.
    """
    if not body:
        return False

    lower = body.lower()

    # Akamai Bot Manager challenge markers
    _CHALLENGE_MARKERS = (
        "access denied",
        "reference&#32;&#35;",  # Akamai "Reference #<number>"
        "px-captcha",  # PerimeterX
        "please enable cookies",
        "managed by akamai",
        "/_sec/cp_challenge/",  # Akamai challenge path
        "challenge-platform",
        "just a moment",  # Cloudflare "Just a moment..."
        "checking your browser",
    )

    for marker in _CHALLENGE_MARKERS:
        if marker in lower:
            return True

    return False


def _random_user_agent(user_agents: list[str]) -> str:
    return random.choice(user_agents) if user_agents else ""


def _build_connector(proxy: Proxy | None) -> aiohttp.BaseConnector:
    ssl_ctx = _default_ssl_context()
    if proxy is None:
        return aiohttp.TCPConnector(ssl=ssl_ctx)
    # Public/untrusted proxies often intercept TLS with their own
    # certificates causing CERTIFICATE_VERIFY_FAILED.  Disable strict
    # verification when traffic is routed through an external proxy.
    return ProxyConnector.from_url(proxy.url, ssl=False)


def _get_domain_overrides(url: str) -> tuple[dict[str, str], dict[str, str]]:
    """Return ``(extra_headers, cookies)`` for *url* based on domain rules."""
    host = urlparse(url).hostname or ""
    overrides = _DOMAIN_OVERRIDES.get(host, {})
    return overrides.get("headers", {}), overrides.get("cookies", {})


async def create_session(
    *,
    proxy: Proxy | None = None,
    user_agents: list[str] | None = None,
    timeout: float = 30.0,
    headers: dict[str, str] | None = None,
    cookies: dict[str, str] | None = None,
) -> aiohttp.ClientSession:
    """Create an ``aiohttp.ClientSession`` pre-configured with an optional proxy."""
    connector = _build_connector(proxy)
    ua = _random_user_agent(user_agents or [])
    default_headers: dict[str, str] = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
    }
    if ua:
        default_headers["User-Agent"] = ua
    if headers:
        default_headers.update(headers)

    return aiohttp.ClientSession(
        connector=connector,
        timeout=aiohttp.ClientTimeout(total=timeout),
        headers=default_headers,
        cookies=cookies or None,
    )


# ---------------------------------------------------------------------------
# curl_cffi-based fetch (preferred – impersonates real Chrome fingerprint)
# ---------------------------------------------------------------------------

async def _fetch_with_curl_cffi(
    url: str,
    *,
    proxy: Proxy | None = None,
    user_agents: list[str] | None = None,
    timeout: float = 30.0,
    headers: dict[str, str] | None = None,
    cookies: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Fetch *url* using curl_cffi with Chrome browser impersonation.

    This gives the request a genuine Chrome TLS/JA3/HTTP2 fingerprint,
    which is critical for bypassing Cloudflare and Akamai bot detection
    on sites like pokemoncenter.com.
    """
    browser = random.choice(_IMPERSONATE_BROWSERS)
    ua = _random_user_agent(user_agents or [])

    req_headers: dict[str, str] = {}
    if ua:
        req_headers["User-Agent"] = ua
    if headers:
        req_headers.update(headers)

    proxy_url = proxy.url if proxy else None

    async with CffiAsyncSession() as session:
        resp = await session.get(
            url,
            headers=req_headers or None,
            cookies=cookies or None,
            proxy=proxy_url,
            timeout=timeout,
            impersonate=browser,
            allow_redirects=True,
            verify=proxy is None,  # skip TLS verification through proxies
        )
        return {
            "status": resp.status_code,
            "body": resp.text,
            "headers": dict(resp.headers),
            "url": str(resp.url),
        }


# ---------------------------------------------------------------------------
# aiohttp-based fetch (fallback when curl_cffi is not available)
# ---------------------------------------------------------------------------

async def _fetch_with_aiohttp(
    url: str,
    *,
    proxy: Proxy | None = None,
    user_agents: list[str] | None = None,
    timeout: float = 30.0,
    headers: dict[str, str] | None = None,
    cookies: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Fetch *url* using plain aiohttp (no browser fingerprint impersonation)."""
    session = await create_session(
        proxy=proxy,
        user_agents=user_agents,
        timeout=timeout,
        headers=headers,
        cookies=cookies,
    )
    async with session:
        async with session.get(url, allow_redirects=True) as resp:
            body = await resp.text()
            return {
                "status": resp.status,
                "body": body,
                "headers": dict(resp.headers),
                "url": str(resp.url),
            }


# ---------------------------------------------------------------------------
# Public fetch() – retries across proxies with jitter
# ---------------------------------------------------------------------------

async def fetch(
    url: str,
    *,
    proxy_pool: ProxyPool | None = None,
    user_agents: list[str] | None = None,
    timeout: float = 30.0,
    proxy_timeout: float | None = None,
    extra_headers: dict[str, str] | None = None,
    max_retries: int = 3,
    direct_fallback: bool = True,
    retry_on_status: frozenset[int] | None = None,
) -> dict[str, Any]:
    """Fetch a URL with automatic proxy rotation on failure.

    *max_retries* caps the number of **timeout** proxy attempts per call.
    Non-timeout proxy failures (SOCKS, TLS, CONNECT errors) are much
    faster and are given a separate, larger budget so the bot can skip
    through many dead proxies without spending minutes on each one.
    Failed proxies are placed on cooldown so they are automatically
    skipped on subsequent requests.

    *proxy_timeout*, when set, overrides *timeout* for requests routed
    through a proxy.  Public proxies are unreliable and a shorter timeout
    (e.g. 10 s) prevents one dead proxy from blocking the whole cycle.

    When *direct_fallback* is ``False`` the final direct (no-proxy)
    request that normally fires after all proxy attempts fail is skipped.
    This is useful when the user has a large proxy pool and never wants
    their real IP exposed.

    *retry_on_status*, when set, is a frozenset of HTTP status codes
    (e.g. ``frozenset({403})``) that should be treated as retryable
    proxy failures rather than successful responses.  This is useful for
    sites with bot protection (e.g. Akamai on pokemoncenter.com) where
    a 403 from one proxy may succeed from another.

    When ``curl_cffi`` is installed the request impersonates a real Chrome
    browser (TLS fingerprint, HTTP/2 settings, etc.) which dramatically
    reduces 403 blocks from Cloudflare / Akamai.

    Returns a dict with ``status``, ``body``, ``headers``, and ``url``.
    """
    # Merge domain-specific overrides with caller-supplied headers.
    domain_headers, domain_cookies = _get_domain_overrides(url)
    merged_headers = {**domain_headers, **(extra_headers or {})}

    # Build an ordered list of fetcher backends.  When curl_cffi is
    # available the list contains *both* backends so the retry loop can
    # alternate between them.  Different HTTP / TLS stacks handle
    # different proxies differently – curl_cffi gives Chrome TLS
    # fingerprint impersonation while aiohttp uses a standard Python
    # TLS stack which may succeed where curl fails (and vice-versa).
    backends: list[_Fetcher] = []
    if _HAS_CURL_CFFI:
        backends.append(_fetch_with_curl_cffi)
    backends.append(_fetch_with_aiohttp)
    backend_cycle = itertools.cycle(backends)

    # --- Retry budget --------------------------------------------------
    # When a proxy pool is present, proxy failures fall into two buckets:
    #
    #  * **Timeout failures** (curl error 28, "Connection timed out") are
    #    *expensive* – each blocks for the full proxy_timeout (e.g. 10 s).
    #    These are capped at *max_retries* so the monitor doesn't stall
    #    for minutes.
    #
    #  * **Fast failures** (SOCKS, TLS, CONNECT errors) return in < 1 s
    #    and are essentially free.  These do NOT count toward max_retries
    #    so the bot can cheaply skip many bad proxies until it finds a
    #    working one.  A separate cap prevents infinite loops.
    #
    # Without a proxy pool every error counts toward *max_retries* (the
    # old behaviour).
    max_fast = min(proxy_pool.size, _MAX_FAST_PROXY_SKIPS) if proxy_pool else 0
    timeout_fails = 0
    fast_fails = 0

    last_error: Exception | None = None
    last_result: dict[str, Any] | None = None
    while True:
        # --- budget check ---
        if timeout_fails >= max_retries:
            break
        if proxy_pool and fast_fails >= max_fast:
            break

        if proxy_pool:
            proxy = proxy_pool.next_available()
            if proxy is None:
                # All proxies are on cooldown
                logger.warning("All %d proxies on cooldown for %s", proxy_pool.size, url)
                break
        else:
            proxy = None

        # Use a shorter timeout for proxy requests so dead proxies don't
        # block the monitoring cycle for minutes.
        effective_timeout = (
            proxy_timeout if (proxy is not None and proxy_timeout is not None)
            else timeout
        )

        do_fetch = next(backend_cycle)
        try:
            logger.debug(
                "GET %s via %s (timeout=%.0fs)",
                url,
                proxy or "direct",
                effective_timeout,
            )
            result = await do_fetch(
                url,
                proxy=proxy,
                user_agents=user_agents,
                timeout=effective_timeout,
                headers=merged_headers or None,
                cookies=domain_cookies or None,
            )

            # When retry_on_status is set and the response has a
            # retryable status code, treat this as a fast proxy failure
            # so another proxy is tried.  This handles bot protection
            # (e.g. Akamai on pokemoncenter.com returning 403) where
            # the same URL may succeed from a different IP / fingerprint.
            resp_status = result.get("status", 0)
            if (
                retry_on_status is not None
                and resp_status in retry_on_status
                and proxy is not None
            ):
                fast_fails += 1
                proxy_pool.mark_failed(proxy)  # type: ignore[union-attr]
                logger.warning(
                    "Proxy skip (%d) for %s: %d %s",
                    fast_fails, url, resp_status,
                    _STATUS_REASONS.get(resp_status, ""),
                )
                last_result = result
                continue

            # Mark the proxy as preferred (sticky) so it is reused
            # on subsequent requests until it fails.
            if proxy is not None and proxy_pool is not None:
                proxy_pool.mark_success(proxy)
            return result
        except Exception as exc:
            last_error = exc

            if proxy and proxy_pool:
                proxy_pool.mark_failed(proxy)

            # Classify the failure and adjust budget / logging.
            if proxy is not None and not _is_timeout_error(exc):
                # Fast proxy failure – skip to next proxy immediately.
                fast_fails += 1
                logger.warning("Proxy skip (%d) for %s: %s", fast_fails, url, _format_error(exc))
            else:
                # Timeout or non-proxy failure – count toward max_retries.
                timeout_fails += 1
                logger.warning(
                    "Attempt %d/%d failed for %s: %s",
                    timeout_fails, max_retries, url, _format_error(exc),
                )
                # Small random jitter between retries to look more human-like
                # and avoid hammering the target in a tight loop.
                if timeout_fails < max_retries:
                    jitter = random.uniform(0.5, 2.0)
                    await asyncio.sleep(jitter)

    total = timeout_fails + fast_fails

    # --- Direct-connection fallback ---
    # When a proxy pool is configured but every proxied attempt failed,
    # try each backend once without a proxy.  curl_cffi provides Chrome
    # fingerprint impersonation; aiohttp uses a standard TLS stack.
    # Trying both maximises the chance of getting through.
    # Disabled when direct_fallback=False (user has a large pool and
    # never wants their real IP exposed).
    if proxy_pool is not None and direct_fallback:
        for do_fetch in backends:
            try:
                logger.info(
                    "Trying direct connection for %s",
                    url,
                )
                result = await do_fetch(
                    url,
                    proxy=None,
                    user_agents=user_agents,
                    timeout=timeout,
                    headers=merged_headers or None,
                    cookies=domain_cookies or None,
                )
                return result
            except Exception as exc:
                last_error = exc
                logger.warning("Direct fallback failed for %s: %s", url, _format_error(exc))

    # If every proxy returned a retryable status code (e.g. 403), return
    # the last such response rather than raising ConnectionError.  The
    # monitor layer can still inspect the status and act accordingly.
    if last_result is not None and last_error is None:
        return last_result

    raise ConnectionError(
        f"All {total} attempts failed for {url}"
    ) from last_error


# ---------------------------------------------------------------------------
# Playwright browser-based fetch – for sites requiring JS execution
# ---------------------------------------------------------------------------
# Akamai Bot Manager (used by pokemoncenter.com) requires a real browser
# that executes JavaScript to set challenge cookies (_abck, bm_sz).
# Without these cookies every request returns 403.  A headless Chromium
# instance handles the challenge automatically.
#
# The browser is started lazily on first use and reused across calls.
# Each fetch opens a new page, navigates, waits for any JS challenge to
# resolve, extracts the fully-rendered HTML, then closes the page.

_pw_instance: Any = None
_pw_browser: Any = None


async def _ensure_browser() -> Any:
    """Lazily start a shared Playwright Chromium browser.

    Returns the browser instance.  Restarts it if a previous instance
    was closed or crashed.
    """
    global _pw_instance, _pw_browser

    if _pw_browser is not None and _pw_browser.is_connected():
        return _pw_browser

    # Clean up any stale state.
    if _pw_instance is not None:
        try:
            await _pw_instance.stop()
        except Exception:
            pass

    _pw_instance = await async_playwright().start()
    _pw_browser = await _pw_instance.chromium.launch(
        headless=True,
        args=[
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
        ],
    )
    logger.info("Playwright Chromium browser started for JS-protected sites")
    return _pw_browser


async def close_browser() -> None:
    """Shut down the shared Playwright browser (call on app shutdown)."""
    global _pw_instance, _pw_browser
    if _pw_browser is not None:
        try:
            await _pw_browser.close()
        except Exception:
            pass
        _pw_browser = None
    if _pw_instance is not None:
        try:
            await _pw_instance.stop()
        except Exception:
            pass
        _pw_instance = None


async def _browser_fetch_once(
    url: str,
    *,
    proxy: Proxy | None = None,
    timeout: float = 30.0,
    extra_headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Single browser fetch attempt, optionally routed through *proxy*.

    Handles Akamai Bot Manager / PerimeterX challenges by waiting for
    the challenge page to render, clicking the captcha checkbox if
    present, and then waiting for the page to resolve.
    """
    browser = await _ensure_browser()

    domain_headers, domain_cookies = _get_domain_overrides(url)
    merged_headers = {**domain_headers, **(extra_headers or {})}

    ctx_kwargs: dict[str, Any] = {
        "user_agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/136.0.0.0 Safari/537.36"
        ),
        "viewport": {"width": 1920, "height": 1080},
        "locale": "en-GB",
        "extra_http_headers": merged_headers,
    }

    # Route the browser context through a proxy when available.
    if proxy is not None:
        proxy_cfg: dict[str, str] = {"server": proxy.url}
        if proxy.username:
            proxy_cfg["username"] = proxy.username
            proxy_cfg["password"] = proxy.password
        ctx_kwargs["proxy"] = proxy_cfg
        ctx_kwargs["ignore_https_errors"] = True

    context = await browser.new_context(**ctx_kwargs)

    # Inject domain cookies before navigation.
    if domain_cookies:
        host = urlparse(url).hostname or ""
        cookie_list = [
            {"name": k, "value": v, "domain": host, "path": "/"}
            for k, v in domain_cookies.items()
        ]
        await context.add_cookies(cookie_list)

    page = await context.new_page()
    try:
        timeout_ms = int(timeout * 1000)
        response = await page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=timeout_ms,
        )

        # -----------------------------------------------------------------
        # Challenge / captcha handling
        # -----------------------------------------------------------------
        # Wait 3 seconds for any Akamai / PerimeterX / DataDome challenge
        # page to fully render its interactive elements.
        await asyncio.sleep(3)

        # Common challenge selectors used by bot-protection services.
        _CHALLENGE_SELECTORS = [
            # Akamai Bot Manager / PerimeterX challenge checkbox
            "#challenge-stage input[type='checkbox']",
            "#px-captcha",
            "iframe[title*='challenge']",
            "iframe[src*='captcha']",
            # PerimeterX HUMAN challenge
            "#px-captcha-wrapper button",
            # Cloudflare Turnstile
            "iframe[src*='challenges.cloudflare.com']",
            # Generic challenge buttons
            "#challenge-running",
            "button[data-action='verify']",
        ]

        for selector in _CHALLENGE_SELECTORS:
            try:
                elem = await page.query_selector(selector)
                if elem is not None and await elem.is_visible():
                    logger.info("Clicking challenge element %s for %s", selector, url)
                    await elem.click()
                    # Wait for the challenge to resolve after clicking.
                    await asyncio.sleep(3)
                    break
            except Exception:
                logger.debug("Challenge selector %s not interactable for %s", selector, url)

        # Wait for network to settle after any challenge resolution.
        try:
            await page.wait_for_load_state("networkidle", timeout=timeout_ms)
        except Exception as exc:
            logger.debug("Network idle wait failed for %s: %s", url, _format_error(exc))

        # -----------------------------------------------------------------
        # Wait for JS-rendered product content
        # -----------------------------------------------------------------
        # Many e-commerce sites (PokemonCenter, etc.) use React / Next.js
        # where the initial HTML is a thin shell and product details
        # (including "Add to Basket" / "Add to Cart" buttons) render
        # after JavaScript hydration.  Poll briefly for stock-related
        # content to appear in the page before capturing its HTML.
        # If the content is already present the call returns immediately;
        # if it never appears the timeout fires and we proceed with
        # whatever the page currently contains.
        try:
            await page.wait_for_function(
                r"""() => {
                    const text = (document.body && document.body.innerText) || '';
                    const html = (document.body && document.body.innerHTML) || '';
                    return /add.to.(cart|basket)/i.test(text)
                        || /sold.out/i.test(text)
                        || /out.of.stock/i.test(text)
                        || /currently.unavailable/i.test(text)
                        || /"availability"\s*:/i.test(html);
                }""",
                timeout=8_000,
            )
        except Exception:
            logger.debug(
                "Product content wait timed out for %s – "
                "page may lack stock data or JS has not rendered",
                url,
            )

        body = await page.content()
        status = response.status if response else 0
        resp_headers = await response.all_headers() if response else {}
        final_url = page.url

        return {
            "status": status,
            "body": body,
            "headers": dict(resp_headers),
            "url": final_url,
        }
    finally:
        await page.close()
        await context.close()


async def fetch_with_browser(
    url: str,
    *,
    proxy_pool: ProxyPool | None = None,
    timeout: float = 30.0,
    proxy_timeout: float | None = None,
    extra_headers: dict[str, str] | None = None,
    max_retries: int = 3,
    direct_fallback: bool = True,
) -> dict[str, Any]:
    """Fetch *url* using a real Chromium browser via Playwright.

    This executes JavaScript automatically, handling Akamai Bot Manager
    challenges that block pure HTTP clients (curl_cffi, aiohttp).

    When *proxy_pool* is provided, each attempt routes through a
    different proxy.  Proxies that return 403 or fail are marked as
    failed and the next proxy is tried.

    When *direct_fallback* is ``True`` (the default) and a proxy pool
    is configured but every proxied attempt fails, one final attempt is
    made without a proxy.  Set to ``False`` for sites where the real IP
    must never be exposed (e.g. pokemoncenter).

    Returns a dict with ``status``, ``body``, ``headers``, and ``url``
    matching the format used by :func:`fetch`.

    Falls back to :func:`fetch` (curl_cffi/aiohttp) if Playwright is
    not installed.
    """
    if not _HAS_PLAYWRIGHT:
        logger.warning(
            "Playwright not installed – falling back to HTTP fetch for %s",
            url,
        )
        return await fetch(url, timeout=timeout, extra_headers=extra_headers)

    effective_timeout = proxy_timeout if proxy_timeout is not None else timeout
    attempts = 0

    while attempts < max_retries:
        proxy: Proxy | None = None
        if proxy_pool is not None:
            proxy = proxy_pool.next_available()
            if proxy is None:
                logger.warning(
                    "All %d proxies on cooldown for browser fetch of %s",
                    proxy_pool.size, url,
                )
                break

        attempts += 1
        try:
            result = await _browser_fetch_once(
                url,
                proxy=proxy,
                timeout=effective_timeout,
                extra_headers=extra_headers,
            )

            # Treat 403 as a proxy failure when we have a proxy pool.
            resp_status = result.get("status", 0)
            if resp_status == 403 and proxy is not None and proxy_pool is not None:
                proxy_pool.mark_failed(proxy)
                logger.warning(
                    "Browser proxy skip (%d/%d) for %s: 403 %s via %s",
                    attempts, max_retries, url,
                    _STATUS_REASONS.get(403, ""), proxy,
                )
                continue

            # Detect challenge/bot-protection pages that return HTTP 200
            # but contain no real content.  Without this check these
            # inflate the dashboard "Successes" counter and hide the
            # fact that the proxy did not actually reach real content.
            resp_body = result.get("body", "")
            if _is_challenge_page(resp_body) and proxy is not None and proxy_pool is not None:
                proxy_pool.mark_failed(proxy)
                logger.warning(
                    "Browser proxy skip (%d/%d) for %s: "
                    "challenge/bot page (HTTP %d) via %s",
                    attempts, max_retries, url, resp_status, proxy,
                )
                continue

            # Mark the proxy as preferred (sticky) so it is reused
            # on subsequent requests until it fails.
            if proxy is not None and proxy_pool is not None:
                proxy_pool.mark_success(proxy)
            return result
        except Exception as exc:
            if proxy is not None and proxy_pool is not None:
                proxy_pool.mark_failed(proxy)
            logger.warning(
                "Browser attempt %d/%d failed for %s via %s: %s",
                attempts, max_retries, url, proxy or "direct",
                _format_error(exc),
            )
            if attempts < max_retries:
                await asyncio.sleep(random.uniform(0.5, 2.0))

    # --- Direct-connection fallback ---
    # When a proxy pool was used but every proxied attempt failed, try
    # one final browser fetch without a proxy.  This mirrors the
    # direct_fallback behaviour in fetch() and keeps monitors alive
    # when all public proxies are dead.
    if proxy_pool is not None and direct_fallback:
        try:
            logger.info("Trying direct browser connection for %s", url)
            return await _browser_fetch_once(
                url,
                proxy=None,
                timeout=timeout,
                extra_headers=extra_headers,
            )
        except Exception as exc:
            logger.warning(
                "Direct browser fallback failed for %s: %s",
                url, _format_error(exc),
            )

    raise ConnectionError(
        f"All {attempts} browser attempts failed for {url}"
    )
