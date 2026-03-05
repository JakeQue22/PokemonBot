"""Tests for the web dashboard module."""

import json

import pytest
from aiohttp.test_utils import AioHTTPTestCase, TestClient, TestServer

from pokemonbot.web import create_web_app


@pytest.fixture
def web_app(tmp_path):
    """Create a web app with a minimal config file."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        """\
monitors:
  - name: test
    url: https://example.com
    site: generic
    keywords: []
    interval: 5.0
email:
  enabled: false
  smtp_host: ""
  smtp_port: 465
  username: ""
  password: ""
  use_ssl: true
  from_address: ""
  to_addresses: []
"""
    )
    return create_web_app(config_path=str(cfg))


@pytest.fixture
def web_app_no_monitors(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("monitors: []\n")
    return create_web_app(config_path=str(cfg))


class TestDashboard:
    @pytest.mark.asyncio
    async def test_index_returns_html(self, web_app, aiohttp_client):
        client = await aiohttp_client(web_app)
        resp = await client.get("/")
        assert resp.status == 200
        text = await resp.text()
        assert "PokemonBot" in text

    @pytest.mark.asyncio
    async def test_api_status(self, web_app, aiohttp_client):
        client = await aiohttp_client(web_app)
        resp = await client.get("/api/status")
        assert resp.status == 200
        data = await resp.json()
        assert data["running"] is False
        assert "version" in data

    @pytest.mark.asyncio
    async def test_api_start_and_stop(self, web_app, aiohttp_client):
        client = await aiohttp_client(web_app)

        # Start
        resp = await client.post("/api/start")
        assert resp.status == 200
        data = await resp.json()
        assert data["status"] == "started"

        # Status should show running
        resp = await client.get("/api/status")
        data = await resp.json()
        assert data["running"] is True

        # Stop
        resp = await client.post("/api/stop")
        assert resp.status == 200
        data = await resp.json()
        assert data["status"] == "stopped"

    @pytest.mark.asyncio
    async def test_api_start_already_running(self, web_app, aiohttp_client):
        client = await aiohttp_client(web_app)
        await client.post("/api/start")
        resp = await client.post("/api/start")
        assert resp.status == 409
        # Cleanup
        await client.post("/api/stop")

    @pytest.mark.asyncio
    async def test_api_stop_not_running(self, web_app, aiohttp_client):
        client = await aiohttp_client(web_app)
        resp = await client.post("/api/stop")
        assert resp.status == 409

    @pytest.mark.asyncio
    async def test_api_start_no_monitors(self, web_app_no_monitors, aiohttp_client):
        client = await aiohttp_client(web_app_no_monitors)
        resp = await client.post("/api/start")
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_api_logs(self, web_app, aiohttp_client):
        client = await aiohttp_client(web_app)
        resp = await client.get("/api/logs")
        assert resp.status == 200
        data = await resp.json()
        assert "lines" in data
        assert isinstance(data["lines"], list)

    @pytest.mark.asyncio
    async def test_email_settings_roundtrip(self, web_app, aiohttp_client):
        client = await aiohttp_client(web_app)

        # GET defaults
        resp = await client.get("/api/email-settings")
        assert resp.status == 200
        data = await resp.json()
        assert data["enabled"] is False

        # POST update
        resp = await client.post(
            "/api/email-settings",
            json={
                "enabled": True,
                "smtp_host": "smtp.test.com",
                "smtp_port": 587,
                "username": "user",
                "password": "secret",
                "use_ssl": False,
                "from_address": "bot@test.com",
                "to_addresses": "a@b.com, c@d.com",
            },
        )
        assert resp.status == 200

        # GET updated
        resp = await client.get("/api/email-settings")
        data = await resp.json()
        assert data["enabled"] is True
        assert data["smtp_host"] == "smtp.test.com"
        assert data["smtp_port"] == 587
        assert data["from_address"] == "bot@test.com"
        assert data["to_addresses"] == ["a@b.com", "c@d.com"]
        # Password should be masked in GET
        assert data["password"] == "••••••••"

    @pytest.mark.asyncio
    async def test_test_email_no_host(self, web_app, aiohttp_client):
        client = await aiohttp_client(web_app)
        resp = await client.post("/api/test-email")
        assert resp.status == 400
        data = await resp.json()
        assert data["status"] == "error"
