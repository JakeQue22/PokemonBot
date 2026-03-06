"""Tests for the config module."""

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
