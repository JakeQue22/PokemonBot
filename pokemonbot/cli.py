"""Command-line interface for PokemonBot."""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
from pathlib import Path

import click
from rich.console import Console
from rich.logging import RichHandler
from rich.table import Table

from pokemonbot import __version__
from pokemonbot.config import AppConfig, MonitorConfig, NotifierConfig, ProxyConfig, load_config
from pokemonbot.monitor import MONITOR_REGISTRY
from pokemonbot.notifier import ConsoleNotifier, DiscordWebhookNotifier, NotifierPipeline
from pokemonbot.proxy import ProxyPool, load_proxies
from pokemonbot.tasks import TaskManager

console = Console()


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(message)s",
        datefmt="[%X]",
        handlers=[RichHandler(rich_tracebacks=True, show_path=False)],
    )


@click.group()
@click.version_option(__version__, prog_name="pokemonbot")
def main() -> None:
    """PokemonBot – Product availability monitor with proxy rotation."""


@main.command()
@click.option("-c", "--config", "config_path", default="config.yaml", help="Path to config YAML.")
@click.option("-v", "--verbose", is_flag=True, help="Enable debug logging.")
def run(config_path: str, verbose: bool) -> None:
    """Start monitoring configured products."""
    _setup_logging(verbose)
    logger = logging.getLogger("pokemonbot")

    path = Path(config_path)
    if not path.exists():
        console.print(f"[red]Config file not found:[/red] {config_path}")
        console.print("Run [bold]pokemonbot init[/bold] to create a starter config.")
        sys.exit(1)

    cfg = load_config(path)

    # Load proxies
    proxy_pool: ProxyPool | None = None
    proxy_path = Path(cfg.proxies.file)
    if proxy_path.exists():
        proxies = load_proxies(proxy_path)
        if proxies:
            proxy_pool = ProxyPool(proxies)
            logger.info("Loaded %d proxies from %s", proxy_pool.size, proxy_path)
    else:
        logger.info("No proxy file found at %s – running without proxies.", proxy_path)

    # Build notifier pipeline
    notifier = NotifierPipeline()
    if cfg.notifier.console:
        notifier.add(ConsoleNotifier())
    if cfg.notifier.discord_webhook_url:
        notifier.add(DiscordWebhookNotifier(cfg.notifier.discord_webhook_url))

    if not cfg.monitors:
        console.print("[yellow]No monitors configured in the config file.[/yellow]")
        sys.exit(1)

    console.print(
        f"[bold cyan]PokemonBot v{__version__}[/bold cyan] – "
        f"Starting {len(cfg.monitors)} monitor(s)…"
    )

    manager = TaskManager(
        app_config=cfg,
        proxy_pool=proxy_pool,
        notifier=notifier,
    )

    loop = asyncio.new_event_loop()

    def _handle_signal() -> None:
        console.print("\n[yellow]Shutting down…[/yellow]")
        manager.stop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _handle_signal)

    try:
        loop.run_until_complete(manager.run())
    finally:
        loop.close()


@main.command()
@click.option("-o", "--output", default="config.yaml", help="Output path for the config file.")
def init(output: str) -> None:
    """Generate a starter configuration file."""
    template = """\
# PokemonBot Configuration
# ========================

# Proxy settings
proxies:
  file: proxies.txt          # One proxy per line
  rotate_on_error: true
  test_url: https://httpbin.org/ip
  timeout: 10.0

# Notification settings
notifier:
  console: true
  discord_webhook_url: ""    # Paste your Discord webhook URL here

# Global settings
concurrency: 10
request_timeout: 30.0

# Monitor tasks – add as many as you like
monitors:
  - name: "Pokemon Center - Ascending Heroes"
    url: "https://www.pokemoncenter.com/category/trading-card-game"
    site: pokemoncenter
    keywords:
      - "Ascending Heroes"
    interval: 10.0

  # Example: generic site monitor
  # - name: "Target Pokemon Cards"
  #   url: "https://www.target.com/c/pokemon-trading-cards/-/N-5xt8a"
  #   site: generic
  #   keywords:
  #     - "pokemon"
  #     - "ascending heroes"
  #   interval: 15.0
"""
    path = Path(output)
    if path.exists():
        console.print(f"[yellow]Config file already exists:[/yellow] {output}")
        return
    path.write_text(template)
    console.print(f"[green]Created starter config:[/green] {output}")
    console.print("Edit the file, then run [bold]pokemonbot run[/bold]")


@main.command()
def sites() -> None:
    """List supported site monitors."""
    table = Table(title="Supported Site Monitors")
    table.add_column("Site Key", style="cyan")
    table.add_column("Class", style="green")
    for key, cls in MONITOR_REGISTRY.items():
        table.add_row(key, cls.__name__)
    console.print(table)


@main.command()
@click.argument("url")
@click.option("-s", "--site", default="generic", help="Site monitor to use.")
@click.option("-k", "--keyword", multiple=True, help="Keywords to filter.")
@click.option("-v", "--verbose", is_flag=True, help="Enable debug logging.")
def check(url: str, site: str, keyword: tuple[str, ...], verbose: bool) -> None:
    """Perform a single check on a URL and display the result."""
    _setup_logging(verbose)

    from pokemonbot.monitor import get_monitor
    from pokemonbot.session import fetch

    monitor = get_monitor(site)
    console.print(f"Checking [bold]{url}[/bold] with [cyan]{site}[/cyan] monitor…")

    async def _check() -> None:
        try:
            response = await fetch(url, max_retries=1, timeout=15.0)
        except ConnectionError as exc:
            console.print(f"[red]Connection error:[/red] {exc}")
            return

        alert = monitor.parse(response, url=url, keywords=list(keyword))
        if alert:
            console.print(f"[bold green]✓ Alert:[/bold green] {alert.status} – {alert.product_name}")
            if alert.price:
                console.print(f"  Price: {alert.price}")
        else:
            console.print("[dim]No stock/queue signal detected.[/dim]")

    asyncio.run(_check())


if __name__ == "__main__":
    main()
