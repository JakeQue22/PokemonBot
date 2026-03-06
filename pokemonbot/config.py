"""Configuration loading and validation."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class ProxyConfig:
    """Proxy-related settings."""

    file: str = "proxies.txt"
    rotate_on_error: bool = True
    rotate_every_n_requests: int = 0
    test_url: str = "https://httpbin.org/ip"
    timeout: float = 10.0


@dataclass
class MonitorConfig:
    """Settings for a single monitor task."""

    name: str = ""
    url: str = ""
    site: str = "generic"
    keywords: list[str] = field(default_factory=list)
    interval: float = 5.0
    headers: dict[str, str] = field(default_factory=dict)


@dataclass
class NotifierConfig:
    """Notification settings."""

    discord_webhook_url: str = ""
    console: bool = True


@dataclass
class EmailConfig:
    """SMTP email notification settings."""

    enabled: bool = False
    smtp_host: str = ""
    smtp_port: int = 587
    username: str = ""
    password: str = ""
    use_ssl: bool = True
    from_address: str = ""
    to_addresses: list[str] = field(default_factory=list)


@dataclass
class AppConfig:
    """Top-level application configuration."""

    proxies: ProxyConfig = field(default_factory=ProxyConfig)
    monitors: list[MonitorConfig] = field(default_factory=list)
    notifier: NotifierConfig = field(default_factory=NotifierConfig)
    email: EmailConfig = field(default_factory=EmailConfig)
    user_agents: list[str] = field(default_factory=lambda: _DEFAULT_USER_AGENTS.copy())
    concurrency: int = 10
    request_timeout: float = 30.0
    portal_name: str = "PokemonBot"
    base_url: str = ""


_DEFAULT_USER_AGENTS: list[str] = [
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) "
        "Version/17.4 Safari/605.1.15"
    ),
    (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) "
        "Gecko/20100101 Firefox/125.0"
    ),
]


def _build_proxy_config(raw: dict[str, Any]) -> ProxyConfig:
    return ProxyConfig(**{k: v for k, v in raw.items() if k in ProxyConfig.__dataclass_fields__})


def _build_monitor_config(raw: dict[str, Any]) -> MonitorConfig:
    return MonitorConfig(**{k: v for k, v in raw.items() if k in MonitorConfig.__dataclass_fields__})


def _build_notifier_config(raw: dict[str, Any]) -> NotifierConfig:
    return NotifierConfig(**{k: v for k, v in raw.items() if k in NotifierConfig.__dataclass_fields__})


def _build_email_config(raw: dict[str, Any]) -> EmailConfig:
    cfg = {k: v for k, v in raw.items() if k in EmailConfig.__dataclass_fields__}
    return EmailConfig(**cfg)


def load_config(path: str | Path) -> AppConfig:
    """Load and validate a YAML configuration file.

    Environment variables in the form ``${VAR_NAME}`` are expanded before
    parsing.
    """
    text = Path(path).read_text()
    # Expand environment variables
    text = os.path.expandvars(text)
    raw: dict[str, Any] = yaml.safe_load(text) or {}

    proxies = _build_proxy_config(raw.get("proxies", {}))
    monitors = [_build_monitor_config(m) for m in raw.get("monitors", [])]
    notifier = _build_notifier_config(raw.get("notifier", {}))
    email = _build_email_config(raw.get("email", {}))
    user_agents = raw.get("user_agents", _DEFAULT_USER_AGENTS.copy())
    concurrency = int(raw.get("concurrency", 10))
    request_timeout = float(raw.get("request_timeout", 30.0))

    return AppConfig(
        proxies=proxies,
        monitors=monitors,
        notifier=notifier,
        email=email,
        user_agents=user_agents,
        concurrency=concurrency,
        request_timeout=request_timeout,
        portal_name=str(raw.get("portal_name", "PokemonBot")),
        base_url=str(raw.get("base_url", "")),
    )
