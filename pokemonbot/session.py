"""HTTP session management with proxy rotation and fingerprint diversity."""

from __future__ import annotations

import logging
import random
import ssl
from typing import Any
from urllib.parse import urlparse

import aiohttp
from aiohttp_socks import ProxyConnector

from pokemonbot.proxy import Proxy, ProxyPool

logger = logging.getLogger(__name__)


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


async def fetch(
    url: str,
    *,
    proxy_pool: ProxyPool | None = None,
    user_agents: list[str] | None = None,
    timeout: float = 30.0,
    extra_headers: dict[str, str] | None = None,
    max_retries: int = 3,
) -> dict[str, Any]:
    """Fetch a URL with automatic proxy rotation on failure.

    When a *proxy_pool* is provided the function cycles through **all**
    available (non-cooldown) proxies before giving up, so ``max_retries``
    only applies when no proxy pool is used.  Failed proxies are placed on
    cooldown so they are automatically skipped on subsequent requests.

    Returns a dict with ``status``, ``body``, ``headers``, and ``url``.
    """
    # Merge domain-specific overrides with caller-supplied headers.
    domain_headers, domain_cookies = _get_domain_overrides(url)
    merged_headers = {**domain_headers, **(extra_headers or {})}

    # When we have a proxy pool, try every available proxy (not just 3).
    attempts = proxy_pool.size if proxy_pool else max_retries

    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        if proxy_pool:
            proxy = proxy_pool.next_available()
            if proxy is None:
                # All proxies are on cooldown
                logger.warning("All %d proxies on cooldown for %s", proxy_pool.size, url)
                break
        else:
            proxy = None

        session = await create_session(
            proxy=proxy,
            user_agents=user_agents,
            timeout=timeout,
            headers=merged_headers or None,
            cookies=domain_cookies or None,
        )
        try:
            async with session:
                logger.debug(
                    "Attempt %d/%d – GET %s via %s",
                    attempt,
                    attempts,
                    url,
                    proxy or "direct",
                )
                async with session.get(url, allow_redirects=True) as resp:
                    body = await resp.text()
                    return {
                        "status": resp.status,
                        "body": body,
                        "headers": dict(resp.headers),
                        "url": str(resp.url),
                    }
        except Exception as exc:
            last_error = exc
            logger.warning(
                "Attempt %d/%d failed for %s: %s", attempt, attempts, url, exc
            )
            if proxy and proxy_pool:
                proxy_pool.mark_failed(proxy)

    raise ConnectionError(
        f"All {attempts} attempts failed for {url}"
    ) from last_error
