"""Tests for the config module."""

import errno
from unittest.mock import patch

from pokemonbot.config import AppConfig, MonitorConfig, load_config, save_config


class TestLoadConfig:
    def test_minimal_config(self, tmp_path):
        f = tmp_path / "config.yaml"
        f.write_text("monitors: []\n")
        cfg = load_config(f)
        assert isinstance(cfg, AppConfig)
        assert cfg.monitors == []

    def test_full_config(self, tmp_path):
        f = tmp_path / "config.yaml"
        f.write_text(
            """\
proxies:
  file: my_proxies.txt
  rotate_on_error: false
notifier:
  console: true
  discord_webhook_url: "https://discord.com/api/webhooks/test"
concurrency: 5
request_timeout: 15.0
max_retries: 20
monitors:
  - name: test
    url: https://example.com
    site: generic
    keywords:
      - pokemon
    interval: 3.0
"""
        )
        cfg = load_config(f)
        assert cfg.proxies.file == "my_proxies.txt"
        assert cfg.proxies.rotate_on_error is False
        assert cfg.notifier.discord_webhook_url == "https://discord.com/api/webhooks/test"
        assert cfg.concurrency == 5
        assert cfg.max_retries == 20
        assert len(cfg.monitors) == 1
        m = cfg.monitors[0]
        assert m.name == "test"
        assert m.site == "generic"
        assert m.keywords == ["pokemon"]

    def test_empty_file(self, tmp_path):
        f = tmp_path / "config.yaml"
        f.write_text("")
        cfg = load_config(f)
        assert isinstance(cfg, AppConfig)

    def test_env_var_expansion(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MY_WEBHOOK", "https://hooks.example.com/abc")
        f = tmp_path / "config.yaml"
        f.write_text('notifier:\n  discord_webhook_url: "${MY_WEBHOOK}"\n')
        cfg = load_config(f)
        assert cfg.notifier.discord_webhook_url == "https://hooks.example.com/abc"


class TestSaveConfig:
    def test_save_and_reload_roundtrip(self, tmp_path):
        """Config survives a save → load cycle with all fields intact."""
        f = tmp_path / "config.yaml"

        cfg = AppConfig(
            concurrency=5,
            request_timeout=15.0,
            portal_name="MyBot",
            base_url="https://bot.example.com",
        )
        cfg.proxies.file = "my_proxies.txt"
        cfg.proxies.rotate_on_error = False
        cfg.notifier.discord_webhook_url = "https://discord.com/api/webhooks/test"
        cfg.email.enabled = True
        cfg.email.smtp_host = "smtp.example.com"
        cfg.email.smtp_port = 465
        cfg.email.username = "user"
        cfg.email.password = "secret"
        cfg.email.from_address = "bot@example.com"
        cfg.email.to_addresses = ["admin@example.com"]
        cfg.monitors.append(
            MonitorConfig(
                name="test",
                url="https://example.com",
                site="pokemoncenter",
                keywords=["pokemon", "cards"],
                interval=3.0,
            )
        )

        save_config(cfg, f)
        loaded = load_config(f)

        assert loaded.concurrency == 5
        assert loaded.request_timeout == 15.0
        assert loaded.portal_name == "MyBot"
        assert loaded.base_url == "https://bot.example.com"
        assert loaded.proxies.file == "my_proxies.txt"
        assert loaded.proxies.rotate_on_error is False
        assert loaded.notifier.discord_webhook_url == "https://discord.com/api/webhooks/test"
        assert loaded.email.enabled is True
        assert loaded.email.smtp_host == "smtp.example.com"
        assert loaded.email.smtp_port == 465
        assert loaded.email.username == "user"
        assert loaded.email.password == "secret"
        assert loaded.email.from_address == "bot@example.com"
        assert loaded.email.to_addresses == ["admin@example.com"]
        assert len(loaded.monitors) == 1
        m = loaded.monitors[0]
        assert m.name == "test"
        assert m.url == "https://example.com"
        assert m.site == "pokemoncenter"
        assert m.keywords == ["pokemon", "cards"]
        assert m.interval == 3.0

    def test_save_default_config_roundtrip(self, tmp_path):
        """A default AppConfig roundtrips without error."""
        f = tmp_path / "config.yaml"
        cfg = AppConfig()
        save_config(cfg, f)
        loaded = load_config(f)
        assert isinstance(loaded, AppConfig)
        assert loaded.monitors == []
        assert loaded.concurrency == 10

    def test_save_creates_file(self, tmp_path):
        """save_config creates the file if it doesn't exist."""
        f = tmp_path / "new_config.yaml"
        assert not f.exists()
        save_config(AppConfig(), f)
        assert f.exists()

    def test_save_overwrites_existing(self, tmp_path):
        """save_config overwrites an existing config file."""
        f = tmp_path / "config.yaml"
        f.write_text("monitors: []\nconcurrency: 99\n")
        cfg = load_config(f)
        assert cfg.concurrency == 99

        cfg.concurrency = 42
        save_config(cfg, f)
        reloaded = load_config(f)
        assert reloaded.concurrency == 42

    def test_monitors_persist_after_add(self, tmp_path):
        """Adding a monitor and saving persists it to disk."""
        f = tmp_path / "config.yaml"
        f.write_text("monitors: []\n")
        cfg = load_config(f)
        assert len(cfg.monitors) == 0

        cfg.monitors.append(
            MonitorConfig(name="new", url="https://new.example.com", site="generic", interval=10.0)
        )
        save_config(cfg, f)
        reloaded = load_config(f)
        assert len(reloaded.monitors) == 1
        assert reloaded.monitors[0].name == "new"
        assert reloaded.monitors[0].url == "https://new.example.com"

    def test_save_falls_back_on_ebusy(self, tmp_path):
        """When os.replace raises EBUSY (Docker bind-mount), save_config falls back to direct write."""
        f = tmp_path / "config.yaml"
        f.write_text("monitors: []\n")

        cfg = AppConfig(concurrency=77)
        with patch("pokemonbot.config.os.replace", side_effect=OSError(errno.EBUSY, "Device or resource busy")):
            save_config(cfg, f)

        reloaded = load_config(f)
        assert reloaded.concurrency == 77

    def test_enabled_field_default_true(self, tmp_path):
        """MonitorConfig.enabled defaults to True."""
        f = tmp_path / "config.yaml"
        f.write_text(
            "monitors:\n  - name: test\n    url: https://example.com\n    site: generic\n    interval: 5.0\n"
        )
        cfg = load_config(f)
        assert len(cfg.monitors) == 1
        assert cfg.monitors[0].enabled is True

    def test_enabled_field_false_roundtrip(self, tmp_path):
        """A disabled monitor survives a save → load cycle."""
        f = tmp_path / "config.yaml"
        cfg = AppConfig()
        cfg.monitors.append(
            MonitorConfig(name="off", url="https://example.com", site="generic", enabled=False)
        )
        save_config(cfg, f)
        reloaded = load_config(f)
        assert len(reloaded.monitors) == 1
        assert reloaded.monitors[0].enabled is False

    def test_enabled_field_true_not_written(self, tmp_path):
        """enabled=True should not appear in the YAML (it's the default)."""
        f = tmp_path / "config.yaml"
        cfg = AppConfig()
        cfg.monitors.append(
            MonitorConfig(name="on", url="https://example.com", site="generic", enabled=True)
        )
        save_config(cfg, f)
        content = f.read_text()
        assert "enabled" not in content

    def test_direct_fallback_default_false(self, tmp_path):
        """ProxyConfig.direct_fallback defaults to False."""
        f = tmp_path / "config.yaml"
        f.write_text("monitors: []\n")
        cfg = load_config(f)
        assert cfg.proxies.direct_fallback is False

    def test_direct_fallback_false_roundtrip(self, tmp_path):
        """A disabled direct_fallback survives a save → load cycle."""
        f = tmp_path / "config.yaml"
        cfg = AppConfig()
        cfg.proxies.direct_fallback = False
        save_config(cfg, f)
        reloaded = load_config(f)
        assert reloaded.proxies.direct_fallback is False

    def test_direct_fallback_false_not_written(self, tmp_path):
        """direct_fallback=False should not appear in the YAML (it's the default)."""
        f = tmp_path / "config.yaml"
        cfg = AppConfig()
        cfg.proxies.direct_fallback = False
        save_config(cfg, f)
        content = f.read_text()
        assert "direct_fallback" not in content

    def test_xai_api_key_roundtrip(self, tmp_path):
        """xai_api_key should persist and load correctly."""
        f = tmp_path / "config.yaml"
        cfg = AppConfig(xai_api_key="xai-test-key-123")
        save_config(cfg, f)
        reloaded = load_config(f)
        assert reloaded.xai_api_key == "xai-test-key-123"

    def test_xai_api_key_not_written_when_empty(self, tmp_path):
        """xai_api_key should not appear in the YAML when empty (default)."""
        f = tmp_path / "config.yaml"
        cfg = AppConfig()
        save_config(cfg, f)
        content = f.read_text()
        assert "xai_api_key" not in content

    def test_xai_api_key_from_env_var(self, tmp_path):
        """xai_api_key should support ${ENV_VAR} expansion."""
        f = tmp_path / "config.yaml"
        f.write_text('xai_api_key: "${XAI_KEY}"\nmonitors: []\n')
        with patch.dict("os.environ", {"XAI_KEY": "xai-from-env"}):
            cfg = load_config(f)
        assert cfg.xai_api_key == "xai-from-env"

    def test_xai_api_key_defaults_empty(self, tmp_path):
        """When xai_api_key is not in config, it defaults to empty string."""
        f = tmp_path / "config.yaml"
        f.write_text("monitors: []\n")
        cfg = load_config(f)
        assert cfg.xai_api_key == ""
