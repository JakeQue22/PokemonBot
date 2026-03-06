"""Tests for the web dashboard module."""

import json
from unittest.mock import AsyncMock, patch

import pytest
from aiohttp.test_utils import AioHTTPTestCase, TestClient, TestServer

from pokemonbot.proxy import Proxy
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

    # ---- Monitor update (edit) tests ----

    @pytest.mark.asyncio
    async def test_monitors_update(self, web_app, aiohttp_client):
        client = await aiohttp_client(web_app)
        resp = await client.put(
            "/api/monitors/0",
            json={
                "name": "Updated Name",
                "url": "https://example.com/updated",
                "keywords": "kw1, kw2",
                "interval": 20,
            },
        )
        assert resp.status == 200
        data = await resp.json()
        assert data["status"] == "updated"
        assert data["monitor"]["name"] == "Updated Name"
        assert data["monitor"]["url"] == "https://example.com/updated"
        assert data["monitor"]["keywords"] == ["kw1", "kw2"]
        assert data["monitor"]["interval"] == 20

    @pytest.mark.asyncio
    async def test_monitors_update_out_of_range(self, web_app, aiohttp_client):
        client = await aiohttp_client(web_app)
        resp = await client.put("/api/monitors/99", json={"name": "x"})
        assert resp.status == 404

    # ---- Proxy CRUD tests ----

    @pytest.mark.asyncio
    async def test_proxies_list_empty(self, web_app, aiohttp_client):
        client = await aiohttp_client(web_app)
        resp = await client.get("/api/proxies")
        assert resp.status == 200
        data = await resp.json()
        assert isinstance(data["proxies"], list)

    @pytest.mark.asyncio
    async def test_proxies_add_and_list(self, web_app, aiohttp_client):
        client = await aiohttp_client(web_app)
        resp = await client.post(
            "/api/proxies",
            json={"proxy": "http://1.2.3.4:8080"},
        )
        assert resp.status == 200
        data = await resp.json()
        assert data["status"] == "added"

        resp = await client.get("/api/proxies")
        data = await resp.json()
        assert len(data["proxies"]) >= 1
        assert any(p["host"] == "1.2.3.4" for p in data["proxies"])

    @pytest.mark.asyncio
    async def test_proxies_add_invalid(self, web_app, aiohttp_client):
        client = await aiohttp_client(web_app)
        resp = await client.post(
            "/api/proxies",
            json={"proxy": "not_valid_at_all"},
        )
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_proxies_add_empty(self, web_app, aiohttp_client):
        client = await aiohttp_client(web_app)
        resp = await client.post("/api/proxies", json={"proxy": ""})
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_proxies_delete(self, web_app, aiohttp_client):
        client = await aiohttp_client(web_app)
        # Add first
        await client.post("/api/proxies", json={"proxy": "http://10.0.0.1:3128"})
        # Delete
        resp = await client.delete("/api/proxies/0")
        assert resp.status == 200
        data = await resp.json()
        assert data["status"] == "removed"

    @pytest.mark.asyncio
    async def test_proxies_delete_out_of_range(self, web_app, aiohttp_client):
        client = await aiohttp_client(web_app)
        resp = await client.delete("/api/proxies/99")
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_proxies_bulk_update(self, web_app, aiohttp_client):
        client = await aiohttp_client(web_app)
        resp = await client.put(
            "/api/proxies",
            json={"proxies": [
                "http://1.1.1.1:8080",
                "socks5://2.2.2.2:1080",
                "invalid_line",
            ]},
        )
        assert resp.status == 200
        data = await resp.json()
        assert data["status"] == "updated"
        assert data["count"] == 2  # only 2 valid
        assert len(data["errors"]) == 1  # 1 invalid

    # ---- General settings tests ----

    @pytest.mark.asyncio
    async def test_general_settings_get(self, web_app, aiohttp_client):
        client = await aiohttp_client(web_app)
        resp = await client.get("/api/general-settings")
        assert resp.status == 200
        data = await resp.json()
        assert "portal_name" in data
        assert "concurrency" in data
        assert "request_timeout" in data

    @pytest.mark.asyncio
    async def test_general_settings_roundtrip(self, web_app, aiohttp_client):
        client = await aiohttp_client(web_app)
        resp = await client.post(
            "/api/general-settings",
            json={"portal_name": "MyBot", "concurrency": 5, "request_timeout": 15},
        )
        assert resp.status == 200

        resp = await client.get("/api/general-settings")
        data = await resp.json()
        assert data["portal_name"] == "MyBot"
        assert data["concurrency"] == 5
        assert data["request_timeout"] == 15

    @pytest.mark.asyncio
    async def test_portal_name_in_html(self, web_app, aiohttp_client):
        client = await aiohttp_client(web_app)
        # Update portal name
        await client.post(
            "/api/general-settings",
            json={"portal_name": "TestPortal"},
        )
        resp = await client.get("/")
        text = await resp.text()
        assert "TestPortal" in text

    # ---- Fetch public proxies tests ----

    @pytest.mark.asyncio
    async def test_fetch_public_proxies_save_error_returns_json(
        self, web_app, aiohttp_client
    ):
        """When file I/O fails after fetching, the endpoint must still return JSON."""
        client = await aiohttp_client(web_app)
        fake_proxies = [
            Proxy(protocol="http", host="1.2.3.4", port=8080),
        ]
        with patch(
            "pokemonbot.web.fetch_public_proxies",
            new_callable=AsyncMock,
            return_value=fake_proxies,
        ), patch(
            "pokemonbot.web.ensure_proxy_file",
            side_effect=PermissionError("Permission denied: proxies.txt"),
        ):
            resp = await client.post("/api/proxies/fetch-public")
            assert resp.status == 500
            data = await resp.json()
            assert "error" in data
            assert "could not be saved" in data["error"]

    @pytest.mark.asyncio
    async def test_fetch_public_proxies_success_returns_json(
        self, web_app, aiohttp_client
    ):
        """When fetching and saving succeed, the endpoint returns valid JSON."""
        client = await aiohttp_client(web_app)
        fake_proxies = [
            Proxy(protocol="http", host="1.2.3.4", port=8080),
            Proxy(protocol="socks5", host="5.6.7.8", port=1080),
        ]
        with patch(
            "pokemonbot.web.fetch_public_proxies",
            new_callable=AsyncMock,
            return_value=fake_proxies,
        ):
            resp = await client.post("/api/proxies/fetch-public")
            assert resp.status == 200
            data = await resp.json()
            assert data["status"] == "ok"
            assert data["fetched"] == 2
            assert data["total"] >= 2

    @pytest.mark.asyncio
    async def test_monitor_add_persists_to_disk(self, web_app, aiohttp_client):
        """Adding a monitor should write the change to config.yaml."""
        client = await aiohttp_client(web_app)
        config_path = web_app["state"].config_path

        resp = await client.post(
            "/api/monitors",
            json={"name": "Persisted", "url": "https://persist.example.com", "site": "generic", "interval": 7},
        )
        assert resp.status == 200

        from pokemonbot.config import load_config
        reloaded = load_config(config_path)
        names = [m.name for m in reloaded.monitors]
        assert "Persisted" in names

    @pytest.mark.asyncio
    async def test_monitor_delete_persists_to_disk(self, web_app, aiohttp_client):
        """Deleting a monitor should write the change to config.yaml."""
        client = await aiohttp_client(web_app)
        config_path = web_app["state"].config_path

        resp = await client.delete("/api/monitors/0")
        assert resp.status == 200

        from pokemonbot.config import load_config
        reloaded = load_config(config_path)
        assert len(reloaded.monitors) == 0

    @pytest.mark.asyncio
    async def test_general_settings_persist_to_disk(self, web_app, aiohttp_client):
        """Changing general settings should persist them to config.yaml."""
        client = await aiohttp_client(web_app)
        config_path = web_app["state"].config_path

        resp = await client.post(
            "/api/general-settings",
            json={"portal_name": "TestBot", "concurrency": 42},
        )
        assert resp.status == 200

        from pokemonbot.config import load_config
        reloaded = load_config(config_path)
        assert reloaded.portal_name == "TestBot"
        assert reloaded.concurrency == 42

    @pytest.mark.asyncio
    async def test_discord_settings_persist_to_disk(self, web_app, aiohttp_client):
        """Changing discord settings should persist them to config.yaml."""
        client = await aiohttp_client(web_app)
        config_path = web_app["state"].config_path

        resp = await client.post(
            "/api/discord-settings",
            json={"discord_webhook_url": "https://discord.com/api/webhooks/123/abc"},
        )
        assert resp.status == 200

        from pokemonbot.config import load_config
        reloaded = load_config(config_path)
        assert reloaded.notifier.discord_webhook_url == "https://discord.com/api/webhooks/123/abc"

    @pytest.mark.asyncio
    async def test_email_settings_persist_to_disk(self, web_app, aiohttp_client):
        """Changing email settings should persist them to config.yaml."""
        client = await aiohttp_client(web_app)
        config_path = web_app["state"].config_path

        resp = await client.post(
            "/api/email-settings",
            json={"enabled": True, "smtp_host": "smtp.test.com", "smtp_port": 587},
        )
        assert resp.status == 200

        from pokemonbot.config import load_config
        reloaded = load_config(config_path)
        assert reloaded.email.enabled is True
        assert reloaded.email.smtp_host == "smtp.test.com"
        assert reloaded.email.smtp_port == 587

    @pytest.mark.asyncio
    async def test_proxies_list_includes_successes(self, web_app, aiohttp_client):
        client = await aiohttp_client(web_app)
        await client.post("/api/proxies", json={"proxy": "http://5.6.7.8:9090"})
        resp = await client.get("/api/proxies")
        data = await resp.json()
        proxy = next(p for p in data["proxies"] if p["host"] == "5.6.7.8")
        assert "successes" in proxy
        assert proxy["successes"] == 0

    @pytest.mark.asyncio
    async def test_index_contains_copy_last_20_button(self, web_app, aiohttp_client):
        client = await aiohttp_client(web_app)
        resp = await client.get("/")
        text = await resp.text()
        assert "Copy Last 20" in text

    @pytest.mark.asyncio
    async def test_index_contains_successes_column(self, web_app, aiohttp_client):
        client = await aiohttp_client(web_app)
        resp = await client.get("/")
        text = await resp.text()
        assert "<th>Successes</th>" in text

    @pytest.mark.asyncio
    async def test_index_contains_proxy_successes_card(self, web_app, aiohttp_client):
        """Proxy list should have a Total Successes summary card."""
        client = await aiohttp_client(web_app)
        resp = await client.get("/")
        text = await resp.text()
        assert 'id="px-successes"' in text
        assert ">Success<" in text

    @pytest.mark.asyncio
    async def test_general_settings_max_retries_roundtrip(self, web_app, aiohttp_client):
        client = await aiohttp_client(web_app)
        resp = await client.post(
            "/api/general-settings",
            json={"max_retries": 20},
        )
        assert resp.status == 200

        resp = await client.get("/api/general-settings")
        data = await resp.json()
        assert data["max_retries"] == 20

    @pytest.mark.asyncio
    async def test_general_settings_max_retries_minimum(self, web_app, aiohttp_client):
        """max_retries should never go below 1."""
        client = await aiohttp_client(web_app)
        resp = await client.post(
            "/api/general-settings",
            json={"max_retries": 0},
        )
        assert resp.status == 200
        resp = await client.get("/api/general-settings")
        data = await resp.json()
        assert data["max_retries"] == 1

    @pytest.mark.asyncio
    async def test_index_contains_log_color_classes(self, web_app, aiohttp_client):
        """HTML should contain CSS classes for success and error log coloring."""
        client = await aiohttp_client(web_app)
        resp = await client.get("/")
        text = await resp.text()
        assert ".log-success" in text
        assert ".log-error" in text

    @pytest.mark.asyncio
    async def test_index_contains_logclass_function(self, web_app, aiohttp_client):
        """The logClass JS function should exist for content-based coloring."""
        client = await aiohttp_client(web_app)
        resp = await client.get("/")
        text = await resp.text()
        assert "function logClass" in text
