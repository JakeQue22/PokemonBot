"""Proxy loading, validation, and round-robin cycling."""

from __future__ import annotations

import itertools
import logging
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

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


class ProxyPool:
    """Thread-safe, round-robin proxy pool with optional shuffle."""

    def __init__(self, proxies: list[Proxy], *, shuffle: bool = True) -> None:
        if not proxies:
            raise ValueError("Proxy pool requires at least one proxy")
        pool = list(proxies)
        if shuffle:
            random.shuffle(pool)
        self._proxies = pool
        self._cycle: Iterator[Proxy] = itertools.cycle(self._proxies)
        self._failed: set[str] = set()

    @property
    def size(self) -> int:
        return len(self._proxies)

    def next(self) -> Proxy:
        """Return the next proxy in the rotation."""
        return next(self._cycle)

    def mark_failed(self, proxy: Proxy) -> None:
        """Record a proxy as failed (for informational purposes)."""
        self._failed.add(proxy.url)
        logger.debug("Proxy marked as failed: %s", proxy.url)

    @property
    def failed_count(self) -> int:
        return len(self._failed)

    def reset_failures(self) -> None:
        self._failed.clear()
