"""Tests for the monitor module."""

from pokemonbot.monitor import GenericMonitor, PokemonCenterMonitor, get_monitor


class TestPokemonCenterMonitor:
    def _make_response(self, body: str, status: int = 200) -> dict:
        return {"body": body, "status": status, "headers": {}, "url": ""}

    def test_detect_in_stock_schema(self):
        m = PokemonCenterMonitor()
        body = '<script type="application/ld+json">{"availability": "InStock"}</script>'
        alert = m.parse(self._make_response(body), url="https://example.com", keywords=[])
        assert alert is not None
        assert alert.status == "in_stock"

    def test_detect_in_stock_schema_url(self):
        m = PokemonCenterMonitor()
        body = '{"availability": "https://schema.org/InStock", "price": "49.99"}'
        alert = m.parse(self._make_response(body), url="https://example.com", keywords=[])
        assert alert is not None
        assert alert.status == "in_stock"
        assert alert.price == "$49.99"

    def test_detect_add_to_cart(self):
        m = PokemonCenterMonitor()
        body = '<button class="add-to-cart">Add to Cart</button>'
        alert = m.parse(self._make_response(body), url="https://example.com", keywords=[])
        assert alert is not None
        assert alert.status == "in_stock"

    def test_detect_out_of_stock(self):
        m = PokemonCenterMonitor()
        body = '<span class="stock-label">Sold Out</span>'
        alert = m.parse(self._make_response(body), url="https://example.com", keywords=[])
        assert alert is None

    def test_detect_queue(self):
        m = PokemonCenterMonitor()
        body = '<div>You are in the queue. Please wait.</div>'
        alert = m.parse(self._make_response(body), url="https://example.com", keywords=[])
        assert alert is not None
        assert alert.status == "queue_active"

    def test_detect_queue_it(self):
        m = PokemonCenterMonitor()
        body = '<script src="https://queue-it.net/script.js"></script>'
        alert = m.parse(self._make_response(body), url="https://example.com", keywords=[])
        assert alert is not None
        assert alert.status == "queue_active"

    def test_keyword_filter_match(self):
        m = PokemonCenterMonitor()
        body = '<title>Ascending Heroes Booster</title><button class="add-to-cart">Buy</button>'
        alert = m.parse(
            self._make_response(body),
            url="https://example.com",
            keywords=["Ascending Heroes"],
        )
        assert alert is not None

    def test_keyword_filter_no_match(self):
        m = PokemonCenterMonitor()
        body = '<title>Some Other Product</title><button class="add-to-cart">Buy</button>'
        alert = m.parse(
            self._make_response(body),
            url="https://example.com",
            keywords=["Ascending Heroes"],
        )
        assert alert is None

    def test_403_returns_none(self):
        m = PokemonCenterMonitor()
        alert = m.parse(
            self._make_response("blocked", status=403),
            url="https://example.com",
            keywords=[],
        )
        assert alert is None

    def test_403_logs_as_warning(self, caplog):
        """403 should be logged at WARNING level, not INFO, so it appears red."""
        import logging
        m = PokemonCenterMonitor()
        with caplog.at_level(logging.WARNING, logger="pokemonbot.monitor"):
            m.parse(
                self._make_response("blocked", status=403),
                url="https://example.com",
                keywords=[],
            )
        assert any("Access denied (403)" in r.message and r.levelno == logging.WARNING for r in caplog.records)

    def test_500_returns_none(self):
        m = PokemonCenterMonitor()
        alert = m.parse(
            self._make_response("error", status=500),
            url="https://example.com",
            keywords=[],
        )
        assert alert is None

    def test_extract_title(self):
        m = PokemonCenterMonitor()
        body = '<html><head><title>Cool Product</title></head><body>{"availability":"InStock"}</body></html>'
        alert = m.parse(self._make_response(body), url="https://example.com", keywords=[])
        assert alert is not None
        assert alert.product_name == "Cool Product"


class TestGenericMonitor:
    def _make_response(self, body: str, status: int = 200) -> dict:
        return {"body": body, "status": status, "headers": {}, "url": ""}

    def test_detect_add_to_cart(self):
        m = GenericMonitor()
        body = '<button>Add to Cart</button>'
        alert = m.parse(self._make_response(body), url="https://example.com", keywords=[])
        assert alert is not None
        assert alert.status == "in_stock"

    def test_detect_buy_now(self):
        m = GenericMonitor()
        body = '<a href="#">Buy Now</a>'
        alert = m.parse(self._make_response(body), url="https://example.com", keywords=[])
        assert alert is not None

    def test_no_signal(self):
        m = GenericMonitor()
        body = '<p>This page has nothing special</p>'
        alert = m.parse(self._make_response(body), url="https://example.com", keywords=[])
        assert alert is None

    def test_http_error(self):
        m = GenericMonitor()
        alert = m.parse(
            self._make_response("not found", status=404),
            url="https://example.com",
            keywords=[],
        )
        assert alert is None


class TestGetMonitor:
    def test_pokemoncenter(self):
        m = get_monitor("pokemoncenter")
        assert isinstance(m, PokemonCenterMonitor)

    def test_generic(self):
        m = get_monitor("generic")
        assert isinstance(m, GenericMonitor)

    def test_unknown_falls_back_to_generic(self):
        m = get_monitor("totally_unknown_site")
        assert isinstance(m, GenericMonitor)
