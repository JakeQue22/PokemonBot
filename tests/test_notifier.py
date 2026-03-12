"""Tests for the notifier module."""

import asyncio

from pokemonbot.notifier import Alert, ConsoleNotifier, NotifierPipeline


class TestAlert:
    def test_timestamp_auto_set(self):
        a = Alert(product_name="Test", url="https://example.com", status="in_stock")
        assert a.timestamp != ""

    def test_fields(self):
        a = Alert(
            product_name="Card Pack",
            url="https://example.com/pack",
            status="in_stock",
            site="pokemoncenter",
            price="$49.99",
        )
        assert a.product_name == "Card Pack"
        assert a.price == "$49.99"


class TestConsoleNotifier:
    def test_send(self):
        n = ConsoleNotifier()
        alert = Alert(product_name="Test", url="https://example.com", status="in_stock")
        asyncio.get_event_loop().run_until_complete(n.send(alert))


class TestNotifierPipeline:
    def test_pipeline_dispatches(self):
        sent = []

        class FakeNotifier:
            async def send(self, alert):
                sent.append(alert)

        pipeline = NotifierPipeline()
        pipeline._notifiers.append(FakeNotifier())  # type: ignore[arg-type]
        pipeline._notifiers.append(FakeNotifier())  # type: ignore[arg-type]
        alert = Alert(product_name="Test", url="https://x.com", status="queue_active")
        asyncio.get_event_loop().run_until_complete(pipeline.send(alert))
        assert len(sent) == 2
