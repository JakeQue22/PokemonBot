"""Configuration loading and validation."""

from __future__ import annotations

import errno
import logging
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)


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
    max_retries: int = 10
    portal_name: str = "PokemonBot"
    base_url: str = ""


_DEFAULT_USER_AGENTS: list[str] = [
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/130.0.0.0 Safari/537.36"
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
    max_retries = int(raw.get("max_retries", 10))

    return AppConfig(
        proxies=proxies,
        monitors=monitors,
        notifier=notifier,
        email=email,
        user_agents=user_agents,
        concurrency=concurrency,
        request_timeout=request_timeout,
        max_retries=max_retries,
        portal_name=str(raw.get("portal_name", "PokemonBot")),
        base_url=str(raw.get("base_url", "")),
    )


def _config_to_dict(cfg: AppConfig) -> dict[str, Any]:
    """Convert an ``AppConfig`` to a plain dict suitable for YAML serialisation.

    Omits keys whose values match the dataclass defaults to keep the file
    compact and readable.
    """
    data: dict[str, Any] = {}

    # Proxies
    proxy = asdict(cfg.proxies)
    defaults = asdict(ProxyConfig())
    proxy_out = {k: v for k, v in proxy.items() if v != defaults.get(k)}
    if proxy_out:
        data["proxies"] = proxy_out

    # Notifier
    notif = asdict(cfg.notifier)
    defaults_n = asdict(NotifierConfig())
    notif_out = {k: v for k, v in notif.items() if v != defaults_n.get(k)}
    if notif_out:
        data["notifier"] = notif_out

    # Email
    email = asdict(cfg.email)
    defaults_e = asdict(EmailConfig())
    email_out = {k: v for k, v in email.items() if v != defaults_e.get(k)}
    if email_out:
        data["email"] = email_out

    # Scalar settings – only write non-default values
    if cfg.concurrency != 10:
        data["concurrency"] = cfg.concurrency
    if cfg.request_timeout != 30.0:
        data["request_timeout"] = cfg.request_timeout
    if cfg.max_retries != 10:
        data["max_retries"] = cfg.max_retries
    if cfg.portal_name and cfg.portal_name != "PokemonBot":
        data["portal_name"] = cfg.portal_name
    if cfg.base_url:
        data["base_url"] = cfg.base_url
    if cfg.user_agents != _DEFAULT_USER_AGENTS:
        data["user_agents"] = cfg.user_agents

    # Monitors
    monitors = []
    for m in cfg.monitors:
        md: dict[str, Any] = {"name": m.name, "url": m.url, "site": m.site}
        if m.keywords:
            md["keywords"] = m.keywords
        md["interval"] = m.interval
        if m.headers:
            md["headers"] = m.headers
        monitors.append(md)
    data["monitors"] = monitors

    return data


def save_config(cfg: AppConfig, path: str | Path) -> None:
    """Persist *cfg* to a YAML file at *path* using an atomic write."""
    dest = Path(path)
    data = _config_to_dict(cfg)
    content = yaml.dump(data, default_flow_style=False, sort_keys=False, allow_unicode=True)

    # Atomic write: temp file in the same directory, then rename.
    # Falls back to a direct (non-atomic) write when the destination is a
    # Docker bind-mount and os.replace() raises EBUSY (errno 16).
    fd, tmp = tempfile.mkstemp(
        dir=str(dest.parent), prefix=".config_", suffix=".tmp",
    )
    try:
        os.write(fd, content.encode())
    finally:
        os.close(fd)
    try:
        os.replace(tmp, str(dest))
    except OSError as exc:
        # EBUSY – the target file is a Docker bind-mount; fall back to a
        # direct overwrite of the existing file contents.
        try:
            os.unlink(tmp)
        except OSError:
            pass
        if exc.errno == errno.EBUSY:
            dest.write_text(content, encoding="utf-8")
        else:
            raise
    logger.info("Configuration saved to %s", dest)
