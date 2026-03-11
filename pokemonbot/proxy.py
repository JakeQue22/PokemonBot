"""Proxy loading, validation, and round-robin cycling."""

from __future__ import annotations

import itertools
import json
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


def _stats_path(proxy_path: str | Path) -> Path:
    """Return the JSON stats file path derived from the proxy list path."""
    p = Path(proxy_path)
    return p.with_suffix(".stats.json")


def save_proxy_stats(
    stats: dict[str, dict[str, int]],
    proxy_path: str | Path,
) -> None:
    """Persist per-proxy request/failure counters to a JSON sidecar file.

    *stats* maps proxy URL → ``{"requests": int, "failures": int}``.
    Uses atomic write to avoid partial-write corruption.
    """
    import os
    import tempfile

    dest = _stats_path(proxy_path)
    content = json.dumps(stats, indent=2)

    try:
        fd, tmp = tempfile.mkstemp(
            dir=str(dest.parent), prefix=".proxystats_", suffix=".tmp",
        )
        try:
            os.write(fd, content.encode())
        finally:
            os.close(fd)
        os.replace(tmp, str(dest))
    except OSError:
        dest.write_text(content)


def load_proxy_stats(proxy_path: str | Path) -> dict[str, dict[str, int]]:
    """Load persisted proxy stats from the JSON sidecar file.

    Returns an empty dict when the file does not exist or is invalid.
    """
    dest = _stats_path(proxy_path)
    if not dest.is_file():
        return {}
    try:
        return json.loads(dest.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Could not load proxy stats from %s: %s", dest, exc)
        return {}


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
    """Thread-safe, round-robin proxy pool with optional shuffle and per-proxy stats.

    Failed proxies are placed on a cooldown so they are skipped by
    :meth:`next_available` for *cooldown_seconds* (default 120 s).

    When *persisted_stats* is supplied (a dict mapping proxy URL to
    ``{"requests": N, "failures": N}``), the pool restores counters
    from the previous session and sorts proxies so that those with the
    highest historical success rate are tried first.
    """

    def __init__(
        self,
        proxies: list[Proxy],
        *,
        shuffle: bool = True,
        cooldown_seconds: float = 120.0,
        persisted_stats: dict[str, dict[str, int]] | None = None,
    ) -> None:
        if not proxies:
            raise ValueError("Proxy pool requires at least one proxy")
        pool = list(proxies)

        # ---- Restore persisted stats & order by success rate ----
        if persisted_stats:
            def _success_rate(p: Proxy) -> float:
                s = persisted_stats.get(p.url)
                if s is None or s.get("requests", 0) == 0:
                    return 0.0  # unknown – sort after proven proxies
                return (s["requests"] - s.get("failures", 0)) / s["requests"]

            pool.sort(key=_success_rate, reverse=True)
            logger.info(
                "Proxy pool sorted by persisted success rate "
                "(%d proxies, %d with stats)",
                len(pool),
                sum(1 for p in pool if p.url in persisted_stats),
            )
        elif shuffle:
            random.shuffle(pool)

        self._proxies = pool
        self._cycle: Iterator[Proxy] = itertools.cycle(self._proxies)
        self._failed: set[str] = set()
        self._cooldown_seconds = cooldown_seconds
        # Per-proxy counters keyed by proxy URL
        self._requests: dict[str, int] = {p.url: 0 for p in self._proxies}
        self._failures: dict[str, int] = {p.url: 0 for p in self._proxies}

        # Seed counters from persisted stats.
        if persisted_stats:
            for p in self._proxies:
                s = persisted_stats.get(p.url)
                if s:
                    self._requests[p.url] = s.get("requests", 0)
                    self._failures[p.url] = s.get("failures", 0)

        # Timestamp of last failure per proxy URL (monotonic clock)
        self._fail_times: dict[str, float] = {}
        # Sticky proxy: last proxy that returned a successful response.
        # next_available() will try this proxy first before round-robin.
        self._preferred: Proxy | None = None
        # Optional path for auto-persisting stats on mark_success / mark_failed.
        self._stats_path: str | Path | None = None

    @property
    def size(self) -> int:
        return len(self._proxies)

    @property
    def proxies(self) -> list[Proxy]:
        return list(self._proxies)

    def next(self) -> Proxy:
        """Return the next proxy in the rotation (ignores cooldown)."""
        proxy = next(self._cycle)
        self._requests[proxy.url] = self._requests.get(proxy.url, 0) + 1
        return proxy

    def next_available(self) -> Proxy | None:
        """Return the next proxy that is **not** on cooldown.

        If a *preferred* (sticky) proxy has been set via :meth:`mark_success`
        and it is not on cooldown, it is returned first.  This keeps traffic
        flowing through a known-good proxy until it fails.

        Otherwise scans up to ``len(pool)`` candidates via round-robin.
        Returns ``None`` when every proxy is currently on cooldown.
        """
        import time

        now = time.monotonic()

        # Try the sticky / preferred proxy first.
        if self._preferred is not None:
            purl = self._preferred.url
            if purl not in self._fail_times or now - self._fail_times[purl] >= self._cooldown_seconds:
                self._requests[purl] = self._requests.get(purl, 0) + 1
                return self._preferred

        for _ in range(len(self._proxies)):
            proxy = next(self._cycle)
            purl = proxy.url
            # A proxy not in _fail_times was never failed – always available.
            if purl not in self._fail_times or now - self._fail_times[purl] >= self._cooldown_seconds:
                self._requests[purl] = self._requests.get(purl, 0) + 1
                return proxy
        return None

    def mark_failed(self, proxy: Proxy) -> None:
        """Record a proxy as failed and start its cooldown timer."""
        import time

        self._failed.add(proxy.url)
        self._failures[proxy.url] = self._failures.get(proxy.url, 0) + 1
        self._fail_times[proxy.url] = time.monotonic()
        # If the preferred proxy just failed, clear it so
        # next_available() falls back to round-robin.
        if self._preferred is not None and self._preferred.url == proxy.url:
            self._preferred = None
        logger.debug("Proxy marked as failed (cooldown %ds): %s", self._cooldown_seconds, proxy.url)
        self._auto_persist()

    def mark_success(self, proxy: Proxy) -> None:
        """Record *proxy* as the preferred (sticky) proxy.

        Subsequent calls to :meth:`next_available` will return this proxy
        first, as long as it is not on cooldown.  This keeps traffic
        flowing through a known-good proxy until it fails.
        """
        self._preferred = proxy
        logger.debug("Proxy marked as preferred (sticky): %s", proxy.url)
        self._auto_persist()

    def set_stats_path(self, proxy_path: str | Path) -> None:
        """Set the proxy file path so stats are auto-persisted on updates."""
        self._stats_path = proxy_path

    def persist_stats(self) -> None:
        """Write current request/failure counters to the stats sidecar file."""
        if self._stats_path is None:
            return
        data: dict[str, dict[str, int]] = {}
        for p in self._proxies:
            data[p.url] = {
                "requests": self._requests.get(p.url, 0),
                "failures": self._failures.get(p.url, 0),
            }
        try:
            save_proxy_stats(data, self._stats_path)
        except OSError as exc:
            logger.debug("Failed to persist proxy stats: %s", exc)

    def _auto_persist(self) -> None:
        """Persist stats to disk if a stats path is configured."""
        self.persist_stats()

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
                "successes": self._requests.get(p.url, 0) - self._failures.get(p.url, 0),
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
