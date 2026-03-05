"""Tests for the config module."""

from pokemonbot.config import AppConfig, MonitorConfig, load_config


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
