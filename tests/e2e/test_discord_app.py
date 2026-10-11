"""on_ready / attach_handlers: старт фоновых задач и досылка буфера при старте.

Полностью на фейках: клиент-«регистратор» событий вместо discord.Client,
sender со счётчиком вызовов; asyncio.create_task подменён счётчиком корутин,
чтобы бесконечные llm_worker/flush_loop/author_refresh_worker не запускались
по-настоящему.
"""

import asyncio
from types import SimpleNamespace

from bot import discord_app


class _EventClient:
    """Фейк discord-клиента: event() копит обработчики по имени; user/guilds — для лога on_ready."""

    def __init__(self):
        self.events = {}
        self.user = SimpleNamespace(name="фейк-бот")
        self.guilds = []

    def event(self, coro):
        self.events[coro.__name__] = coro
        return coro


class _CountingSender:
    """Фейк deps.sender: считает вызовы flush_pending."""

    def __init__(self):
        self.flush_pending_calls = 0

    async def flush_pending(self):
        self.flush_pending_calls += 1

    async def flush_loop(self):
        raise AssertionError("flush_loop не должен запускаться напрямую")


def _patch_create_task(monkeypatch):
    """create_task копит корутины и закрывает их — воркеры бесконечные, запускать нельзя."""

    created = []

    def _fake_create_task(coro):
        created.append(coro)
        coro.close()  # без «coroutine was never awaited»
        return SimpleNamespace()

    monkeypatch.setattr("bot.discord_app.asyncio.create_task", _fake_create_task)
    return created


def _run_on_ready_twice(client):
    async def _scenario():
        on_ready = client.events["on_ready"]
        await on_ready()  # старт
        await on_ready()  # переподключение

    asyncio.run(_scenario())


def test_on_ready_starts_background_tasks_exactly_once(monkeypatch, make_deps):
    created = _patch_create_task(monkeypatch)
    deps = make_deps(sender=_CountingSender())

    client = discord_app.attach_handlers(_EventClient(), deps)
    _run_on_ready_twice(client)

    # три фоновые задачи созданы ровно один раз, при первом on_ready
    # (фаза 3 добавила author_refresh_worker — фоновую очередь обновления обзоров)
    assert [c.cr_code.co_name for c in created] == [
        "llm_worker",
        "flush_loop",
        "author_refresh_worker",
    ]


def test_on_ready_flushes_pending_on_start(monkeypatch, make_deps):
    _patch_create_task(monkeypatch)
    sender = _CountingSender()
    deps = make_deps(sender=sender)

    client = discord_app.attach_handlers(_EventClient(), deps)
    _run_on_ready_twice(client)

    # досылка буфера — на каждом on_ready (вернулись онлайн — сразу дослали)
    assert sender.flush_pending_calls == 2
