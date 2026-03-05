"""Tests for the proxy module."""

from pokemonbot.proxy import Proxy, ProxyPool, load_proxies, parse_proxy


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
