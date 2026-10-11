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


# ------------------------------------------------------- recovery джоб (фаза 4)


def test_on_ready_recovers_jobs_before_workers_start(monkeypatch, make_deps):
    # восстановление джоб из БД — ДО старта воркеров, иначе джоба могла бы
    # обработаться дважды: из recovery и из очереди, куда её уже поставили
    order = []

    async def _fake_recover(deps):
        order.append("recover")

    def _fake_create_task(coro):
        order.append(coro.cr_code.co_name)
        coro.close()  # без «coroutine was never awaited»
        return SimpleNamespace()

    monkeypatch.setattr("bot.discord_app.recover_jobs", _fake_recover, raising=False)
    monkeypatch.setattr("bot.discord_app.asyncio.create_task", _fake_create_task)
    deps = make_deps(sender=_CountingSender())

    client = discord_app.attach_handlers(_EventClient(), deps)
    _run_on_ready_twice(client)

    assert order == ["recover", "llm_worker", "flush_loop", "author_refresh_worker"], (
        "recover_jobs выполняется до старта воркеров; переподключение восстановление не дублирует"
    )


def test_on_ready_enqueues_recovered_jobs_with_attached_status(
    monkeypatch, make_deps, make_thread, make_status
):
    # джоба «застряла» в БД (state=new, процесс умер): on_ready ставит её в
    # очередь и прикрепляет статусное сообщение через fetch_message
    _patch_create_task(monkeypatch)
    thread = make_thread(4242)
    status = make_status(888)
    thread.fetchable[888] = status
    deps = make_deps(thread=thread, sender=_CountingSender())
    deps.jobs.put(
        {
            "kind": "video",
            "url": "https://youtu.be/abcdefghijk",
            "canonical_id": "youtube:video:abcdefghijk",
            "thread_id": 4242,
            "message_id": 777,
            "status_message_id": 888,
        }
    )

    client = discord_app.attach_handlers(_EventClient(), deps)
    asyncio.run(client.events["on_ready"]())

    job = deps.queue.get_nowait()
    assert job["job_id"] == 1
    assert job["url"] == "https://youtu.be/abcdefghijk"
    assert job["thread_id"] == 4242
    assert job["status"] is status, "статус прикреплён к восстановленной джобе"


def test_on_ready_recovered_job_without_status_still_enqueued(monkeypatch, make_deps):
    # ни канала, ни сообщения на стороне Discord: джоба всё равно ставится в
    # очередь (разбор потерять нельзя), статус None — пайплайн это допускает
    _patch_create_task(monkeypatch)
    deps = make_deps(sender=_CountingSender())  # клиент без каналов
    deps.jobs.put(
        {
            "kind": "video",
            "url": "https://youtu.be/abcdefghijk",
            "canonical_id": "youtube:video:abcdefghijk",
            "thread_id": 9999,
            "message_id": 777,
            "status_message_id": 1,
        }
    )

    client = discord_app.attach_handlers(_EventClient(), deps)
    asyncio.run(client.events["on_ready"]())

    job = deps.queue.get_nowait()
    assert job.get("status") is None
    assert job["thread_id"] == 9999
