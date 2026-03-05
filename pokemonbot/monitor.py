"""Product availability monitors for various sites."""

from __future__ import annotations

import abc
import logging
import re
from typing import Any

from pokemonbot.notifier import Alert

logger = logging.getLogger(__name__)


class BaseMonitor(abc.ABC):
    """Abstract base class for site monitors."""

    site_name: str = "generic"

    @abc.abstractmethod
    def parse(self, response: dict[str, Any], *, url: str, keywords: list[str]) -> Alert | None:
        """Parse an HTTP response and return an ``Alert`` if a notable state is detected."""

    def _keyword_match(self, text: str, keywords: list[str]) -> bool:
        if not keywords:
            return True
        lower = text.lower()
        return any(kw.lower() in lower for kw in keywords)


class PokemonCenterMonitor(BaseMonitor):
    """Monitor for pokemoncenter.com product pages and listings."""

    site_name = "pokemoncenter"

    # Common indicators found in Pokemon Center product pages
    _ADD_TO_CART_PATTERNS = [
        re.compile(r'"availability"\s*:\s*"InStock"', re.IGNORECASE),
        re.compile(r'"availability"\s*:\s*"https?://schema\.org/InStock"', re.IGNORECASE),
        re.compile(r'add[_-]?to[_-]?cart', re.IGNORECASE),
        re.compile(r'addToCart', re.IGNORECASE),
    ]
    _OUT_OF_STOCK_PATTERNS = [
        re.compile(r'"availability"\s*:\s*"OutOfStock"', re.IGNORECASE),
        re.compile(r'"availability"\s*:\s*"https?://schema\.org/OutOfStock"', re.IGNORECASE),
        re.compile(r'sold\s*out', re.IGNORECASE),
        re.compile(r'out\s*of\s*stock', re.IGNORECASE),
        re.compile(r'currently\s*unavailable', re.IGNORECASE),
    ]
    _QUEUE_PATTERNS = [
        re.compile(r'queue-it', re.IGNORECASE),
        re.compile(r'waiting\s*room', re.IGNORECASE),
        re.compile(r'you\s*are\s*in\s*(?:the\s*)?queue', re.IGNORECASE),
        re.compile(r'queue\.it', re.IGNORECASE),
    ]
    _PRICE_PATTERN = re.compile(r'"price"\s*:\s*"?([\d.]+)"?')

    def parse(self, response: dict[str, Any], *, url: str, keywords: list[str]) -> Alert | None:
        body: str = response.get("body", "")
        status_code: int = response.get("status", 0)

        if status_code == 403:
            logger.info("Access denied (403) for %s – possible bot protection", url)
            return None

        if status_code >= 500:
            logger.warning("Server error (%d) for %s", status_code, url)
            return None

        if not self._keyword_match(body, keywords):
            return None

        # Detect queue page
        for pat in self._QUEUE_PATTERNS:
            if pat.search(body):
                logger.info("Queue detected for %s", url)
                return Alert(
                    product_name=self._extract_title(body) or url,
                    url=url,
                    status="queue_active",
                    site=self.site_name,
                    price=self._extract_price(body),
                )

        # Detect in-stock
        for pat in self._ADD_TO_CART_PATTERNS:
            if pat.search(body):
                return Alert(
                    product_name=self._extract_title(body) or url,
                    url=url,
                    status="in_stock",
                    site=self.site_name,
                    price=self._extract_price(body),
                )

        # Detect out-of-stock (informational only)
        for pat in self._OUT_OF_STOCK_PATTERNS:
            if pat.search(body):
                logger.debug("Product is out of stock at %s", url)
                return None

        logger.debug("No definitive stock status found for %s", url)
        return None

    @staticmethod
    def _extract_title(body: str) -> str:
        m = re.search(r"<title>([^<]+)</title>", body, re.IGNORECASE)
        return m.group(1).strip() if m else ""

    def _extract_price(self, body: str) -> str:
        m = self._PRICE_PATTERN.search(body)
        return f"${m.group(1)}" if m else ""


class GenericMonitor(BaseMonitor):
    """Keyword-based monitor suitable for any website."""

    site_name = "generic"

    _STOCK_INDICATORS = [
        re.compile(r'add[_\- ]?to[_\- ]?cart', re.IGNORECASE),
        re.compile(r'"availability"\s*:\s*"[^"]*InStock"', re.IGNORECASE),
        re.compile(r'in\s*stock', re.IGNORECASE),
        re.compile(r'buy\s*now', re.IGNORECASE),
    ]

    def parse(self, response: dict[str, Any], *, url: str, keywords: list[str]) -> Alert | None:
        body: str = response.get("body", "")
        status_code: int = response.get("status", 0)

        if status_code >= 400:
            logger.info("HTTP %d for %s", status_code, url)
            return None

        if not self._keyword_match(body, keywords):
            return None

        for pat in self._STOCK_INDICATORS:
            if pat.search(body):
                title = self._extract_title(body) or url
                return Alert(
                    product_name=title,
                    url=url,
                    status="in_stock",
                    site=self.site_name,
                )

        return None

    @staticmethod
    def _extract_title(body: str) -> str:
        m = re.search(r"<title>([^<]+)</title>", body, re.IGNORECASE)
        return m.group(1).strip() if m else ""


# Registry of available monitors keyed by site name.
MONITOR_REGISTRY: dict[str, type[BaseMonitor]] = {
    "pokemoncenter": PokemonCenterMonitor,
    "generic": GenericMonitor,
}


def get_monitor(site: str) -> BaseMonitor:
    """Return an instantiated monitor for the given site name."""
    cls = MONITOR_REGISTRY.get(site, GenericMonitor)
    return cls()
