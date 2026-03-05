"""Notification delivery – console + Discord webhook."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import aiohttp

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


class NotifierPipeline:
    """Fan-out to multiple notifiers."""

    def __init__(self) -> None:
        self._notifiers: list[ConsoleNotifier | DiscordWebhookNotifier] = []

    def add(self, notifier: ConsoleNotifier | DiscordWebhookNotifier) -> None:
        self._notifiers.append(notifier)

    async def send(self, alert: Alert) -> None:
        for n in self._notifiers:
            try:
                await n.send(alert)
            except Exception as exc:
                logger.warning("Notifier %s failed: %s", type(n).__name__, exc)
