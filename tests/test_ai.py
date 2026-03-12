"""Tests for the AI module (xAI / Grok integration)."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pokemonbot.ai import _parse_response, analyse_page


class _FakeResponse:
    """Minimal fake for an aiohttp response used as an async context manager."""

    def __init__(self, status: int, json_data: dict | None = None, text: str = ""):
        self.status = status
        self._json = json_data
        self._text = text

    async def json(self):
        return self._json

    async def text(self):
        return self._text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


class _FakeSession:
    """Minimal fake for aiohttp.ClientSession."""

    def __init__(self, response: _FakeResponse, capture: dict | None = None):
        self._response = response
        self._capture = capture

    def post(self, url, *, json=None, headers=None):
        if self._capture is not None and json is not None:
            self._capture.update(json)
        return self._response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


class TestParseResponse:
    def test_in_stock(self):
        data = {"choices": [{"message": {"content": "IN_STOCK\nThe add to basket button is present."}}]}
        assert _parse_response(data) == "in_stock"

    def test_out_of_stock(self):
        data = {"choices": [{"message": {"content": "OUT_OF_STOCK\nProduct shows sold out."}}]}
        assert _parse_response(data) == "out_of_stock"

    def test_unknown(self):
        data = {"choices": [{"message": {"content": "UNKNOWN\nInsufficient data."}}]}
        assert _parse_response(data) is None

    def test_case_insensitive_first_line(self):
        data = {"choices": [{"message": {"content": "in_stock\nAvailable."}}]}
        assert _parse_response(data) == "in_stock"

    def test_missing_choices(self):
        assert _parse_response({}) is None

    def test_empty_choices(self):
        assert _parse_response({"choices": []}) is None

    def test_missing_message(self):
        assert _parse_response({"choices": [{}]}) is None

    def test_multiline_response(self):
        data = {"choices": [{"message": {"content": "OUT_OF_STOCK\nSold out badge is visible.\nNo add to cart."}}]}
        assert _parse_response(data) == "out_of_stock"


class TestAnalysePage:
    @pytest.mark.asyncio
    async def test_empty_api_key_skips(self):
        """When no API key is provided, analyse_page returns None immediately."""
        result = await analyse_page("", "<html></html>", "https://example.com")
        assert result is None

    @pytest.mark.asyncio
    async def test_successful_in_stock(self):
        """Simulate a successful xAI API call returning IN_STOCK."""
        resp = _FakeResponse(200, {"choices": [{"message": {"content": "IN_STOCK\nAdd to basket button found."}}]})
        session = _FakeSession(resp)

        with patch("pokemonbot.ai.aiohttp.ClientSession", return_value=session):
            result = await analyse_page("xai-test-key", "<html>Add to Basket</html>", "https://example.com")

        assert result == "in_stock"

    @pytest.mark.asyncio
    async def test_successful_out_of_stock(self):
        """Simulate a successful xAI API call returning OUT_OF_STOCK."""
        resp = _FakeResponse(200, {"choices": [{"message": {"content": "OUT_OF_STOCK\nSold out."}}]})
        session = _FakeSession(resp)

        with patch("pokemonbot.ai.aiohttp.ClientSession", return_value=session):
            result = await analyse_page("xai-test-key", "<html>Sold Out</html>", "https://example.com")

        assert result == "out_of_stock"

    @pytest.mark.asyncio
    async def test_api_error_returns_none(self):
        """When the API returns an error, analyse_page returns None."""
        resp = _FakeResponse(500, text="Internal Server Error")
        session = _FakeSession(resp)

        with patch("pokemonbot.ai.aiohttp.ClientSession", return_value=session):
            result = await analyse_page("xai-test-key", "<html></html>", "https://example.com")

        assert result is None

    @pytest.mark.asyncio
    async def test_network_error_returns_none(self):
        """When the network call fails, analyse_page returns None."""

        class _ErrorSession:
            def post(self, *a, **kw):
                raise Exception("Connection refused")

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

        with patch("pokemonbot.ai.aiohttp.ClientSession", return_value=_ErrorSession()):
            result = await analyse_page("xai-test-key", "<html></html>", "https://example.com")

        assert result is None

    @pytest.mark.asyncio
    async def test_body_truncation(self):
        """Long HTML bodies should be truncated before sending to the API."""
        long_body = "x" * 20_000
        captured: dict = {}
        resp = _FakeResponse(200, {"choices": [{"message": {"content": "UNKNOWN\nInsufficient data."}}]})
        session = _FakeSession(resp, capture=captured)

        with patch("pokemonbot.ai.aiohttp.ClientSession", return_value=session):
            await analyse_page("xai-test-key", long_body, "https://example.com")

        # The user message should contain truncated body
        user_msg = captured["messages"][1]["content"]
        assert "... [truncated]" in user_msg
        assert len(user_msg) < 20_000

    @pytest.mark.asyncio
    async def test_uses_grok3_model(self):
        """Ensure the API request uses the grok-3 model."""
        captured: dict = {}
        resp = _FakeResponse(200, {"choices": [{"message": {"content": "UNKNOWN\nNo data."}}]})
        session = _FakeSession(resp, capture=captured)

        with patch("pokemonbot.ai.aiohttp.ClientSession", return_value=session):
            await analyse_page("xai-test-key", "<html></html>", "https://example.com")

        assert captured["model"] == "grok-3"

