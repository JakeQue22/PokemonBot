"""Notification delivery – console, Discord webhook, and SMTP email."""

from __future__ import annotations

import email.mime.multipart
import email.mime.text
import json
import logging
import smtplib
import ssl
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import aiohttp

from pokemonbot.config import EmailConfig

logger = logging.getLogger(__name__)


@dataclass
class Alert:
    """A product availability alert."""

    product_name: str
    url: str
    status: str  # e.g. "in_stock", "queue_active"
    site: str = ""
    price: str = ""
    image_url: str = ""
    timestamp: str = ""

    def __post_init__(self) -> None:
        if not self.timestamp:
            self.timestamp = datetime.now(timezone.utc).isoformat()


class ConsoleNotifier:
    """Prints alerts to the console via the ``rich`` library."""

    def __init__(self) -> None:
        try:
            from rich.console import Console

            self._console = Console()
        except ImportError:
            self._console = None

    async def send(self, alert: Alert) -> None:
        msg = (
            f"[bold green]🔔 {alert.status.upper()}[/bold green] "
            f"[bold]{alert.product_name}[/bold]  {alert.url}"
        )
        if self._console:
            self._console.print(msg)
        else:
            print(f"🔔 {alert.status.upper()} – {alert.product_name}  {alert.url}")


class DiscordWebhookNotifier:
    """Sends alerts to a Discord channel via webhook."""

    def __init__(self, webhook_url: str) -> None:
        self._webhook_url = webhook_url

    async def send(self, alert: Alert) -> None:
        if not self._webhook_url:
            return
        embed = {
            "title": f"🔔 {alert.product_name}",
            "url": alert.url,
            "description": f"Status: **{alert.status}**",
            "color": 0x00FF00 if alert.status == "in_stock" else 0xFFFF00,
            "fields": [],
            "timestamp": alert.timestamp,
        }
        if alert.price:
            embed["fields"].append({"name": "Price", "value": alert.price, "inline": True})
        if alert.site:
            embed["fields"].append({"name": "Site", "value": alert.site, "inline": True})
        if alert.image_url:
            embed["thumbnail"] = {"url": alert.image_url}

        payload = {"embeds": [embed]}
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    self._webhook_url,
                    data=json.dumps(payload),
                    headers={"Content-Type": "application/json"},
                ) as resp:
                    if resp.status >= 400:
                        logger.warning("Discord webhook returned %d", resp.status)
        except Exception as exc:
            logger.warning("Failed to send Discord notification: %s", exc)


class EmailNotifier:
    """Sends alerts via SMTP email."""

    def __init__(self, config: EmailConfig) -> None:
        self._config = config

    async def send(self, alert: Alert) -> None:
        """Send an alert email.  Runs the blocking SMTP call in an executor."""
        if not self._config.enabled or not self._config.to_addresses:
            return
        import asyncio

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._send_sync, alert)

    def _send_sync(self, alert: Alert) -> None:
        cfg = self._config
        subject = f"🔔 PokemonBot Alert: {alert.status.upper()} – {alert.product_name}"
        body_lines = [
            f"Product: {alert.product_name}",
            f"Status:  {alert.status}",
            f"URL:     {alert.url}",
        ]
        if alert.price:
            body_lines.append(f"Price:   {alert.price}")
        if alert.site:
            body_lines.append(f"Site:    {alert.site}")
        body_lines.append(f"Time:    {alert.timestamp}")

        msg = email.mime.multipart.MIMEMultipart()
        msg["From"] = cfg.from_address
        msg["To"] = ", ".join(cfg.to_addresses)
        msg["Subject"] = subject
        msg.attach(email.mime.text.MIMEText("\n".join(body_lines), "plain"))

        try:
            if cfg.use_ssl:
                context = ssl.create_default_context()
                with smtplib.SMTP_SSL(cfg.smtp_host, cfg.smtp_port, context=context) as server:
                    if cfg.username:
                        server.login(cfg.username, cfg.password)
                    server.sendmail(cfg.from_address, cfg.to_addresses, msg.as_string())
            else:
                with smtplib.SMTP(cfg.smtp_host, cfg.smtp_port) as server:
                    server.ehlo()
                    if cfg.username:
                        server.starttls()
                        server.login(cfg.username, cfg.password)
                    server.sendmail(cfg.from_address, cfg.to_addresses, msg.as_string())
            logger.info("Email alert sent to %s", cfg.to_addresses)
        except Exception as exc:
            logger.warning("Failed to send email notification: %s", exc)

    @staticmethod
    def send_test_email(config: EmailConfig) -> str:
        """Send a test email synchronously.  Returns 'ok' or an error string."""
        if not config.smtp_host:
            return "SMTP host is not configured"
        if not config.to_addresses:
            return "No recipient addresses configured"

        msg = email.mime.multipart.MIMEMultipart()
        msg["From"] = config.from_address
        msg["To"] = ", ".join(config.to_addresses)
        msg["Subject"] = "PokemonBot – Test Email"
        msg.attach(
            email.mime.text.MIMEText(
                "This is a test email from PokemonBot.\n"
                "If you received this, your SMTP settings are correct!",
                "plain",
            )
        )
        try:
            if config.use_ssl:
                context = ssl.create_default_context()
                with smtplib.SMTP_SSL(config.smtp_host, config.smtp_port, context=context) as srv:
                    if config.username:
                        srv.login(config.username, config.password)
                    srv.sendmail(config.from_address, config.to_addresses, msg.as_string())
            else:
                with smtplib.SMTP(config.smtp_host, config.smtp_port) as srv:
                    srv.ehlo()
                    if config.username:
                        srv.starttls()
                        srv.login(config.username, config.password)
                    srv.sendmail(config.from_address, config.to_addresses, msg.as_string())
            return "ok"
        except Exception as exc:
            return str(exc)


Notifier = ConsoleNotifier | DiscordWebhookNotifier | EmailNotifier


class NotifierPipeline:
    """Fan-out to multiple notifiers."""

    def __init__(self) -> None:
        self._notifiers: list[Any] = []

    def add(self, notifier: Any) -> None:
        self._notifiers.append(notifier)

    async def send(self, alert: Alert) -> None:
        for n in self._notifiers:
            try:
                await n.send(alert)
            except Exception as exc:
                logger.warning("Notifier %s failed: %s", type(n).__name__, exc)
