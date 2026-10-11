"""Юнит-тесты SafeSender: ретраи отправки, классификация ошибок, досылка буфера.

Асинхронные вызовы гоняются через asyncio.run() (плагинов pytest-asyncio
в репозитории нет, см. docs/TESTING.md «E2E in-process»).
"""

import asyncio
from types import SimpleNamespace

import aiohttp
import discord
import pytest

from bot.config import BotConfig
from bot.delivery.outbox import PendingOutbox
from bot.delivery.sender import SafeSender, is_retryable_send_error


def _config(**overrides):
    params = {"bot_token": "test-token", "llm_api_key": "test-key"}
    params.update(overrides)
    return BotConfig(**params)


def _http_exc(status):
    return discord.HTTPException(SimpleNamespace(status=status, reason="x"), "boom")


def _make_sender(tmp_path, client, config=None):
    outbox = PendingOutbox(tmp_path / "pending.json")
    return SafeSender(client, outbox, config or _config()), outbox


def _record_sleeps(monkeypatch):
    """Подменяет asyncio.sleep на записывающую заглушку (поверх autouse-ной)."""
    sleeps = []

    async def _fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", _fake_sleep)
    return sleeps


class ScriptedChannel:
    """Канал, раздающий заготовленные исходы send(): None — успех, Exception — бросить."""

    def __init__(self, id=1, outcomes=()):
        self.id = id
        self.outcomes = list(outcomes)
        self.sent = []
        self.attempts = 0

    async def send(self, content):
        self.attempts += 1
        outcome = self.outcomes.pop(0) if self.outcomes else None
        if isinstance(outcome, Exception):
            raise outcome
        self.sent.append(content)


class BrokenChannel:
    """Канал, чей send всегда падает ретраебельной 5xx-ошибкой Discord."""

    def __init__(self, id=1):
        self.id = id
        self.sent = []
        self.attempts = 0

    async def send(self, content):
        self.attempts += 1
        self.sent.append(content)
        raise _http_exc(502)


class TestIsRetryableSendError:
    def test_classification(self):
        assert is_retryable_send_error(aiohttp.ClientError()) is True
        # asyncio.TimeoutError с Python 3.11 — это встроенный TimeoutError
        assert is_retryable_send_error(TimeoutError()) is True
        # 5xx ретраим, 4xx — нет
        fake_500 = discord.HTTPException(SimpleNamespace(status=500, reason="x"), "boom")
        fake_403 = discord.HTTPException(SimpleNamespace(status=403, reason="x"), "no rights")
        assert is_retryable_send_error(fake_500) is True
        assert is_retryable_send_error(fake_403) is False


class TestSend:
    def test_success_first_try(self, tmp_path):
        channel = ScriptedChannel()
        sender, _ = _make_sender(tmp_path, None)

        assert asyncio.run(sender.send(channel, "привет")) is True
        assert channel.sent == ["привет"]

    def test_retryable_then_success(self, monkeypatch, tmp_path):
        sleeps = _record_sleeps(monkeypatch)
        channel = ScriptedChannel(outcomes=[_http_exc(502), _http_exc(502), None])
        sender, _ = _make_sender(tmp_path, None)

        assert asyncio.run(sender.send(channel, "текст")) is True
        assert channel.attempts == 3
        # пауза экспоненциальная: min(30, 3 * 2^(attempt-1))
        assert sleeps == [3, 6]

    def test_non_retryable_4xx_immediate_false(self, monkeypatch, tmp_path):
        sleeps = _record_sleeps(monkeypatch)
        channel = ScriptedChannel(outcomes=[_http_exc(403)])
        sender, _ = _make_sender(tmp_path, None)

        assert asyncio.run(sender.send(channel, "текст")) is False
        assert channel.attempts == 1
        assert sleeps == []  # 4xx не ретраим и не ждём

    def test_gives_up_after_default_attempts(self, monkeypatch, tmp_path):
        sleeps = _record_sleeps(monkeypatch)
        channel = BrokenChannel()
        sender, _ = _make_sender(tmp_path, None)

        assert asyncio.run(sender.send(channel, "текст")) is False
        assert channel.attempts == 3  # дефолт send_max_attempts
        # пауза делается после каждой неудачной попытки, включая последнюю
        assert sleeps == [3, 6, 12]

    def test_send_max_attempts_from_config(self, monkeypatch, tmp_path):
        sleeps = _record_sleeps(monkeypatch)
        channel = BrokenChannel()
        sender, _ = _make_sender(tmp_path, None, _config(send_max_attempts=2))

        assert asyncio.run(sender.send(channel, "текст")) is False
        assert channel.attempts == 2
        assert sleeps == [3, 6]

    def test_network_error_retried(self, monkeypatch, tmp_path):
        _record_sleeps(monkeypatch)
        channel = ScriptedChannel(outcomes=[aiohttp.ClientError()] * 3)
        sender, _ = _make_sender(tmp_path, None)

        assert asyncio.run(sender.send(channel, "текст")) is False
        assert channel.attempts == 3

    def test_wait_capped_at_30_seconds(self, monkeypatch, tmp_path):
        sleeps = _record_sleeps(monkeypatch)
        channel = BrokenChannel()
        sender, _ = _make_sender(tmp_path, None, _config(send_max_attempts=6))

        assert asyncio.run(sender.send(channel, "текст")) is False
        assert sleeps == [3, 6, 12, 24, 30, 30]


class TestFlushPending:
    def test_delivers_in_order_and_empties(self, tmp_path, make_client, make_thread):
        thread = make_thread(777)
        sender, outbox = _make_sender(tmp_path, make_client({777: thread}))
        outbox.append(777, "первое")
        outbox.append(777, "второе")

        asyncio.run(sender.flush_pending())

        assert thread.sent == ["первое", "второе"]
        assert outbox.load() == []

    def test_drops_messages_for_dead_threads(self, tmp_path, make_client):
        sender, outbox = _make_sender(tmp_path, make_client())  # get_channel всегда None
        outbox.append(999, "в никуда")

        asyncio.run(sender.flush_pending())

        # мёртвый тред: сообщение отброшено, буфер не растёт вечно
        assert outbox.load() == []

    def test_stops_on_failure_and_keeps_remainder(self, tmp_path, make_client, make_thread):
        good = make_thread(1)
        broken = BrokenChannel(2)
        sender, outbox = _make_sender(tmp_path, make_client({1: good, 2: broken}))
        outbox.append(1, "первое")
        outbox.append(2, "второе")
        outbox.append(1, "третье")

        asyncio.run(sender.flush_pending())

        # сеть лежит: недоставленное (включая упавшее) сохраняется в порядке
        assert good.sent == ["первое"]
        assert [i["content"] for i in outbox.load()] == ["второе", "третье"]

    @pytest.mark.parametrize("closed", [True, False], ids=["closed", "not_ready"])
    def test_noop_when_client_offline(self, tmp_path, make_client, closed):
        client = make_client()
        client.closed = closed
        client.ready = False
        sender, outbox = _make_sender(tmp_path, client)
        outbox.append(777, "сообщение")

        asyncio.run(sender.flush_pending())

        assert len(outbox.load()) == 1  # буфер не тронут

    def test_empty_outbox_noop(self, tmp_path, make_client):
        sender, _ = _make_sender(tmp_path, make_client())

        asyncio.run(sender.flush_pending())  # просто не должен падать


class TestFlushLoop:
    def test_one_iteration_then_exits_on_close(self, monkeypatch, tmp_path, make_client):
        # smoke: цикл спит flush_interval_s и вызывает flush; выход — по is_closed
        sleeps = _record_sleeps(monkeypatch)
        client = make_client()
        sender, _ = _make_sender(tmp_path, client, _config(flush_interval_s=60))
        flushed = []

        async def _fake_flush():
            flushed.append(True)
            client.closed = True  # после первой итерации «бот выключился»

        monkeypatch.setattr(sender, "flush_pending", _fake_flush)

        asyncio.run(sender.flush_loop())

        assert flushed == [True]
        assert sleeps == [60]
