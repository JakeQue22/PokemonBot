"""Tests for the proxy module."""

from unittest.mock import patch

from pokemonbot.proxy import (
    Proxy,
    ProxyPool,
    ensure_proxy_file,
    load_proxies,
    load_proxy_stats,
    parse_proxy,
    save_proxies,
    save_proxy_stats,
)


class TestParseProxy:
    def test_full_url(self):
        p = parse_proxy("http://1.2.3.4:8080")
        assert p == Proxy(protocol="http", host="1.2.3.4", port=8080)

    def test_socks5_with_auth(self):
        p = parse_proxy("socks5://user:pass@10.0.0.1:1080")
        assert p.protocol == "socks5"
        assert p.username == "user"
        assert p.password == "pass"
        assert p.host == "10.0.0.1"
        assert p.port == 1080

    def test_host_port_only(self):
        p = parse_proxy("192.168.1.1:3128")
        assert p.protocol == "http"
        assert p.host == "192.168.1.1"
        assert p.port == 3128

    def test_host_port_user_pass(self):
        p = parse_proxy("192.168.1.1:3128:admin:secret")
        assert p.username == "admin"
        assert p.password == "secret"

    def test_url_property(self):
        p = Proxy(protocol="http", host="1.2.3.4", port=8080, username="u", password="p")
        assert p.url == "http://u:p@1.2.3.4:8080"

    def test_url_property_no_auth(self):
        p = Proxy(protocol="http", host="1.2.3.4", port=8080)
        assert p.url == "http://1.2.3.4:8080"


class TestProxyPool:
    def _make_proxies(self, n: int = 3) -> list[Proxy]:
        return [
            Proxy(protocol="http", host=f"10.0.0.{i}", port=8080)
            for i in range(n)
        ]

    def test_cycles(self):
        proxies = self._make_proxies(3)
        pool = ProxyPool(proxies, shuffle=False)
        seen = [pool.next() for _ in range(6)]
        # Should cycle through all 3, then repeat
        assert seen[:3] == seen[3:]

    def test_size(self):
        proxies = self._make_proxies(5)
        pool = ProxyPool(proxies)
        assert pool.size == 5

    def test_mark_failed(self):
        proxies = self._make_proxies(2)
        pool = ProxyPool(proxies, shuffle=False)
        assert pool.failed_count == 0
        pool.mark_failed(proxies[0])
        assert pool.failed_count == 1
        pool.reset_failures()
        assert pool.failed_count == 0

    def test_empty_raises(self):
        try:
            ProxyPool([])
            assert False, "Should have raised"
        except ValueError:
            pass

    def test_stats(self):
        proxies = self._make_proxies(2)
        pool = ProxyPool(proxies, shuffle=False)
        # Use each proxy once
        p0 = pool.next()
        p1 = pool.next()
        pool.mark_failed(p1)
        stats = pool.stats()
        assert len(stats) == 2
        assert stats[0]["requests"] == 1
        assert stats[0]["failures"] == 0
        assert stats[0]["successes"] == 1
        assert stats[1]["requests"] == 1
        assert stats[1]["failures"] == 1
        assert stats[1]["successes"] == 0

    def test_proxies_property(self):
        proxies = self._make_proxies(3)
        pool = ProxyPool(proxies, shuffle=False)
        assert pool.proxies == proxies

    def test_next_increments_requests(self):
        proxies = self._make_proxies(1)
        pool = ProxyPool(proxies, shuffle=False)
        pool.next()
        pool.next()
        pool.next()
        stats = pool.stats()
        assert stats[0]["requests"] == 3

    def test_mark_success_sticky(self):
        """After mark_success(), next_available() returns the preferred proxy."""
        proxies = self._make_proxies(3)
        pool = ProxyPool(proxies, shuffle=False)
        # Without sticky, first call returns proxies[0]
        first = pool.next_available()
        assert first == proxies[0]
        # Mark proxies[2] as preferred
        pool.mark_success(proxies[2])
        # Now next_available() should return proxies[2] (the preferred)
        preferred = pool.next_available()
        assert preferred == proxies[2]
        # Calling again still returns the preferred
        preferred2 = pool.next_available()
        assert preferred2 == proxies[2]

    def test_mark_success_cleared_on_failure(self):
        """When the preferred proxy fails, next_available() falls back to round-robin."""
        proxies = self._make_proxies(3)
        pool = ProxyPool(proxies, shuffle=False)
        pool.mark_success(proxies[1])
        assert pool.next_available() == proxies[1]
        # Now mark that proxy as failed
        pool.mark_failed(proxies[1])
        # Preferred was cleared, should get round-robin
        result = pool.next_available()
        assert result != proxies[1]

    def test_mark_success_skipped_when_on_cooldown(self):
        """Preferred proxy on cooldown is skipped; round-robin takes over."""
        proxies = self._make_proxies(3)
        pool = ProxyPool(proxies, shuffle=False, cooldown_seconds=9999)
        pool.mark_success(proxies[0])
        # Put preferred on cooldown
        pool.mark_failed(proxies[0])
        # Preferred is cleared and on cooldown, should get round-robin
        result = pool.next_available()
        assert result is not None
        assert result != proxies[0]


class TestLoadProxies:
    def test_load_from_file(self, tmp_path):
        f = tmp_path / "proxies.txt"
        f.write_text("http://1.1.1.1:8080\n# comment\nsocks5://2.2.2.2:1080\n\n")
        result = load_proxies(f)
        assert len(result) == 2
        assert result[0].protocol == "http"
        assert result[1].protocol == "socks5"

    def test_invalid_lines_skipped(self, tmp_path):
        f = tmp_path / "proxies.txt"
        f.write_text("valid:8080\nnot_valid_at_all\n")
        result = load_proxies(f)
        assert len(result) == 1


class TestSaveProxies:
    def test_save_and_reload(self, tmp_path):
        f = tmp_path / "proxies.txt"
        proxies = [
            Proxy(protocol="http", host="1.1.1.1", port=8080),
            Proxy(protocol="socks5", host="2.2.2.2", port=1080),
        ]
        save_proxies(proxies, f)
        loaded = load_proxies(f)
        assert len(loaded) == 2
        assert loaded[0].host == "1.1.1.1"
        assert loaded[1].protocol == "socks5"

    def test_save_empty(self, tmp_path):
        f = tmp_path / "proxies.txt"
        save_proxies([], f)
        assert f.read_text() == ""


class TestEnsureProxyFile:
    def test_creates_missing_file(self, tmp_path):
        f = tmp_path / "new_proxies.txt"
        assert not f.exists()
        result = ensure_proxy_file(f)
        assert result.is_file()

    def test_replaces_directory_with_file(self, tmp_path):
        d = tmp_path / "proxies.txt"
        d.mkdir()
        assert d.is_dir()
        result = ensure_proxy_file(d)
        assert result.is_file()
        assert not result.is_dir()

    def test_leaves_existing_file_alone(self, tmp_path):
        f = tmp_path / "proxies.txt"
        f.write_text("http://1.1.1.1:8080\n")
        result = ensure_proxy_file(f)
        assert result.is_file()
        assert "1.1.1.1" in result.read_text()

    def test_falls_back_to_file_inside_immovable_directory(self, tmp_path):
        """When the directory cannot be removed (e.g. Docker mount), use a file inside it."""
        d = tmp_path / "proxies.txt"
        d.mkdir()
        with patch("shutil.rmtree", side_effect=OSError("Device or resource busy")):
            result = ensure_proxy_file(d)
        assert result.is_file()
        assert result.parent == d  # file is INSIDE the directory
        assert result.name == "proxies.txt"


class TestProxyStatsPeristence:
    """Tests for saving / loading per-proxy stats to a JSON sidecar file."""

    def test_save_and_load_roundtrip(self, tmp_path):
        proxy_file = tmp_path / "proxies.txt"
        proxy_file.write_text("")
        stats = {
            "http://1.1.1.1:8080": {"requests": 10, "failures": 2},
            "socks5://2.2.2.2:1080": {"requests": 5, "failures": 0},
        }
        save_proxy_stats(stats, proxy_file)
        loaded = load_proxy_stats(proxy_file)
        assert loaded == stats

    def test_load_returns_empty_when_no_file(self, tmp_path):
        proxy_file = tmp_path / "proxies.txt"
        proxy_file.write_text("")
        loaded = load_proxy_stats(proxy_file)
        assert loaded == {}

    def test_load_returns_empty_on_corrupt_json(self, tmp_path):
        proxy_file = tmp_path / "proxies.txt"
        proxy_file.write_text("")
        stats_file = tmp_path / "proxies.stats.json"
        stats_file.write_text("{not valid json")
        loaded = load_proxy_stats(proxy_file)
        assert loaded == {}

    def test_pool_auto_persists_on_mark_success(self, tmp_path):
        proxy_file = tmp_path / "proxies.txt"
        proxy_file.write_text("")
        proxy = Proxy(protocol="http", host="1.1.1.1", port=8080)
        pool = ProxyPool([proxy], shuffle=False)
        pool.set_stats_path(proxy_file)
        pool.next()
        pool.mark_success(proxy)
        loaded = load_proxy_stats(proxy_file)
        assert "http://1.1.1.1:8080" in loaded
        assert loaded["http://1.1.1.1:8080"]["requests"] == 1

    def test_pool_auto_persists_on_mark_failed(self, tmp_path):
        proxy_file = tmp_path / "proxies.txt"
        proxy_file.write_text("")
        proxy = Proxy(protocol="http", host="1.1.1.1", port=8080)
        pool = ProxyPool([proxy], shuffle=False)
        pool.set_stats_path(proxy_file)
        pool.next()
        pool.mark_failed(proxy)
        loaded = load_proxy_stats(proxy_file)
        assert loaded["http://1.1.1.1:8080"]["failures"] == 1


class TestProxyPoolPersistedStats:
    """Tests for restoring stats from a previous session and sorting by success rate."""

    def test_restores_counters_from_persisted(self):
        proxies = [
            Proxy(protocol="http", host="1.1.1.1", port=8080),
            Proxy(protocol="http", host="2.2.2.2", port=8080),
        ]
        persisted = {
            "http://1.1.1.1:8080": {"requests": 10, "failures": 2},
            "http://2.2.2.2:8080": {"requests": 5, "failures": 1},
        }
        pool = ProxyPool(proxies, shuffle=False, persisted_stats=persisted)
        stats = {s["url"]: s for s in pool.stats()}
        assert stats["http://1.1.1.1:8080"]["requests"] == 10
        assert stats["http://1.1.1.1:8080"]["failures"] == 2
        assert stats["http://2.2.2.2:8080"]["requests"] == 5
        assert stats["http://2.2.2.2:8080"]["failures"] == 1

    def test_sorts_by_success_rate(self):
        proxies = [
            Proxy(protocol="http", host="bad.proxy", port=8080),    # 50% success
            Proxy(protocol="http", host="good.proxy", port=8080),   # 90% success
            Proxy(protocol="http", host="new.proxy", port=8080),    # no stats
        ]
        persisted = {
            "http://bad.proxy:8080": {"requests": 10, "failures": 5},
            "http://good.proxy:8080": {"requests": 10, "failures": 1},
        }
        pool = ProxyPool(proxies, persisted_stats=persisted)
        order = [p.host for p in pool.proxies]
        # good.proxy (90% success) first, then bad.proxy (50%), then new (0%)
        assert order[0] == "good.proxy"
        assert order[1] == "bad.proxy"
        assert order[2] == "new.proxy"

    def test_shuffle_skipped_when_persisted_stats_provided(self):
        """When persisted stats are present, shuffle is not applied."""
        proxies = [
            Proxy(protocol="http", host="a", port=1),
            Proxy(protocol="http", host="b", port=1),
        ]
        persisted = {
            "http://a:1": {"requests": 10, "failures": 0},
            "http://b:1": {"requests": 10, "failures": 5},
        }
        # Even with shuffle=True (default), persisted_stats sorting takes precedence.
        pool = ProxyPool(proxies, persisted_stats=persisted)
        assert pool.proxies[0].host == "a"
        assert pool.proxies[1].host == "b"
