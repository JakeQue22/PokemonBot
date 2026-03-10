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

    def describe_status(self, response: dict[str, Any], *, url: str) -> str:
        """Return a short human-readable stock status for logging.

        Called when ``parse()`` returns ``None`` to give the user a
        meaningful status line (e.g. *Out of stock*) instead of the
        generic *no change*.
        """
        return "no change"

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

    def describe_status(self, response: dict[str, Any], *, url: str) -> str:
        body: str = response.get("body", "")
        status_code: int = response.get("status", 0)
        if status_code == 403:
            return "Access denied (bot protection)"
        if status_code >= 500:
            return f"Server error ({status_code})"
        for pat in self._QUEUE_PATTERNS:
            if pat.search(body):
                return "Queue active"
        for pat in self._OUT_OF_STOCK_PATTERNS:
            if pat.search(body):
                return "Out of stock"
        for pat in self._ADD_TO_CART_PATTERNS:
            if pat.search(body):
                return "In stock"
        return "No stock data found"

    def parse(self, response: dict[str, Any], *, url: str, keywords: list[str]) -> Alert | None:
        body: str = response.get("body", "")
        status_code: int = response.get("status", 0)

        if status_code == 403:
            logger.warning("Access denied (403) for %s – possible bot protection", url)
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


class SmythsToysMonitor(BaseMonitor):
    """Monitor for smythstoys.com product and category pages.

    Supports two URL patterns:
    - **Product pages** (``/p/<id>``): checks for add-to-basket / in-stock
      indicators and can filter store availability by keyword (e.g.
      ``["Liverpool"]``).
    - **Category pages** (``/c/<id>``): scans product listings and alerts
      when any in-stock item matches the configured keywords (e.g.
      ``["Trainer Box"]``).
    """

    site_name = "smythstoys"

    _ADD_TO_BASKET_PATTERNS = [
        re.compile(r'add[_\-\s]?to[_\-\s]?basket', re.IGNORECASE),
        re.compile(r'addToBasket', re.IGNORECASE),
        re.compile(r'"availability"\s*:\s*"[^"]*InStock"', re.IGNORECASE),
        re.compile(r'"inStock"\s*:\s*true', re.IGNORECASE),
    ]
    _OUT_OF_STOCK_PATTERNS = [
        re.compile(r'out[_\-\s]?of[_\-\s]?stock', re.IGNORECASE),
        re.compile(r'currently[_\-\s]?unavailable', re.IGNORECASE),
        re.compile(r'"availability"\s*:\s*"[^"]*OutOfStock"', re.IGNORECASE),
        re.compile(r'"inStock"\s*:\s*false', re.IGNORECASE),
        re.compile(r'sold\s*out', re.IGNORECASE),
    ]
    _PRICE_PATTERN = re.compile(r'"price"\s*:\s*"?([\d.]+)"?')

    # Category page: individual product blocks.  SmythsToys wraps each item in
    # an element whose class contains "product" – we capture the whole block up
    # to the next similar element so we can inspect title + availability
    # together.
    _PRODUCT_BLOCK = re.compile(
        r'<[^>]+class="[^"]*\bproduct[^"]*"[^>]*>.*?(?=<[^>]+class="[^"]*\bproduct[^"]*"|$)',
        re.IGNORECASE | re.DOTALL,
    )
    _TITLE_IN_BLOCK = re.compile(r'<(?:a|h\d|span)[^>]*>([^<]{3,})</(?:a|h\d|span)>', re.IGNORECASE)
    _LINK_IN_BLOCK = re.compile(r'href="([^"]*?/p/[^"]*)"', re.IGNORECASE)

    # Store-stock JSON fragments (returned by SmythsToys stock-check XHR).
    _STORE_ENTRY = re.compile(
        r'\{[^}]*"(?:store[Nn]ame|displayName)"\s*:\s*"(?P<store>[^"]+)"[^}]*'
        r'"(?:stock[Ll]evel(?:Status)?|availableStock)"\s*:\s*"?(?P<stock>[^",}]+)',
        re.DOTALL,
    )

    def describe_status(self, response: dict[str, Any], *, url: str) -> str:
        body: str = response.get("body", "")
        status_code: int = response.get("status", 0)
        if status_code == 403:
            return "Access denied (bot protection)"
        if status_code >= 500:
            return f"Server error ({status_code})"
        for pat in self._OUT_OF_STOCK_PATTERNS:
            if pat.search(body):
                return "Out of stock"
        for pat in self._ADD_TO_BASKET_PATTERNS:
            if pat.search(body):
                return "In stock"
        return "No stock data found"

    def parse(self, response: dict[str, Any], *, url: str, keywords: list[str]) -> Alert | None:
        body: str = response.get("body", "")
        status_code: int = response.get("status", 0)

        if status_code == 403:
            logger.warning("Access denied (403) for %s – possible bot protection", url)
            return None
        if status_code >= 500:
            logger.warning("Server error (%d) for %s", status_code, url)
            return None

        # Decide parsing strategy based on URL shape.
        if "/c/" in url:
            return self._parse_category(body, url=url, keywords=keywords)
        return self._parse_product(body, url=url, keywords=keywords)

    # -- Product page --------------------------------------------------------

    def _parse_product(self, body: str, *, url: str, keywords: list[str]) -> Alert | None:
        # If the response looks like a store-stock JSON payload, delegate.
        store_alert = self._parse_store_stock(body, url=url, keywords=keywords)
        if store_alert is not None:
            return store_alert

        if not self._keyword_match(body, keywords):
            return None

        for pat in self._ADD_TO_BASKET_PATTERNS:
            if pat.search(body):
                return Alert(
                    product_name=self._extract_title(body) or url,
                    url=url,
                    status="in_stock",
                    site=self.site_name,
                    price=self._extract_price(body),
                )

        for pat in self._OUT_OF_STOCK_PATTERNS:
            if pat.search(body):
                logger.debug("Product out of stock at %s", url)
                return None

        logger.debug("No definitive stock status for %s", url)
        return None

    # -- Category page -------------------------------------------------------

    def _parse_category(self, body: str, *, url: str, keywords: list[str]) -> Alert | None:
        blocks = self._PRODUCT_BLOCK.findall(body)
        if not blocks:
            # Fallback: treat the whole page as a single product-like page.
            return self._parse_product(body, url=url, keywords=keywords)

        for block in blocks:
            title_m = self._TITLE_IN_BLOCK.search(block)
            title = title_m.group(1).strip() if title_m else ""
            if not self._keyword_match(title, keywords):
                continue

            # Check if *this* block signals stock.
            in_stock = any(pat.search(block) for pat in self._ADD_TO_BASKET_PATTERNS)
            if not in_stock:
                continue

            link_m = self._LINK_IN_BLOCK.search(block)
            product_url = link_m.group(1) if link_m else url
            if product_url.startswith("/"):
                # Make absolute using the base from the original URL.
                from urllib.parse import urlparse
                parts = urlparse(url)
                product_url = f"{parts.scheme}://{parts.netloc}{product_url}"

            return Alert(
                product_name=title or url,
                url=product_url,
                status="in_stock",
                site=self.site_name,
                price=self._extract_price(block),
            )

        logger.debug("No in-stock keyword-matched items found on category page %s", url)
        return None

    # -- Store stock JSON ----------------------------------------------------

    def _parse_store_stock(self, body: str, *, url: str, keywords: list[str]) -> Alert | None:
        """Parse a store-stock JSON/HTML payload and filter by store name keywords."""
        entries = self._STORE_ENTRY.findall(body)
        if not entries:
            return None  # not a store-stock response

        for store_name, stock_level in entries:
            if not self._keyword_match(store_name, keywords):
                continue
            stock_lower = stock_level.strip().lower()
            if stock_lower in ("green", "instock", "available", "true") or (stock_lower.isdigit() and int(stock_lower) > 0):
                return Alert(
                    product_name=f"{self._extract_title(body) or url} @ {store_name}",
                    url=url,
                    status="in_stock",
                    site=self.site_name,
                )

        logger.debug("No matching in-stock stores for %s", url)
        return None

    @staticmethod
    def _extract_title(body: str) -> str:
        m = re.search(r"<title>([^<]+)</title>", body, re.IGNORECASE)
        return m.group(1).strip() if m else ""

    def _extract_price(self, body: str) -> str:
        m = self._PRICE_PATTERN.search(body)
        return f"£{m.group(1)}" if m else ""


# Registry of available monitors keyed by site name.
MONITOR_REGISTRY: dict[str, type[BaseMonitor]] = {
    "pokemoncenter": PokemonCenterMonitor,
    "smythstoys": SmythsToysMonitor,
    "generic": GenericMonitor,
}


def get_monitor(site: str) -> BaseMonitor:
    """Return an instantiated monitor for the given site name."""
    cls = MONITOR_REGISTRY.get(site, GenericMonitor)
    return cls()
