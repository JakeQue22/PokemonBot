"""Task manager – runs multiple monitors concurrently."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from pokemonbot.config import AppConfig, MonitorConfig
from pokemonbot.monitor import get_monitor
from pokemonbot.notifier import Alert, NotifierPipeline
from pokemonbot.proxy import ProxyPool
from pokemonbot.session import (
    _HAS_PLAYWRIGHT,
    close_browser,
    fetch,
    fetch_with_browser,
)

logger = logging.getLogger(__name__)

# Maximum characters of the page body to log when stock data is missing.
_DEBUG_SNIPPET_LENGTH = 500

# Sites protected by Akamai / Cloudflare may return 403 from one
# proxy but succeed from another.  We retry on these status codes
# for known bot-protected sites.
_SITE_RETRY_STATUSES: dict[str, frozenset[int]] = {
    "pokemoncenter": frozenset({403}),
}

# Sites where a real browser is required because the anti-bot layer
# (e.g. Akamai Bot Manager) demands JavaScript execution to set
# challenge cookies.  curl/aiohttp cannot handle these.
_BROWSER_SITES: frozenset[str] = frozenset({"pokemoncenter"})

# Historical reference: pokemoncenter was the first site to require
# proxy-only access.  As of now, ALL sites have direct_fallback=False
# (see _check_once below) so the real IP is never exposed for any site.
_PROXY_REQUIRED_SITES: frozenset[str] = frozenset({"pokemoncenter"})


@dataclass
class TaskState:
    """Runtime state for a single monitor task."""

    config: MonitorConfig
    last_status: str = ""
    checks: int = 0
    alerts: int = 0
    errors: int = 0
    successes: int = 0


@dataclass
class TaskManager:
    """Orchestrates concurrent monitor tasks."""

    app_config: AppConfig
    proxy_pool: ProxyPool | None = None
    notifier: NotifierPipeline = field(default_factory=NotifierPipeline)
    _tasks: list[TaskState] = field(default_factory=list, init=False)
    _stop_event: asyncio.Event = field(default_factory=asyncio.Event, init=False)

    def stop(self) -> None:
        """Signal all tasks to stop after the current cycle."""
        self._stop_event.set()

    async def run(self) -> None:
        """Start all configured monitor tasks and run until stopped."""
        if not self.app_config.monitors:
            logger.warning("No monitors configured – nothing to do.")
            return

        enabled = [m for m in self.app_config.monitors if m.enabled]
        if not enabled:
            logger.warning("All monitors are disabled – nothing to do.")
            return

        self._tasks = [TaskState(config=m) for m in enabled]

        sem = asyncio.Semaphore(self.app_config.concurrency)
        tasks = [
            asyncio.create_task(self._run_task(state, sem))
            for state in self._tasks
        ]

        # Wait until the stop event is set, then cancel running tasks.
        await self._stop_event.wait()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        # Clean up the shared browser if it was started.
        await close_browser()
        logger.info("All monitor tasks stopped.")

    async def _run_task(self, state: TaskState, sem: asyncio.Semaphore) -> None:
        monitor = get_monitor(state.config.site)
        logger.info(
            "Starting monitor [%s] for %s every %.1fs",
            state.config.name or state.config.url,
            state.config.url,
            state.config.interval,
        )

        while not self._stop_event.is_set():
            async with sem:
                await self._check_once(state, monitor)
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=state.config.interval
                )
                break  # stop event was set
            except asyncio.TimeoutError:
                pass  # interval elapsed – continue

    async def _check_once(self, state: TaskState, monitor: object) -> None:
        state.checks += 1
        use_browser = (
            state.config.site in _BROWSER_SITES and _HAS_PLAYWRIGHT
        )
        retry_on_status = _SITE_RETRY_STATUSES.get(state.config.site)

        # NEVER fall back to a direct (no-proxy) connection.  All traffic
        # must go through the proxy pool so the real IP is never exposed.
        direct_fallback = False

        try:
            if use_browser:
                response = await fetch_with_browser(
                    state.config.url,
                    proxy_pool=self.proxy_pool,
                    timeout=self.app_config.request_timeout,
                    proxy_timeout=self.app_config.proxies.timeout,
                    extra_headers=state.config.headers or None,
                    max_retries=self.app_config.max_retries,
                    direct_fallback=direct_fallback,
                )
            else:
                response = await fetch(
                    state.config.url,
                    proxy_pool=self.proxy_pool,
                    user_agents=self.app_config.user_agents,
                    timeout=self.app_config.request_timeout,
                    proxy_timeout=self.app_config.proxies.timeout,
                    extra_headers=state.config.headers or None,
                    max_retries=self.app_config.max_retries,
                    direct_fallback=direct_fallback,
                    retry_on_status=retry_on_status,
                )
        except ConnectionError as exc:
            state.errors += 1
            logger.error("Monitor [%s] connection error: %s", state.config.name, exc)
            return

        # If we got a response, the fetch itself succeeded.
        state.successes += 1

        alert: Alert | None = monitor.parse(  # type: ignore[union-attr]
            response,
            url=state.config.url,
            keywords=state.config.keywords,
        )

        if alert is None:
            status_code = response.get("status", 0)
            # Get a human-readable stock status from the monitor.
            status_desc = monitor.describe_status(  # type: ignore[union-attr]
                response, url=state.config.url,
            )
            if 200 <= status_code < 400:
                logger.info(
                    "Monitor [%s] check #%d OK (HTTP %d) – %s",
                    state.config.name, state.checks, status_code, status_desc,
                )
                # Log a body snippet when stock data is missing to aid
                # debugging of detection failures.
                if status_desc == "No stock data found":
                    body = response.get("body", "")
                    snippet = body[:_DEBUG_SNIPPET_LENGTH].replace("\n", " ").strip()
                    logger.debug(
                        "Monitor [%s] page body snippet (first %d chars): %s",
                        state.config.name, _DEBUG_SNIPPET_LENGTH, snippet,
                    )
            else:
                logger.debug(
                    "Monitor [%s] check #%d (HTTP %d) – %s",
                    state.config.name, state.checks, status_code, status_desc,
                )
            return

        # Only notify when status changes to avoid spam.
        if alert.status != state.last_status:
            state.last_status = alert.status
            state.alerts += 1
            await self.notifier.send(alert)
        else:
            logger.debug(
                "Monitor [%s] status unchanged (%s)", state.config.name, alert.status
            )

    @property
    def task_states(self) -> list[TaskState]:
        return list(self._tasks)
