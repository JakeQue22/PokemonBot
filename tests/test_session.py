"""Tests for the session module."""

import asyncio

import pytest

from pokemonbot.proxy import Proxy
from pokemonbot.session import _get_domain_overrides, _random_user_agent, create_session


class TestRandomUserAgent:
    def test_returns_string(self):
        agents = ["Agent-A", "Agent-B"]
        result = _random_user_agent(agents)
        assert result in agents

    def test_empty_list(self):
        assert _random_user_agent([]) == ""


class TestCreateSession:
    @pytest.mark.asyncio
    async def test_creates_session_without_proxy(self):
        session = await create_session(timeout=5.0)
        assert not session.closed
        await session.close()

    @pytest.mark.asyncio
    async def test_creates_session_with_custom_headers(self):
        session = await create_session(
            headers={"X-Custom": "test"},
            timeout=5.0,
        )
        assert session.headers.get("X-Custom") == "test"
        await session.close()

    @pytest.mark.asyncio
    async def test_user_agent_set(self):
        session = await create_session(
            user_agents=["TestBot/1.0"],
            timeout=5.0,
        )
        assert session.headers.get("User-Agent") == "TestBot/1.0"
        await session.close()


class TestDomainOverrides:
    def test_pokemoncenter_headers(self):
        headers, cookies = _get_domain_overrides(
            "https://www.pokemoncenter.com/en-gb/category/elite-trainer-box"
        )
        assert "Accept-Language" in headers
        assert "en-GB" in headers["Accept-Language"]
        assert cookies.get("pokemon-website-language") == "en-gb"
        assert cookies.get("pokemon-website-country") == "gb"

    def test_unknown_domain_empty(self):
        headers, cookies = _get_domain_overrides("https://example.com/page")
        assert headers == {}
        assert cookies == {}
