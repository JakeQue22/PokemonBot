"""Tests for the EmailNotifier and email config."""

from unittest.mock import MagicMock, patch

import pytest

from pokemonbot.config import EmailConfig
from pokemonbot.notifier import Alert, EmailNotifier


class TestEmailNotifier:
    def _make_alert(self) -> Alert:
        return Alert(
            product_name="Test Card",
            url="https://example.com/card",
            status="in_stock",
            site="pokemoncenter",
            price="$49.99",
        )

    def test_send_test_email_no_host(self):
        cfg = EmailConfig(enabled=True, smtp_host="", to_addresses=["a@b.com"])
        result = EmailNotifier.send_test_email(cfg)
        assert result == "SMTP host is not configured"

    def test_send_test_email_no_recipients(self):
        cfg = EmailConfig(enabled=True, smtp_host="smtp.test.com", to_addresses=[])
        result = EmailNotifier.send_test_email(cfg)
        assert result == "No recipient addresses configured"

    @patch("pokemonbot.notifier.smtplib.SMTP_SSL")
    def test_send_test_email_ssl_success(self, mock_smtp_cls):
        mock_server = MagicMock()
        mock_smtp_cls.return_value.__enter__ = MagicMock(return_value=mock_server)
        mock_smtp_cls.return_value.__exit__ = MagicMock(return_value=False)

        cfg = EmailConfig(
            enabled=True,
            smtp_host="smtp.test.com",
            smtp_port=465,
            username="user",
            password="pass",
            use_ssl=True,
            from_address="bot@test.com",
            to_addresses=["a@b.com"],
        )
        result = EmailNotifier.send_test_email(cfg)
        assert result == "ok"
        mock_server.login.assert_called_once_with("user", "pass")
        mock_server.sendmail.assert_called_once()

    @patch("pokemonbot.notifier.smtplib.SMTP")
    def test_send_test_email_no_ssl_success(self, mock_smtp_cls):
        mock_server = MagicMock()
        mock_smtp_cls.return_value.__enter__ = MagicMock(return_value=mock_server)
        mock_smtp_cls.return_value.__exit__ = MagicMock(return_value=False)

        cfg = EmailConfig(
            enabled=True,
            smtp_host="smtp.test.com",
            smtp_port=587,
            username="user",
            password="pass",
            use_ssl=False,
            from_address="bot@test.com",
            to_addresses=["a@b.com"],
        )
        result = EmailNotifier.send_test_email(cfg)
        assert result == "ok"
        mock_server.starttls.assert_called_once()

    @patch("pokemonbot.notifier.smtplib.SMTP")
    def test_send_test_email_no_ssl_no_auth_still_starttls(self, mock_smtp_cls):
        mock_server = MagicMock()
        mock_smtp_cls.return_value.__enter__ = MagicMock(return_value=mock_server)
        mock_smtp_cls.return_value.__exit__ = MagicMock(return_value=False)

        cfg = EmailConfig(
            enabled=True,
            smtp_host="smtp.test.com",
            smtp_port=587,
            username="",
            password="",
            use_ssl=False,
            from_address="bot@test.com",
            to_addresses=["a@b.com"],
        )
        result = EmailNotifier.send_test_email(cfg)
        assert result == "ok"
        mock_server.starttls.assert_called_once()
        mock_server.login.assert_not_called()

    @patch("pokemonbot.notifier.smtplib.SMTP_SSL")
    def test_send_test_email_failure(self, mock_smtp_cls):
        mock_smtp_cls.side_effect = Exception("Connection refused")
        cfg = EmailConfig(
            enabled=True,
            smtp_host="smtp.test.com",
            smtp_port=465,
            use_ssl=True,
            from_address="bot@test.com",
            to_addresses=["a@b.com"],
        )
        result = EmailNotifier.send_test_email(cfg)
        assert "Connection refused" in result

    @pytest.mark.asyncio
    async def test_send_disabled(self):
        cfg = EmailConfig(enabled=False)
        notifier = EmailNotifier(cfg)
        # Should not raise even when disabled
        await notifier.send(self._make_alert())

    @pytest.mark.asyncio
    @patch("pokemonbot.notifier.smtplib.SMTP_SSL")
    async def test_send_async(self, mock_smtp_cls):
        mock_server = MagicMock()
        mock_smtp_cls.return_value.__enter__ = MagicMock(return_value=mock_server)
        mock_smtp_cls.return_value.__exit__ = MagicMock(return_value=False)

        cfg = EmailConfig(
            enabled=True,
            smtp_host="smtp.test.com",
            smtp_port=465,
            use_ssl=True,
            from_address="bot@test.com",
            to_addresses=["a@b.com"],
        )
        notifier = EmailNotifier(cfg)
        await notifier.send(self._make_alert())
        mock_server.sendmail.assert_called_once()
