"""Proxy loading, validation, and round-robin cycling."""

from __future__ import annotations

import itertools
import logging
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Proxy:
    """Parsed proxy entry."""

    protocol: str  # http, https, socks4, socks5
    host: str
    port: int
    username: str = ""
    password: str = ""

    @property
    def url(self) -> str:
        auth = ""
        if self.username:
            auth = f"{self.username}:{self.password}@"
        return f"{self.protocol}://{auth}{self.host}:{self.port}"

    def __str__(self) -> str:
        return self.url


def parse_proxy(line: str) -> Proxy:
    """Parse a proxy from a line of text.

    Supported formats::

        protocol://host:port
        protocol://user:pass@host:port
        host:port                       (defaults to http)
        host:port:user:pass             (defaults to http)
    """
    line = line.strip()
    if not line:
        raise ValueError("Empty proxy line")

    protocol = "http"
    if "://" in line:
        protocol, _, line = line.partition("://")

    username = ""
    password = ""

    if "@" in line:
        creds, _, hostport = line.rpartition("@")
        if ":" in creds:
            username, _, password = creds.partition(":")
        line = hostport

    parts = line.split(":")
    if len(parts) == 2:
        host, port_str = parts
    elif len(parts) == 4:
        host, port_str, username, password = parts
    else:
        raise ValueError(f"Cannot parse proxy: {line}")

    return Proxy(
        protocol=protocol.lower(),
        host=host,
        port=int(port_str),
        username=username,
        password=password,
    )


def load_proxies(path: str | Path) -> list[Proxy]:
    """Load proxies from a text file (one per line, ``#`` comments allowed)."""
    lines = Path(path).read_text().splitlines()
    proxies: list[Proxy] = []
    for i, raw in enumerate(lines, 1):
        raw = raw.strip()
        if not raw or raw.startswith("#"):
            continue
        try:
            proxies.append(parse_proxy(raw))
        except (ValueError, IndexError) as exc:
            logger.warning("Skipping invalid proxy on line %d: %s", i, exc)
    return proxies


def save_proxies(proxies: list[Proxy], path: str | Path) -> None:
    """Write proxies back to a text file (one per line).

    Uses atomic write (temp file + rename) to avoid *EBUSY* or partial-write
    issues when the file is held open by another process or Docker volume.
    """
    import os
    import tempfile

    content = "\n".join(p.url for p in proxies)
    if content:
        content += "\n"

    dest = Path(path)
    try:
        fd, tmp = tempfile.mkstemp(
            dir=str(dest.parent), prefix=".proxies_", suffix=".tmp"
        )
        try:
            os.write(fd, content.encode())
        finally:
            os.close(fd)
        os.replace(tmp, str(dest))
    except OSError:
        # Fallback: direct write (e.g. when rename across filesystems fails)
        dest.write_text(content)


def ensure_proxy_file(path: str | Path) -> Path:
    """Ensure *path* resolves to a regular file, creating it if necessary.

    If the path is a directory (e.g. Docker volume-mount placeholder that
    cannot be removed), a ``proxies.txt`` file is created *inside* it and
    that path is returned instead.
    """
    import shutil

    p = Path(path)
    if p.is_dir():
        logger.warning("Proxy path %s is a directory – attempting to replace", p)
        try:
            shutil.rmtree(p)
        except OSError:
            # Mount point (e.g. Docker volume) – cannot remove.
            # Fall back to writing a file inside the directory.
            p = p / "proxies.txt"
            logger.warning("Cannot remove directory mount; using %s instead", p)
    if not p.exists():
        p.write_text("")
    return p


class ProxyPool:
    """Thread-safe, round-robin proxy pool with optional shuffle and per-proxy stats."""

    def __init__(self, proxies: list[Proxy], *, shuffle: bool = True) -> None:
        if not proxies:
            raise ValueError("Proxy pool requires at least one proxy")
        pool = list(proxies)
        if shuffle:
            random.shuffle(pool)
        self._proxies = pool
        self._cycle: Iterator[Proxy] = itertools.cycle(self._proxies)
        self._failed: set[str] = set()
        # Per-proxy counters keyed by proxy URL
        self._requests: dict[str, int] = {p.url: 0 for p in self._proxies}
        self._failures: dict[str, int] = {p.url: 0 for p in self._proxies}

    @property
    def size(self) -> int:
        return len(self._proxies)

    @property
    def proxies(self) -> list[Proxy]:
        return list(self._proxies)

    def next(self) -> Proxy:
        """Return the next proxy in the rotation."""
        proxy = next(self._cycle)
        self._requests[proxy.url] = self._requests.get(proxy.url, 0) + 1
        return proxy

    def mark_failed(self, proxy: Proxy) -> None:
        """Record a proxy as failed (for informational purposes)."""
        self._failed.add(proxy.url)
        self._failures[proxy.url] = self._failures.get(proxy.url, 0) + 1
        logger.debug("Proxy marked as failed: %s", proxy.url)

    @property
    def failed_count(self) -> int:
        return len(self._failed)

    def reset_failures(self) -> None:
        self._failed.clear()

    def stats(self) -> list[dict[str, Any]]:
        """Return per-proxy statistics."""
        return [
            {
                "url": p.url,
                "protocol": p.protocol,
                "host": p.host,
                "port": p.port,
                "requests": self._requests.get(p.url, 0),
                "failures": self._failures.get(p.url, 0),
            }
            for p in self._proxies
        ]


# ---------------------------------------------------------------------------
# Public proxy fetching
# ---------------------------------------------------------------------------

# Well-known public proxy list URLs that return plain-text lists of
# ``ip:port`` entries (one per line).
_PUBLIC_PROXY_SOURCES: list[dict[str, str]] = [
    {
        "url": "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt",
        "protocol": "http",
    },
    {
        "url": "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks5.txt",
        "protocol": "socks5",
    },
    {
        "url": "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt",
        "protocol": "http",
    },
    {
        "url": "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/socks5.txt",
        "protocol": "socks5",
    },
    {
        "url": "https://raw.githubusercontent.com/hookzof/socks5_list/master/proxy.txt",
        "protocol": "socks5",
    },
    {
        "url": "https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/http.txt",
        "protocol": "http",
    },
    {
        "url": "https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/socks5.txt",
        "protocol": "socks5",
    },
    {
        "url": "https://raw.githubusercontent.com/clarketm/proxy-list/master/proxy-list-raw.txt",
        "protocol": "http",
    },
]


async def _fetch_source(
    session: Any,
    source: dict[str, str],
    seen: set[str],
    max_per_source: int,
) -> list[Proxy]:
    """Fetch proxies from a single source URL."""
    import aiohttp

    proxies: list[Proxy] = []
    url = source["url"]
    try:
        async with session.get(
            url,
            timeout=aiohttp.ClientTimeout(total=20),
        ) as resp:
            if resp.status != 200:
                logger.warning(
                    "Public proxy source returned %d: %s", resp.status, url,
                )
                return proxies
            text = await resp.text()
    except Exception as exc:
        logger.warning("Failed to fetch %s: %s", url, exc)
        return proxies

    count = 0
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            parts = line.split(":")
            if len(parts) != 2:
                continue
            host, port_str = parts
            port = int(port_str)
            proxy_url = f"{source['protocol']}://{host}:{port}"
            if proxy_url in seen:
                continue
            seen.add(proxy_url)
            proxies.append(Proxy(
                protocol=source["protocol"],
                host=host,
                port=port,
            ))
            count += 1
            if count >= max_per_source:
                break
        except (ValueError, IndexError):
            continue

    logger.info("Fetched %d proxies from %s", count, url)
    return proxies


async def fetch_public_proxies(*, max_per_source: int = 150) -> list[Proxy]:
    """Fetch free proxy lists from public GitHub-hosted sources.

    Each source is fetched concurrently with its own timeout so a single
    slow source does not block the others.  Returns a de-duplicated list
    of :class:`Proxy` objects (up to *max_per_source* per source).
    """
    import asyncio

    import aiohttp

    seen: set[str] = set()
    all_proxies: list[Proxy] = []
    successful_sources = 0

    async with aiohttp.ClientSession() as session:
        tasks = [
            _fetch_source(session, source, seen, max_per_source)
            for source in _PUBLIC_PROXY_SOURCES
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for i, result in enumerate(results):
            if isinstance(result, Exception):
                logger.warning(
                    "Public proxy source failed: %s – %s",
                    _PUBLIC_PROXY_SOURCES[i]["url"],
                    result,
                )
            elif result:
                all_proxies.extend(result)
                successful_sources += 1

    logger.info(
        "Fetched %d unique public proxies from %d/%d sources",
        len(all_proxies),
        successful_sources,
        len(_PUBLIC_PROXY_SOURCES),
    )
    return all_proxies
