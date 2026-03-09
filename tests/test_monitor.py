"""Tests for the monitor module."""

from pokemonbot.monitor import GenericMonitor, PokemonCenterMonitor, SmythsToysMonitor, get_monitor


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


class TestSmythsToysMonitor:
    def _make_response(self, body: str, status: int = 200) -> dict:
        return {"body": body, "status": status, "headers": {}, "url": ""}

    def test_detect_add_to_basket(self):
        m = SmythsToysMonitor()
        body = '<button class="add-to-basket">Add to Basket</button>'
        alert = m.parse(
            self._make_response(body),
            url="https://www.smythstoys.com/uk/en-gb/p/237414",
            keywords=[],
        )
        assert alert is not None
        assert alert.status == "in_stock"

    def test_detect_in_stock_schema(self):
        m = SmythsToysMonitor()
        body = '{"availability": "https://schema.org/InStock", "price": "44.99"}'
        alert = m.parse(
            self._make_response(body),
            url="https://www.smythstoys.com/uk/en-gb/p/237414",
            keywords=[],
        )
        assert alert is not None
        assert alert.status == "in_stock"
        assert alert.price == "£44.99"

    def test_detect_out_of_stock(self):
        m = SmythsToysMonitor()
        body = '<span class="availability">Out of Stock</span>'
        alert = m.parse(
            self._make_response(body),
            url="https://www.smythstoys.com/uk/en-gb/p/237414",
            keywords=[],
        )
        assert alert is None

    def test_403_returns_none(self):
        m = SmythsToysMonitor()
        alert = m.parse(
            self._make_response("blocked", status=403),
            url="https://www.smythstoys.com/uk/en-gb/p/237414",
            keywords=[],
        )
        assert alert is None

    def test_500_returns_none(self):
        m = SmythsToysMonitor()
        alert = m.parse(
            self._make_response("error", status=500),
            url="https://www.smythstoys.com/uk/en-gb/p/237414",
            keywords=[],
        )
        assert alert is None

    def test_category_page_matches_keyword(self):
        m = SmythsToysMonitor()
        body = (
            '<div class="product-card">'
            '<a href="/uk/en-gb/brand/pokemon/p/237414">Elite Trainer Box</a>'
            '<button class="addToBasket">Add to Basket</button>'
            '</div>'
            '<div class="product-card">'
            '<a href="/uk/en-gb/brand/pokemon/p/237415">Booster Pack</a>'
            '<button class="addToBasket">Add to Basket</button>'
            '</div>'
        )
        alert = m.parse(
            self._make_response(body),
            url="https://www.smythstoys.com/uk/en-gb/c/SM0601011202",
            keywords=["Trainer Box"],
        )
        assert alert is not None
        assert alert.status == "in_stock"
        assert "Trainer Box" in alert.product_name

    def test_category_page_no_keyword_match(self):
        m = SmythsToysMonitor()
        body = (
            '<div class="product-card">'
            '<a href="/uk/en-gb/brand/pokemon/p/237415">Booster Pack</a>'
            '<button class="addToBasket">Add to Basket</button>'
            '</div>'
        )
        alert = m.parse(
            self._make_response(body),
            url="https://www.smythstoys.com/uk/en-gb/c/SM0601011202",
            keywords=["Trainer Box"],
        )
        assert alert is None

    def test_category_page_no_stock(self):
        m = SmythsToysMonitor()
        body = (
            '<div class="product-card">'
            '<span>Elite Trainer Box</span>'
            '<span class="out-of-stock">Out of Stock</span>'
            '</div>'
        )
        alert = m.parse(
            self._make_response(body),
            url="https://www.smythstoys.com/uk/en-gb/c/SM0601011202",
            keywords=["Trainer Box"],
        )
        assert alert is None

    def test_store_stock_json_match(self):
        m = SmythsToysMonitor()
        body = (
            '{"stores":[{"storeName":"Liverpool ONE","stockLevelStatus":"green"},'
            '{"storeName":"Manchester","stockLevelStatus":"red"}]}'
        )
        alert = m.parse(
            self._make_response(body),
            url="https://www.smythstoys.com/uk/en-gb/p/237414",
            keywords=["Liverpool"],
        )
        assert alert is not None
        assert alert.status == "in_stock"
        assert "Liverpool" in alert.product_name

    def test_store_stock_json_no_match(self):
        m = SmythsToysMonitor()
        body = (
            '{"stores":[{"storeName":"Manchester","stockLevelStatus":"green"},'
            '{"storeName":"Leeds","stockLevelStatus":"red"}]}'
        )
        alert = m.parse(
            self._make_response(body),
            url="https://www.smythstoys.com/uk/en-gb/p/237414",
            keywords=["Liverpool"],
        )
        assert alert is None

    def test_keyword_filter_product_page(self):
        m = SmythsToysMonitor()
        body = '<title>Booster Pack</title><button>Add to Basket</button>'
        alert = m.parse(
            self._make_response(body),
            url="https://www.smythstoys.com/uk/en-gb/p/237414",
            keywords=["Trainer Box"],
        )
        assert alert is None

    def test_extract_title(self):
        m = SmythsToysMonitor()
        body = '<html><head><title>Stellar Crown ETB</title></head><body><button>Add to Basket</button></body></html>'
        alert = m.parse(
            self._make_response(body),
            url="https://www.smythstoys.com/uk/en-gb/p/237414",
            keywords=[],
        )
        assert alert is not None
        assert alert.product_name == "Stellar Crown ETB"

    def test_category_absolute_url(self):
        m = SmythsToysMonitor()
        body = (
            '<div class="product-card">'
            '<a href="/uk/en-gb/brand/pokemon/p/237414">Trainer Box Deluxe</a>'
            '<button class="addToBasket">Add to Basket</button>'
            '</div>'
        )
        alert = m.parse(
            self._make_response(body),
            url="https://www.smythstoys.com/uk/en-gb/c/SM0601011202",
            keywords=["Trainer Box"],
        )
        assert alert is not None
        assert alert.url.startswith("https://www.smythstoys.com/")


class TestGetMonitor:
    def test_pokemoncenter(self):
        m = get_monitor("pokemoncenter")
        assert isinstance(m, PokemonCenterMonitor)

    def test_smythstoys(self):
        m = get_monitor("smythstoys")
        assert isinstance(m, SmythsToysMonitor)

    def test_generic(self):
        m = get_monitor("generic")
        assert isinstance(m, GenericMonitor)

    def test_unknown_falls_back_to_generic(self):
        m = get_monitor("totally_unknown_site")
        assert isinstance(m, GenericMonitor)
