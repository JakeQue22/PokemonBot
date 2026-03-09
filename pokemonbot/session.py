"""HTTP session management with proxy rotation and fingerprint diversity."""

from __future__ import annotations

import asyncio
import logging
import random
import ssl
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
        },
        "cookies": {
            "pokemon-website-language": "en-gb",
            "pokemon-website-country": "gb",
        },
    },
}


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

    When ``curl_cffi`` is installed the request impersonates a real Chrome
    browser (TLS fingerprint, HTTP/2 settings, etc.) which dramatically
    reduces 403 blocks from Cloudflare / Akamai.

    Returns a dict with ``status``, ``body``, ``headers``, and ``url``.
    """
    # Merge domain-specific overrides with caller-supplied headers.
    domain_headers, domain_cookies = _get_domain_overrides(url)
    merged_headers = {**domain_headers, **(extra_headers or {})}

    # Choose the fetcher implementation.
    do_fetch = _fetch_with_curl_cffi if _HAS_CURL_CFFI else _fetch_with_aiohttp

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
            return result
        except Exception as exc:
            last_error = exc

            if proxy and proxy_pool:
                proxy_pool.mark_failed(proxy)

            # Classify the failure and adjust budget / logging.
            if proxy is not None and not _is_timeout_error(exc):
                # Fast proxy failure – skip to next proxy immediately.
                fast_fails += 1
                logger.warning("Proxy skip (%d) for %s: %s", fast_fails, url, exc)
            else:
                # Timeout or non-proxy failure – count toward max_retries.
                timeout_fails += 1
                logger.warning(
                    "Attempt %d/%d failed for %s: %s",
                    timeout_fails, max_retries, url, exc,
                )
                # Small random jitter between retries to look more human-like
                # and avoid hammering the target in a tight loop.
                if timeout_fails < max_retries:
                    jitter = random.uniform(0.5, 2.0)
                    await asyncio.sleep(jitter)

    total = timeout_fails + fast_fails

    # --- Direct-connection fallback ---
    # When a proxy pool is configured but every proxied attempt failed,
    # try ONE final request without a proxy.  This keeps the monitors
    # alive even when all public proxies are dead.
    # Disabled when direct_fallback=False (user has a large pool and
    # never wants their real IP exposed).
    if proxy_pool is not None and direct_fallback:
        try:
            logger.info(
                "All proxy attempts exhausted – trying direct connection for %s",
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
            logger.warning("Direct fallback also failed for %s: %s", url, exc)

    raise ConnectionError(
        f"All {total} attempts failed for {url}"
    ) from last_error
