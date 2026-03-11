"""Tests for the session module."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from pokemonbot.proxy import Proxy
from pokemonbot.session import (
    _build_connector,
    _format_error,
    _get_domain_overrides,
    _random_user_agent,
    _HAS_CURL_CFFI,
    _IMPERSONATE_BROWSERS,
    create_session,
)


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

    def test_pokemoncenter_sec_ch_ua_headers(self):
        """Pokemon Center overrides should include Sec-CH-UA client hints."""
        headers, _ = _get_domain_overrides(
            "https://www.pokemoncenter.com/en-gb/category/elite-trainer-box"
        )
        assert "Sec-CH-UA" in headers
        assert "Sec-CH-UA-Mobile" in headers
        assert "Sec-CH-UA-Platform" in headers
        assert "Chrome" in headers["Sec-CH-UA"]

    def test_unknown_domain_empty(self):
        headers, cookies = _get_domain_overrides("https://example.com/page")
        assert headers == {}
        assert cookies == {}


class TestFormatError:
    def test_message_preserved(self):
        """When the exception has a message, _format_error returns it."""
        exc = ConnectionError("SOCKS5 connection refused")
        assert _format_error(exc) == "SOCKS5 connection refused"

    def test_empty_message_uses_class_name(self):
        """When str(exc) is empty, the class name is returned."""
        exc = asyncio.TimeoutError()
        assert _format_error(exc) == "TimeoutError"

    def test_empty_string_message_uses_class_name(self):
        """When str(exc) is an empty string, the class name is returned."""
        exc = Exception("")
        assert _format_error(exc) == "Exception"

    def test_subclass_qualname(self):
        """Nested/subclass names should use qualname for clarity."""
        exc = ConnectionResetError()
        result = _format_error(exc)
        assert result == "ConnectionResetError"


class TestBuildConnector:
    @pytest.mark.asyncio
    async def test_direct_uses_ssl_context(self):
        """Direct connections should use a real SSL context."""
        connector = _build_connector(None)
        # TCPConnector stores ssl context; should not be False
        assert connector._ssl is not False
        await connector.close()

    @pytest.mark.asyncio
    async def test_proxy_disables_ssl_verification(self):
        """Proxy connections should disable SSL verification to avoid
        CERTIFICATE_VERIFY_FAILED from intercepting proxies."""
        proxy = Proxy(protocol="http", host="1.2.3.4", port=8080)
        connector = _build_connector(proxy)
        assert connector._ssl is False
        await connector.close()


class TestBrotliSupport:
    def test_brotli_importable(self):
        """Brotli must be installed so aiohttp can decompress br responses."""
        import brotli  # noqa: F401

    @pytest.mark.asyncio
    async def test_accept_encoding_includes_br(self):
        """Session headers advertise brotli; the package must back that up."""
        session = await create_session(timeout=5.0)
        assert "br" in session.headers.get("Accept-Encoding", "")
        await session.close()


class TestCurlCffiIntegration:
    def test_curl_cffi_available(self):
        """curl_cffi should be importable when installed."""
        assert _HAS_CURL_CFFI is True

    def test_impersonate_browsers_defined(self):
        """At least one browser impersonation target should be configured."""
        assert len(_IMPERSONATE_BROWSERS) >= 1
        for b in _IMPERSONATE_BROWSERS:
            assert b.startswith("chrome")

    def test_impersonate_browsers_supported(self):
        """Every configured browser target must be recognised by curl_cffi."""
        from curl_cffi.requests import BrowserType

        for browser in _IMPERSONATE_BROWSERS:
            assert hasattr(BrowserType, browser), (
                f"{browser!r} is not supported by curl_cffi {__import__('curl_cffi').__version__}"
            )


class TestFetchUsesCurlCffi:
    @pytest.mark.asyncio
    async def test_fetch_selects_curl_cffi_when_available(self):
        """When curl_cffi is available, fetch() should try it first."""
        from pokemonbot.session import fetch, _HAS_CURL_CFFI

        if not _HAS_CURL_CFFI:
            pytest.skip("curl_cffi not installed")

        fake_response = {
            "status": 200,
            "body": "<html>OK</html>",
            "headers": {},
            "url": "https://example.com",
        }

        with patch(
            "pokemonbot.session._fetch_with_curl_cffi",
            new_callable=AsyncMock,
            return_value=fake_response,
        ) as mock_cffi, patch(
            "pokemonbot.session._fetch_with_aiohttp",
            new_callable=AsyncMock,
            return_value=fake_response,
        ):
            result = await fetch("https://example.com", max_retries=1)
            assert mock_cffi.called
            assert result["status"] == 200


class TestFetchProxyTimeout:
    @pytest.mark.asyncio
    async def test_proxy_timeout_used_when_proxy_present(self):
        """When proxy_timeout is set and a proxy is used, the shorter timeout
        should be passed to the fetcher instead of the main timeout."""
        from pokemonbot.session import fetch
        from pokemonbot.proxy import ProxyPool

        proxy = Proxy(protocol="http", host="1.2.3.4", port=8080)
        pool = ProxyPool([proxy])

        fake_response = {
            "status": 200,
            "body": "<html>OK</html>",
            "headers": {},
            "url": "https://example.com",
        }

        with patch(
            "pokemonbot.session._fetch_with_curl_cffi",
            new_callable=AsyncMock,
            return_value=fake_response,
        ) as mock_cffi, patch(
            "pokemonbot.session._fetch_with_aiohttp",
            new_callable=AsyncMock,
            return_value=fake_response,
        ):
            await fetch(
                "https://example.com",
                proxy_pool=pool,
                timeout=30.0,
                proxy_timeout=10.0,
                max_retries=1,
            )
            # The first backend (curl_cffi) should have been called with proxy_timeout
            _, kwargs = mock_cffi.call_args
            assert kwargs["timeout"] == 10.0

    @pytest.mark.asyncio
    async def test_direct_uses_main_timeout(self):
        """Without a proxy, the main timeout should be used."""
        from pokemonbot.session import fetch

        fake_response = {
            "status": 200,
            "body": "<html>OK</html>",
            "headers": {},
            "url": "https://example.com",
        }

        with patch(
            "pokemonbot.session._fetch_with_curl_cffi",
            new_callable=AsyncMock,
            return_value=fake_response,
        ) as mock_cffi, patch(
            "pokemonbot.session._fetch_with_aiohttp",
            new_callable=AsyncMock,
            return_value=fake_response,
        ):
            await fetch(
                "https://example.com",
                timeout=30.0,
                proxy_timeout=10.0,
                max_retries=1,
            )
            # No proxy pool → direct connection → main timeout used
            _, kwargs = mock_cffi.call_args
            assert kwargs["timeout"] == 30.0


class TestFetchDirectFallback:
    @pytest.mark.asyncio
    async def test_direct_fallback_when_all_proxies_fail(self):
        """When all proxy attempts fail, fetch() should try a direct connection."""
        from pokemonbot.session import fetch
        from pokemonbot.proxy import ProxyPool

        proxy = Proxy(protocol="http", host="1.2.3.4", port=8080)
        pool = ProxyPool([proxy])

        fake_response = {
            "status": 200,
            "body": "<html>OK</html>",
            "headers": {},
            "url": "https://example.com",
        }

        call_count = 0
        last_proxy_arg = "UNSET"

        async def mock_fetch(url, *, proxy=None, **kwargs):
            nonlocal call_count, last_proxy_arg
            call_count += 1
            last_proxy_arg = proxy
            if proxy is not None:
                raise ConnectionError("proxy dead")
            return fake_response

        with patch(
            "pokemonbot.session._fetch_with_curl_cffi",
            side_effect=mock_fetch,
        ), patch(
            "pokemonbot.session._fetch_with_aiohttp",
            side_effect=mock_fetch,
        ):
            result = await fetch(
                "https://example.com",
                proxy_pool=pool,
                timeout=30.0,
                proxy_timeout=10.0,
                max_retries=2,
            )
            assert result["status"] == 200
            # The last (successful) call should be direct (proxy=None)
            assert last_proxy_arg is None
            assert call_count >= 2  # at least 1 proxy + 1 direct

    @pytest.mark.asyncio
    async def test_direct_fallback_also_fails_raises(self):
        """When both proxy and direct attempts fail, ConnectionError is raised."""
        from pokemonbot.session import fetch
        from pokemonbot.proxy import ProxyPool

        proxy = Proxy(protocol="http", host="1.2.3.4", port=8080)
        pool = ProxyPool([proxy])

        async def mock_fetch(url, **kwargs):
            raise ConnectionError("all dead")

        with patch(
            "pokemonbot.session._fetch_with_curl_cffi",
            side_effect=mock_fetch,
        ), patch(
            "pokemonbot.session._fetch_with_aiohttp",
            side_effect=mock_fetch,
        ):
            with pytest.raises(ConnectionError):
                await fetch(
                    "https://example.com",
                    proxy_pool=pool,
                    timeout=30.0,
                    max_retries=1,
                )

    @pytest.mark.asyncio
    async def test_no_fallback_without_proxy_pool(self):
        """Without a proxy pool there is no fallback – just retries."""
        from pokemonbot.session import fetch

        call_count = 0

        async def mock_fetch(url, **kwargs):
            nonlocal call_count
            call_count += 1
            raise ConnectionError("dead")

        with patch(
            "pokemonbot.session._fetch_with_curl_cffi",
            side_effect=mock_fetch,
        ), patch(
            "pokemonbot.session._fetch_with_aiohttp",
            side_effect=mock_fetch,
        ):
            with pytest.raises(ConnectionError):
                await fetch(
                    "https://example.com",
                    timeout=30.0,
                    max_retries=2,
                )
            # Only retry attempts, no extra fallback
            assert call_count == 2

    @pytest.mark.asyncio
    async def test_direct_fallback_disabled(self):
        """When direct_fallback=False, no direct attempt after proxy failures."""
        from pokemonbot.session import fetch
        from pokemonbot.proxy import ProxyPool

        proxy = Proxy(protocol="http", host="1.2.3.4", port=8080)
        pool = ProxyPool([proxy])

        call_count = 0
        proxy_args: list = []

        async def mock_fetch(url, *, proxy=None, **kwargs):
            nonlocal call_count
            call_count += 1
            proxy_args.append(proxy)
            raise ConnectionError("dead")

        with patch(
            "pokemonbot.session._fetch_with_curl_cffi",
            side_effect=mock_fetch,
        ), patch(
            "pokemonbot.session._fetch_with_aiohttp",
            side_effect=mock_fetch,
        ):
            with pytest.raises(ConnectionError):
                await fetch(
                    "https://example.com",
                    proxy_pool=pool,
                    timeout=30.0,
                    max_retries=1,
                    direct_fallback=False,
                )
            # Should only have proxy attempts, no direct (proxy=None) attempt
            assert call_count >= 1
            assert all(p is not None for p in proxy_args)

    @pytest.mark.asyncio
    async def test_direct_fallback_enabled_explicitly(self):
        """When direct_fallback=True (default), direct attempt is made."""
        from pokemonbot.session import fetch
        from pokemonbot.proxy import ProxyPool

        proxy = Proxy(protocol="http", host="1.2.3.4", port=8080)
        pool = ProxyPool([proxy])

        fake_response = {
            "status": 200,
            "body": "<html>OK</html>",
            "headers": {},
            "url": "https://example.com",
        }

        proxy_args: list = []

        async def mock_fetch(url, *, proxy=None, **kwargs):
            proxy_args.append(proxy)
            if proxy is not None:
                raise ConnectionError("proxy dead")
            return fake_response

        with patch(
            "pokemonbot.session._fetch_with_curl_cffi",
            side_effect=mock_fetch,
        ), patch(
            "pokemonbot.session._fetch_with_aiohttp",
            side_effect=mock_fetch,
        ):
            result = await fetch(
                "https://example.com",
                proxy_pool=pool,
                timeout=30.0,
                max_retries=1,
                direct_fallback=True,
            )
            assert result["status"] == 200
            # Last call should be direct (proxy=None)
            assert proxy_args[-1] is None


class TestFetchFastSkip:
    """Tests for the fast-skip optimisation: non-timeout proxy failures
    do NOT count toward max_retries, letting the bot try many more proxies.
    """

    @pytest.mark.asyncio
    async def test_fast_failures_get_extra_attempts(self):
        """Fast-failing proxies (non-timeout) should be retried beyond max_retries."""
        from pokemonbot.session import fetch
        from pokemonbot.proxy import ProxyPool

        # Pool with many proxies so we don't exhaust the pool itself
        proxies = [
            Proxy(protocol="http", host=f"10.0.0.{i}", port=8080)
            for i in range(20)
        ]
        pool = ProxyPool(proxies, cooldown_seconds=0)

        call_count = 0
        success_at = 8  # succeed on the 8th attempt

        async def mock_fetch(url, *, proxy=None, **kwargs):
            nonlocal call_count
            call_count += 1
            if proxy is not None and call_count < success_at:
                # Simulate fast proxy failure (non-timeout)
                raise ConnectionError("SOCKS5 connection refused")
            if proxy is not None:
                return {
                    "status": 200,
                    "body": "OK",
                    "headers": {},
                    "url": url,
                }
            raise ConnectionError("should not reach direct")

        with patch(
            "pokemonbot.session._fetch_with_curl_cffi",
            side_effect=mock_fetch,
        ), patch(
            "pokemonbot.session._fetch_with_aiohttp",
            side_effect=mock_fetch,
        ):
            result = await fetch(
                "https://example.com",
                proxy_pool=pool,
                max_retries=2,  # only 2 timeout retries, but fast-fails get more
            )
            assert result["status"] == 200
            # Should have succeeded after more than max_retries attempts
            assert call_count == success_at

    @pytest.mark.asyncio
    async def test_timeout_failures_respect_max_retries(self):
        """Timeout failures should be capped by max_retries."""
        from pokemonbot.session import fetch
        from pokemonbot.proxy import ProxyPool

        proxies = [
            Proxy(protocol="http", host=f"10.0.0.{i}", port=8080)
            for i in range(20)
        ]
        pool = ProxyPool(proxies, cooldown_seconds=0)

        timeout_calls = 0

        async def mock_fetch(url, *, proxy=None, **kwargs):
            nonlocal timeout_calls
            if proxy is not None:
                timeout_calls += 1
                raise ConnectionError("Connection timed out after 10000 milliseconds")
            raise ConnectionError("direct also dead")

        with patch(
            "pokemonbot.session._fetch_with_curl_cffi",
            side_effect=mock_fetch,
        ), patch(
            "pokemonbot.session._fetch_with_aiohttp",
            side_effect=mock_fetch,
        ):
            with pytest.raises(ConnectionError):
                await fetch(
                    "https://example.com",
                    proxy_pool=pool,
                    max_retries=3,
                    direct_fallback=False,
                )
            # Should stop after exactly max_retries timeout failures
            assert timeout_calls == 3

    @pytest.mark.asyncio
    async def test_mixed_fast_and_timeout_failures(self):
        """Mix of fast and timeout failures: only timeouts count toward limit."""
        from pokemonbot.session import fetch
        from pokemonbot.proxy import ProxyPool

        proxies = [
            Proxy(protocol="http", host=f"10.0.0.{i}", port=8080)
            for i in range(30)
        ]
        pool = ProxyPool(proxies, cooldown_seconds=0)

        call_count = 0
        timeout_count = 0
        fast_count = 0

        async def mock_fetch(url, *, proxy=None, **kwargs):
            nonlocal call_count, timeout_count, fast_count
            call_count += 1
            if proxy is not None:
                # Alternate between fast and timeout failures
                if call_count % 3 == 0:
                    timeout_count += 1
                    raise ConnectionError("Connection timed out after 10000 ms")
                else:
                    fast_count += 1
                    raise ConnectionError("SOCKS5 auth failed")
            raise ConnectionError("direct dead")

        with patch(
            "pokemonbot.session._fetch_with_curl_cffi",
            side_effect=mock_fetch,
        ), patch(
            "pokemonbot.session._fetch_with_aiohttp",
            side_effect=mock_fetch,
        ):
            with pytest.raises(ConnectionError):
                await fetch(
                    "https://example.com",
                    proxy_pool=pool,
                    max_retries=2,
                    direct_fallback=False,
                )
            # Timeout failures should be capped at 2, fast failures > 2
            assert timeout_count == 2
            assert fast_count > timeout_count

    @pytest.mark.asyncio
    async def test_is_timeout_error_detection(self):
        """The _is_timeout_error helper correctly classifies exceptions."""
        from pokemonbot.session import _is_timeout_error

        assert _is_timeout_error(
            Exception("Connection timed out after 10000 milliseconds")
        )
        assert _is_timeout_error(
            Exception("Failed to perform, curl: (28) Timeout was reached")
        )
        assert _is_timeout_error(asyncio.TimeoutError())
        assert _is_timeout_error(TimeoutError("connection timeout"))
        assert not _is_timeout_error(
            Exception("SOCKS5 connection refused")
        )
        assert not _is_timeout_error(
            Exception("CONNECT tunnel failed, response 504")
        )
        assert not _is_timeout_error(
            Exception("TLS connect error: WRONG_VERSION_NUMBER")
        )


class TestBackendRotation:
    """Tests that fetch() alternates between curl_cffi and aiohttp backends."""

    @pytest.mark.asyncio
    async def test_both_backends_tried_on_failure(self):
        """When curl_cffi fails, the next attempt should use aiohttp."""
        from pokemonbot.session import fetch, _HAS_CURL_CFFI
        from pokemonbot.proxy import ProxyPool

        if not _HAS_CURL_CFFI:
            pytest.skip("curl_cffi not installed")

        proxies = [
            Proxy(protocol="http", host=f"10.0.0.{i}", port=8080)
            for i in range(10)
        ]
        pool = ProxyPool(proxies, cooldown_seconds=0)

        fake_response = {
            "status": 200,
            "body": "OK",
            "headers": {},
            "url": "https://example.com",
        }

        cffi_calls = 0
        aiohttp_calls = 0

        async def cffi_fail(url, **kwargs):
            nonlocal cffi_calls
            cffi_calls += 1
            raise ConnectionError("curl failed")

        async def aiohttp_succeed(url, **kwargs):
            nonlocal aiohttp_calls
            aiohttp_calls += 1
            return fake_response

        with patch(
            "pokemonbot.session._fetch_with_curl_cffi",
            side_effect=cffi_fail,
        ), patch(
            "pokemonbot.session._fetch_with_aiohttp",
            side_effect=aiohttp_succeed,
        ):
            result = await fetch(
                "https://example.com",
                proxy_pool=pool,
                max_retries=3,
            )
            assert result["status"] == 200
            assert cffi_calls >= 1
            assert aiohttp_calls >= 1

    @pytest.mark.asyncio
    async def test_direct_fallback_tries_both_backends(self):
        """Direct fallback should try each backend."""
        from pokemonbot.session import fetch, _HAS_CURL_CFFI
        from pokemonbot.proxy import ProxyPool

        if not _HAS_CURL_CFFI:
            pytest.skip("curl_cffi not installed")

        proxy = Proxy(protocol="http", host="1.2.3.4", port=8080)
        pool = ProxyPool([proxy])

        fake_response = {
            "status": 200,
            "body": "OK",
            "headers": {},
            "url": "https://example.com",
        }

        cffi_direct_called = False

        async def cffi_fail(url, *, proxy=None, **kwargs):
            nonlocal cffi_direct_called
            if proxy is None:
                cffi_direct_called = True
            raise ConnectionError("curl always fails")

        async def aiohttp_direct(url, *, proxy=None, **kwargs):
            if proxy is None:
                return fake_response
            raise ConnectionError("proxy dead")

        with patch(
            "pokemonbot.session._fetch_with_curl_cffi",
            side_effect=cffi_fail,
        ), patch(
            "pokemonbot.session._fetch_with_aiohttp",
            side_effect=aiohttp_direct,
        ):
            result = await fetch(
                "https://example.com",
                proxy_pool=pool,
                max_retries=1,
                direct_fallback=True,
            )
            assert result["status"] == 200
            # curl_cffi should have been tried for direct too
            assert cffi_direct_called


class TestRetryOnStatus:
    """Tests for the retry_on_status parameter – retries when the response
    has a status code in the retryable set (e.g. 403 from bot protection).
    """

    @pytest.mark.asyncio
    async def test_403_retried_until_success(self):
        """A proxy returning 403 should be skipped; a later proxy returning 200 wins."""
        from pokemonbot.session import fetch
        from pokemonbot.proxy import ProxyPool

        proxies = [
            Proxy(protocol="http", host=f"10.0.0.{i}", port=8080)
            for i in range(10)
        ]
        pool = ProxyPool(proxies, cooldown_seconds=0)

        call_count = 0
        success_at = 4

        async def mock_fetch(url, *, proxy=None, **kwargs):
            nonlocal call_count
            call_count += 1
            status = 403 if call_count < success_at else 200
            return {
                "status": status,
                "body": "OK" if status == 200 else "blocked",
                "headers": {},
                "url": url,
            }

        with patch(
            "pokemonbot.session._fetch_with_curl_cffi",
            side_effect=mock_fetch,
        ), patch(
            "pokemonbot.session._fetch_with_aiohttp",
            side_effect=mock_fetch,
        ):
            result = await fetch(
                "https://www.pokemoncenter.com/test",
                proxy_pool=pool,
                max_retries=2,
                retry_on_status=frozenset({403}),
            )
            assert result["status"] == 200
            assert call_count == success_at

    @pytest.mark.asyncio
    async def test_403_without_retry_on_status_returns_immediately(self):
        """Without retry_on_status, a 403 response is returned as-is."""
        from pokemonbot.session import fetch
        from pokemonbot.proxy import ProxyPool

        proxies = [
            Proxy(protocol="http", host="10.0.0.1", port=8080),
        ]
        pool = ProxyPool(proxies, cooldown_seconds=0)

        call_count = 0

        async def mock_fetch(url, *, proxy=None, **kwargs):
            nonlocal call_count
            call_count += 1
            return {
                "status": 403,
                "body": "blocked",
                "headers": {},
                "url": url,
            }

        with patch(
            "pokemonbot.session._fetch_with_curl_cffi",
            side_effect=mock_fetch,
        ), patch(
            "pokemonbot.session._fetch_with_aiohttp",
            side_effect=mock_fetch,
        ):
            result = await fetch(
                "https://www.pokemoncenter.com/test",
                proxy_pool=pool,
                max_retries=3,
                # No retry_on_status → 403 returned immediately
            )
            assert result["status"] == 403
            assert call_count == 1

    @pytest.mark.asyncio
    async def test_all_proxies_return_403_returns_last_result(self):
        """When every proxy returns 403 and retries are exhausted, return
        the last 403 response (don't raise ConnectionError).
        """
        from pokemonbot.session import fetch
        from pokemonbot.proxy import ProxyPool

        proxies = [
            Proxy(protocol="http", host=f"10.0.0.{i}", port=8080)
            for i in range(5)
        ]
        pool = ProxyPool(proxies, cooldown_seconds=0)

        async def mock_fetch(url, *, proxy=None, **kwargs):
            return {
                "status": 403,
                "body": "blocked",
                "headers": {},
                "url": url,
            }

        with patch(
            "pokemonbot.session._fetch_with_curl_cffi",
            side_effect=mock_fetch,
        ), patch(
            "pokemonbot.session._fetch_with_aiohttp",
            side_effect=mock_fetch,
        ):
            result = await fetch(
                "https://www.pokemoncenter.com/test",
                proxy_pool=pool,
                max_retries=2,
                retry_on_status=frozenset({403}),
                direct_fallback=False,
            )
            # Should return the 403 response, not raise ConnectionError
            assert result["status"] == 403

    @pytest.mark.asyncio
    async def test_retry_on_status_only_applies_to_proxied_requests(self):
        """Direct (non-proxy) requests should not be retried on status code."""
        from pokemonbot.session import fetch

        call_count = 0

        async def mock_fetch(url, *, proxy=None, **kwargs):
            nonlocal call_count
            call_count += 1
            return {
                "status": 403,
                "body": "blocked",
                "headers": {},
                "url": url,
            }

        with patch(
            "pokemonbot.session._fetch_with_curl_cffi",
            side_effect=mock_fetch,
        ), patch(
            "pokemonbot.session._fetch_with_aiohttp",
            side_effect=mock_fetch,
        ):
            result = await fetch(
                "https://www.pokemoncenter.com/test",
                # No proxy_pool → direct request
                max_retries=3,
                retry_on_status=frozenset({403}),
            )
            assert result["status"] == 403
            assert call_count == 1


class TestPlaywrightSupport:
    """Tests for the Playwright browser-based fetch."""

    def test_playwright_importable(self):
        """Playwright should be importable when installed."""
        from pokemonbot.session import _HAS_PLAYWRIGHT

        assert _HAS_PLAYWRIGHT is True

    @pytest.mark.asyncio
    async def test_fetch_with_browser_falls_back_when_no_playwright(self):
        """When _HAS_PLAYWRIGHT is False, fetch_with_browser falls back to fetch()."""
        from pokemonbot.session import fetch_with_browser

        fake_response = {
            "status": 200,
            "body": "OK",
            "headers": {},
            "url": "https://example.com",
        }

        with patch(
            "pokemonbot.session._HAS_PLAYWRIGHT",
            False,
        ), patch(
            "pokemonbot.session.fetch",
            new_callable=AsyncMock,
            return_value=fake_response,
        ) as mock_fetch:
            result = await fetch_with_browser("https://example.com")
            assert result["status"] == 200
            mock_fetch.assert_called_once()

    @pytest.mark.asyncio
    async def test_close_browser_no_error_when_not_started(self):
        """close_browser() should not raise even if no browser is running."""
        from pokemonbot.session import close_browser

        # Should be a no-op when browser hasn't been started.
        await close_browser()

    def test_browser_sites_includes_pokemoncenter(self):
        """pokemoncenter should be in the _BROWSER_SITES set."""
        from pokemonbot.tasks import _BROWSER_SITES

        assert "pokemoncenter" in _BROWSER_SITES

    @pytest.mark.asyncio
    async def test_fetch_with_browser_passes_proxy_pool(self):
        """fetch_with_browser() should pass proxy to _browser_fetch_once."""
        from pokemonbot.session import fetch_with_browser
        from pokemonbot.proxy import ProxyPool

        proxy = Proxy(protocol="http", host="1.2.3.4", port=8080)
        pool = ProxyPool([proxy])

        fake_response = {
            "status": 200,
            "body": "OK",
            "headers": {},
            "url": "https://example.com",
        }

        with patch(
            "pokemonbot.session._HAS_PLAYWRIGHT",
            True,
        ), patch(
            "pokemonbot.session._browser_fetch_once",
            new_callable=AsyncMock,
            return_value=fake_response,
        ) as mock_once:
            result = await fetch_with_browser(
                "https://example.com",
                proxy_pool=pool,
            )
            assert result["status"] == 200
            mock_once.assert_called_once()
            _, kwargs = mock_once.call_args
            assert kwargs["proxy"] is not None

    @pytest.mark.asyncio
    async def test_fetch_with_browser_retries_on_403(self):
        """fetch_with_browser() retries with another proxy on 403."""
        from pokemonbot.session import fetch_with_browser
        from pokemonbot.proxy import ProxyPool

        proxy1 = Proxy(protocol="http", host="1.1.1.1", port=8080)
        proxy2 = Proxy(protocol="http", host="2.2.2.2", port=8080)
        pool = ProxyPool([proxy1, proxy2], shuffle=False)

        call_count = 0

        async def mock_once(url, *, proxy=None, timeout=30.0, extra_headers=None):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return {"status": 403, "body": "Forbidden", "headers": {}, "url": url}
            return {"status": 200, "body": "OK", "headers": {}, "url": url}

        with patch(
            "pokemonbot.session._HAS_PLAYWRIGHT",
            True,
        ), patch(
            "pokemonbot.session._browser_fetch_once",
            side_effect=mock_once,
        ):
            result = await fetch_with_browser(
                "https://example.com",
                proxy_pool=pool,
                max_retries=3,
                retry_on_status=frozenset({403}),
            )
            assert result["status"] == 200
            assert call_count == 2

    @pytest.mark.asyncio
    async def test_fetch_with_browser_raises_after_max_retries(self):
        """fetch_with_browser() raises ConnectionError after max_retries."""
        from pokemonbot.session import fetch_with_browser
        from pokemonbot.proxy import ProxyPool

        proxy = Proxy(protocol="http", host="1.2.3.4", port=8080)
        pool = ProxyPool([proxy])

        with patch(
            "pokemonbot.session._HAS_PLAYWRIGHT",
            True,
        ), patch(
            "pokemonbot.session._browser_fetch_once",
            new_callable=AsyncMock,
            side_effect=ConnectionError("browser error"),
        ):
            with pytest.raises(ConnectionError):
                await fetch_with_browser(
                    "https://example.com",
                    proxy_pool=pool,
                    max_retries=2,
                )

    @pytest.mark.asyncio
    async def test_fetch_with_browser_direct_fallback(self):
        """When all proxy attempts fail and direct_fallback=True, try without proxy."""
        from pokemonbot.session import fetch_with_browser
        from pokemonbot.proxy import ProxyPool

        proxy = Proxy(protocol="http", host="1.2.3.4", port=8080)
        pool = ProxyPool([proxy])

        call_count = 0

        async def mock_once(url, *, proxy=None, timeout=30.0, extra_headers=None):
            nonlocal call_count
            call_count += 1
            if proxy is not None:
                raise ConnectionError("proxy error")
            return {"status": 200, "body": "OK", "headers": {}, "url": url}

        with patch(
            "pokemonbot.session._HAS_PLAYWRIGHT",
            True,
        ), patch(
            "pokemonbot.session._browser_fetch_once",
            side_effect=mock_once,
        ):
            result = await fetch_with_browser(
                "https://example.com",
                proxy_pool=pool,
                max_retries=1,
                direct_fallback=True,
            )
            assert result["status"] == 200
            # 1 proxied attempt + 1 direct fallback
            assert call_count == 2

    @pytest.mark.asyncio
    async def test_fetch_with_browser_no_direct_fallback(self):
        """When direct_fallback=False, do not try direct connection after proxy failures."""
        from pokemonbot.session import fetch_with_browser
        from pokemonbot.proxy import ProxyPool

        proxy = Proxy(protocol="http", host="1.2.3.4", port=8080)
        pool = ProxyPool([proxy])

        with patch(
            "pokemonbot.session._HAS_PLAYWRIGHT",
            True,
        ), patch(
            "pokemonbot.session._browser_fetch_once",
            new_callable=AsyncMock,
            side_effect=ConnectionError("proxy error"),
        ) as mock_once:
            with pytest.raises(ConnectionError):
                await fetch_with_browser(
                    "https://example.com",
                    proxy_pool=pool,
                    max_retries=1,
                    direct_fallback=False,
                )
            # Only 1 proxied attempt, no direct fallback
            assert mock_once.call_count == 1

    @pytest.mark.asyncio
    async def test_fetch_with_browser_fast_fail_budget(self):
        """Fast proxy failures (non-timeout) should not count toward max_retries."""
        from pokemonbot.session import fetch_with_browser
        from pokemonbot.proxy import ProxyPool

        proxies = [Proxy(protocol="http", host=f"10.0.0.{i}", port=8080) for i in range(5)]
        pool = ProxyPool(proxies, shuffle=False)

        call_count = 0

        async def mock_once(url, *, proxy=None, timeout=30.0, extra_headers=None):
            nonlocal call_count
            call_count += 1
            if call_count <= 3:
                # Fast failures (SOCKS/CONNECT) – should not exhaust max_retries
                raise ConnectionError("net::ERR_PROXY_CONNECTION_FAILED")
            return {"status": 200, "body": "OK", "headers": {}, "url": url}

        with patch(
            "pokemonbot.session._HAS_PLAYWRIGHT",
            True,
        ), patch(
            "pokemonbot.session._browser_fetch_once",
            side_effect=mock_once,
        ):
            result = await fetch_with_browser(
                "https://example.com",
                proxy_pool=pool,
                max_retries=2,  # Only 2 timeout retries allowed
            )
            # Should succeed because fast failures don't count
            assert result["status"] == 200
            assert call_count == 4

    @pytest.mark.asyncio
    async def test_fetch_with_browser_timeout_exhausts_budget(self):
        """Timeout errors should count toward max_retries and stop the loop."""
        from pokemonbot.session import fetch_with_browser
        from pokemonbot.proxy import ProxyPool

        proxies = [Proxy(protocol="http", host=f"10.0.0.{i}", port=8080) for i in range(5)]
        pool = ProxyPool(proxies, shuffle=False)

        with patch(
            "pokemonbot.session._HAS_PLAYWRIGHT",
            True,
        ), patch(
            "pokemonbot.session._browser_fetch_once",
            new_callable=AsyncMock,
            side_effect=ConnectionError("Page.goto: Timeout 10000ms exceeded."),
        ) as mock_once:
            with pytest.raises(ConnectionError):
                await fetch_with_browser(
                    "https://example.com",
                    proxy_pool=pool,
                    max_retries=2,
                    direct_fallback=False,
                )
            # Only 2 attempts because timeouts count toward max_retries
            assert mock_once.call_count == 2

    @pytest.mark.asyncio
    async def test_fetch_with_browser_logs_success(self, caplog):
        """fetch_with_browser() should log at INFO when a fetch succeeds."""
        import logging
        from pokemonbot.session import fetch_with_browser
        from pokemonbot.proxy import ProxyPool

        proxy = Proxy(protocol="http", host="1.2.3.4", port=8080)
        pool = ProxyPool([proxy])

        fake_response = {
            "status": 200,
            "body": "OK",
            "headers": {},
            "url": "https://example.com",
        }

        with patch(
            "pokemonbot.session._HAS_PLAYWRIGHT",
            True,
        ), patch(
            "pokemonbot.session._browser_fetch_once",
            new_callable=AsyncMock,
            return_value=fake_response,
        ):
            with caplog.at_level(logging.INFO, logger="pokemonbot.session"):
                result = await fetch_with_browser(
                    "https://example.com",
                    proxy_pool=pool,
                )
            assert result["status"] == 200
            assert any(
                "Browser fetch OK" in r.message and r.levelno == logging.INFO
                for r in caplog.records
            )

    @pytest.mark.asyncio
    async def test_fetch_with_browser_returns_last_retryable_result(self):
        """When all proxies return a retryable status and no hard error, return the last result."""
        from pokemonbot.session import fetch_with_browser
        from pokemonbot.proxy import ProxyPool

        proxies = [Proxy(protocol="http", host=f"10.0.0.{i}", port=8080) for i in range(3)]
        pool = ProxyPool(proxies, shuffle=False)

        async def mock_once(url, *, proxy=None, timeout=30.0, extra_headers=None):
            return {"status": 403, "body": "Forbidden", "headers": {}, "url": url}

        with patch(
            "pokemonbot.session._HAS_PLAYWRIGHT",
            True,
        ), patch(
            "pokemonbot.session._browser_fetch_once",
            side_effect=mock_once,
        ):
            result = await fetch_with_browser(
                "https://example.com",
                proxy_pool=pool,
                max_retries=3,
                direct_fallback=False,
                retry_on_status=frozenset({403}),
            )
            # Returns the last 403 response instead of raising ConnectionError
            assert result["status"] == 403
