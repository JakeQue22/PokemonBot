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
notifier:
  console: true
  discord_webhook_url: ""
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


@pytest.fixture
def web_app_proxy_dir(tmp_path):
    """Config that points to a proxies.txt that is actually a directory."""
    proxy_dir = tmp_path / "proxies.txt"
    proxy_dir.mkdir()
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        f"""\
proxies:
  file: {proxy_dir}
monitors:
  - name: test
    url: https://example.com
    site: generic
    keywords: []
    interval: 5.0
"""
    )
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
    async def test_api_clear_logs(self, web_app, aiohttp_client):
        client = await aiohttp_client(web_app)
        resp = await client.post("/api/logs/clear")
        assert resp.status == 200
        data = await resp.json()
        assert data["status"] == "cleared"

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

    @pytest.mark.asyncio
    async def test_discord_settings_roundtrip(self, web_app, aiohttp_client):
        client = await aiohttp_client(web_app)

        # GET defaults
        resp = await client.get("/api/discord-settings")
        assert resp.status == 200
        data = await resp.json()
        assert data["discord_webhook_url"] == ""

        # POST update
        resp = await client.post(
            "/api/discord-settings",
            json={"discord_webhook_url": "https://discord.com/api/webhooks/test/token"},
        )
        assert resp.status == 200

        # GET updated
        resp = await client.get("/api/discord-settings")
        data = await resp.json()
        assert data["discord_webhook_url"] == "https://discord.com/api/webhooks/test/token"

    @pytest.mark.asyncio
    async def test_test_discord_no_url(self, web_app, aiohttp_client):
        client = await aiohttp_client(web_app)
        resp = await client.post("/api/test-discord")
        assert resp.status == 400
        data = await resp.json()
        assert data["status"] == "error"

    @pytest.mark.asyncio
    async def test_api_config(self, web_app, aiohttp_client):
        client = await aiohttp_client(web_app)
        resp = await client.get("/api/config")
        assert resp.status == 200
        data = await resp.json()
        assert "monitors" in data
        assert isinstance(data["monitors"], list)
        assert data["monitors"][0]["name"] == "test"

    @pytest.mark.asyncio
    async def test_start_with_proxy_directory(self, web_app_proxy_dir, aiohttp_client):
        """Starting should not crash when proxies.txt is a directory (Docker volume mount)."""
        client = await aiohttp_client(web_app_proxy_dir)
        resp = await client.post("/api/start")
        assert resp.status == 200
        data = await resp.json()
        assert data["status"] == "started"
        # Cleanup
        await client.post("/api/stop")

    @pytest.mark.asyncio
    async def test_monitors_list(self, web_app, aiohttp_client):
        client = await aiohttp_client(web_app)
        resp = await client.get("/api/monitors")
        assert resp.status == 200
        data = await resp.json()
        assert len(data["monitors"]) == 1
        assert data["monitors"][0]["name"] == "test"

    @pytest.mark.asyncio
    async def test_monitors_add(self, web_app, aiohttp_client):
        client = await aiohttp_client(web_app)
        resp = await client.post(
            "/api/monitors",
            json={
                "name": "ETB Monitor",
                "url": "https://www.pokemoncenter.com/en-gb/category/elite-trainer-box",
                "site": "pokemoncenter",
                "keywords": "elite, trainer",
                "interval": 15,
            },
        )
        assert resp.status == 200
        data = await resp.json()
        assert data["status"] == "added"
        assert data["monitor"]["name"] == "ETB Monitor"

        # Verify it shows in the list
        resp = await client.get("/api/monitors")
        data = await resp.json()
        assert len(data["monitors"]) == 2

    @pytest.mark.asyncio
    async def test_monitors_add_missing_url(self, web_app, aiohttp_client):
        client = await aiohttp_client(web_app)
        resp = await client.post("/api/monitors", json={"name": "No URL"})
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_monitors_delete(self, web_app, aiohttp_client):
        client = await aiohttp_client(web_app)
        # Add a second monitor first
        await client.post(
            "/api/monitors",
            json={"url": "https://example.com/new", "name": "new"},
        )
        resp = await client.get("/api/monitors")
        data = await resp.json()
        assert len(data["monitors"]) == 2

        # Delete the second one (index 1)
        resp = await client.delete("/api/monitors/1")
        assert resp.status == 200
        data = await resp.json()
        assert data["status"] == "removed"
        assert data["monitor"]["name"] == "new"

        # Verify list
        resp = await client.get("/api/monitors")
        data = await resp.json()
        assert len(data["monitors"]) == 1

    @pytest.mark.asyncio
    async def test_monitors_delete_out_of_range(self, web_app, aiohttp_client):
        client = await aiohttp_client(web_app)
        resp = await client.delete("/api/monitors/99")
        assert resp.status == 404
