"""Tests for the task manager module."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from pokemonbot.config import AppConfig, MonitorConfig
from pokemonbot.notifier import NotifierPipeline
from pokemonbot.tasks import TaskManager, TaskState


class TestTaskState:
    def test_defaults(self):
        cfg = MonitorConfig(name="test", url="https://example.com")
        state = TaskState(config=cfg)
        assert state.checks == 0
        assert state.alerts == 0
        assert state.errors == 0
        assert state.last_status == ""


class TestTaskManager:
    def test_stop_event(self):
        cfg = AppConfig()
        manager = TaskManager(app_config=cfg)
        assert not manager._stop_event.is_set()
        manager.stop()
        assert manager._stop_event.is_set()

    @pytest.mark.asyncio
    async def test_run_no_monitors(self):
        """Should return immediately when no monitors are configured."""
        cfg = AppConfig(monitors=[])
        manager = TaskManager(app_config=cfg)
        await manager.run()
        assert manager.task_states == []

    @pytest.mark.asyncio
    async def test_check_once_success(self):
        """Simulate a single check that finds a product in stock."""
        monitor_cfg = MonitorConfig(
            name="test",
            url="https://example.com",
            site="generic",
            keywords=[],
            interval=1.0,
        )
        cfg = AppConfig(monitors=[monitor_cfg])

        sent_alerts = []

        class FakeNotifier:
            async def send(self, alert):
                sent_alerts.append(alert)

        notifier = NotifierPipeline()
        notifier._notifiers.append(FakeNotifier())  # type: ignore[arg-type]

        manager = TaskManager(app_config=cfg, notifier=notifier)
        state = TaskState(config=monitor_cfg)

        fake_response = {
            "status": 200,
            "body": '<button>Add to Cart</button>',
            "headers": {},
            "url": "https://example.com",
        }

        from pokemonbot.monitor import GenericMonitor

        monitor = GenericMonitor()

        with patch("pokemonbot.tasks.fetch", new_callable=AsyncMock, return_value=fake_response):
            await manager._check_once(state, monitor)

        assert state.checks == 1
        assert state.alerts == 1
        assert len(sent_alerts) == 1
        assert sent_alerts[0].status == "in_stock"

    @pytest.mark.asyncio
    async def test_check_once_no_change(self):
        """Simulate a check where status didn't change."""
        monitor_cfg = MonitorConfig(
            name="test", url="https://example.com", site="generic"
        )
        cfg = AppConfig(monitors=[monitor_cfg])
        manager = TaskManager(app_config=cfg)
        state = TaskState(config=monitor_cfg, last_status="in_stock")

        fake_response = {
            "status": 200,
            "body": '<button>Add to Cart</button>',
            "headers": {},
            "url": "https://example.com",
        }

        from pokemonbot.monitor import GenericMonitor

        monitor = GenericMonitor()

        with patch("pokemonbot.tasks.fetch", new_callable=AsyncMock, return_value=fake_response):
            await manager._check_once(state, monitor)

        # Checks incremented but no new alert (status unchanged)
        assert state.checks == 1
        assert state.alerts == 0

    @pytest.mark.asyncio
    async def test_check_once_connection_error(self):
        """Simulate a connection failure."""
        monitor_cfg = MonitorConfig(
            name="test", url="https://example.com", site="generic"
        )
        cfg = AppConfig(monitors=[monitor_cfg])
        manager = TaskManager(app_config=cfg)
        state = TaskState(config=monitor_cfg)

        from pokemonbot.monitor import GenericMonitor

        monitor = GenericMonitor()

        with patch(
            "pokemonbot.tasks.fetch",
            new_callable=AsyncMock,
            side_effect=ConnectionError("timeout"),
        ):
            await manager._check_once(state, monitor)

        assert state.checks == 1
        assert state.errors == 1

    @pytest.mark.asyncio
    async def test_check_once_logs_success_at_info(self, caplog):
        """A successful check with no alert should log an INFO-level OK message."""
        import logging
        monitor_cfg = MonitorConfig(
            name="test", url="https://example.com", site="generic"
        )
        cfg = AppConfig(monitors=[monitor_cfg])
        manager = TaskManager(app_config=cfg)
        state = TaskState(config=monitor_cfg)

        fake_response = {
            "status": 200,
            "body": "<p>Nothing special</p>",
            "headers": {},
            "url": "https://example.com",
        }

        from pokemonbot.monitor import GenericMonitor

        monitor = GenericMonitor()

        with patch("pokemonbot.tasks.fetch", new_callable=AsyncMock, return_value=fake_response):
            with caplog.at_level(logging.INFO, logger="pokemonbot.tasks"):
                await manager._check_once(state, monitor)

        assert any("OK" in r.message and r.levelno == logging.INFO for r in caplog.records)
