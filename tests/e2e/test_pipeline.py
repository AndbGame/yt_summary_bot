"""E2E-тесты in-process: полный путь «ссылка -> субтитры -> LLM -> формат -> тред»
без сети — fetch_transcript / summarize / Discord-объекты подменены фейками.

Это «ходячий скелет» (walking skeleton): проверяет пользовательски видимое
поведение (последовательность и формат сообщений в треде), а не внутренности.
"""

import asyncio
from types import SimpleNamespace

import discord
import pytest

import yt_summary_bot as bot

VIDEO_ID = "abcdefghijk"


class FakeThread:
    def __init__(self, id=4242):
        self.id = id
        self.sent = []

    async def send(self, content):
        self.sent.append(content)


class FakeStatus:
    def __init__(self):
        self.deleted = False

    async def delete(self):
        self.deleted = True


def _http_exc(status):
    return discord.HTTPException(SimpleNamespace(status=status, reason="x"), "boom")


def _patch_pipeline(monkeypatch, thread, transcript="…", summary="итог", exc=None):
    monkeypatch.setattr(bot, "_resolve_channel", lambda tid: _async_return(thread))
    monkeypatch.setattr(
        bot, "fetch_transcript", lambda vid: transcript if not exc else (_raise(exc))
    )
    monkeypatch.setattr(bot, "summarize", lambda tr: _raise(exc) if exc else summary)


def _async_return(value):
    async def inner():
        return value

    return inner()


def _raise(exc):
    raise exc


LONG_SUMMARY = (
    "**Заголовок разбора**\n\n"
    + "\n".join(f"- тезис {i}: " + "с" * 90 for i in range(12))
    + "\n\n| Метрика | Значение |\n|---|---|\n| Длина | 42 мин |\n| Сложность | средняя |\n\n"
    + "```python\n" + "\n".join(f"print({i})" for i in range(60)) + "\n```\n"
    + "\n".join(f"абзац {i} подробностей " + "т" * 80 for i in range(20))
)


def _run(coro):
    return asyncio.run(coro)


class TestHappyPath:
    def test_long_summary_chunked_and_formatted(self, monkeypatch):
        thread = FakeThread()
        status = FakeStatus()
        _patch_pipeline(monkeypatch, thread, summary=LONG_SUMMARY)

        _run(bot.process_job({"video_id": VIDEO_ID, "thread_id": thread.id, "status": status}))

        assert thread.sent, "сообщения не отправлены"
        assert all(len(c) <= bot.SPLIT_THRESHOLD for c in thread.sent)
        if len(thread.sent) > 1:
            assert thread.sent[0].endswith("(1/…)".replace("…", str(len(thread.sent))))
        # GFM-таблицы конвертированы (сепараторов вне код-блоков нет)
        assert not any("|---|" in c for c in thread.sent)
        # код-блоки не разорваны: суммарно чётное число фенсов
        fences = sum(c.count("\n```") + c.startswith("```") for c in thread.sent)
        assert fences % 2 == 0
        # заголовок с ссылкой на видео присутствует
        assert f"https://youtu.be/{VIDEO_ID}" in thread.sent[0]
        assert status.deleted

    def test_short_summary_single_message(self, monkeypatch):
        thread = FakeThread()
        _patch_pipeline(monkeypatch, thread, summary="**Суть:** всё хорошо")
        _run(bot.process_job({"video_id": VIDEO_ID, "thread_id": thread.id, "status": None}))
        assert len(thread.sent) == 1


class TestErrorPath:
    def test_transcript_failure_posts_error_message(self, monkeypatch):
        thread = FakeThread()
        status = FakeStatus()
        _patch_pipeline(
            monkeypatch, thread,
            exc=RuntimeError("нет доступных субтитров"),
        )

        _run(bot.process_job({"video_id": VIDEO_ID, "thread_id": thread.id, "status": status}))

        assert len(thread.sent) == 1
        assert "нет доступных субтитров" in thread.sent[0]
        assert status.deleted


class TestOfflineDiscord:
    def test_send_failure_buffers_to_pending(self, monkeypatch, tmp_path):
        monkeypatch.setattr(bot, "PENDING_FILE", tmp_path / "pending.json")
        monkeypatch.setattr("asyncio.sleep", _noop_sleep())
        thread = BrokenThread(FakeThread())  # send всегда падает 5xx
        _patch_pipeline(monkeypatch, thread, summary="короткий итог")

        _run(bot.process_job({"video_id": VIDEO_ID, "thread_id": thread.id, "status": None}))

        items = bot._pending_load()
        assert items and items[0]["thread_id"] == thread.id
        assert items[0]["content"].startswith("**Тезисы:")

    def test_flush_delivers_buffer_in_order(self, monkeypatch, tmp_path):
        monkeypatch.setattr(bot, "PENDING_FILE", tmp_path / "pending.json")
        delivered = FakeThread(id=777)
        bot._pending_save([
            {"thread_id": 777, "content": "первое"},
            {"thread_id": 777, "content": "второе"},
        ])
        monkeypatch.setattr(
            bot, "client",
            SimpleNamespace(
                is_closed=lambda: False,
                is_ready=lambda: True,
                get_channel=lambda tid: delivered if tid == 777 else None,
                fetch_channel=_not_found(),
            ),
        )

        _run(bot.flush_pending())

        assert delivered.sent == ["первое", "второе"]
        assert bot._pending_load() == []

    def test_flush_drops_messages_for_dead_threads(self, monkeypatch, tmp_path):
        monkeypatch.setattr(bot, "PENDING_FILE", tmp_path / "pending.json")
        bot._pending_save([{"thread_id": 999, "content": "в никуда"}])
        monkeypatch.setattr(
            bot, "client",
            SimpleNamespace(
                is_closed=lambda: False,
                is_ready=lambda: True,
                get_channel=lambda tid: None,
                fetch_channel=_not_found(),
            ),
        )

        _run(bot.flush_pending())
        assert bot._pending_load() == []


class BrokenThread:
    """Тред, чей send всегда падает ретраебельной 5xx-ошибкой Discord."""

    def __init__(self, inner):
        self.id = inner.id
        self.sent = []

    async def send(self, content):
        self.sent.append(content)
        raise _http_exc(502)


def _noop_sleep():
    async def noop(_):
        return None

    return noop


def _not_found():
    async def raise_not_found(tid):
        raise discord.NotFound(SimpleNamespace(status=404, reason="x"), "no such channel")

    return raise_not_found


class TestRoutingAndQueue:
    """handle(): распознавание ссылки, дедуп, постановка задачи в очередь."""

    class FakeMessage:
        def __init__(self, content):
            self.id = 1001
            self.content = content
            self.author = SimpleNamespace(bot=False)
            self.channel = SimpleNamespace(id=10, threads=[])
            self.status = FakeStatus()
            self.thread = FakeThread(555)

        async def reply(self, text=""):
            return self.status

        async def create_thread(self, **kwargs):
            return self.thread

    def test_link_enqueues_single_job(self, monkeypatch):
        msg = self.FakeMessage(f"глянь https://youtu.be/{VIDEO_ID}")
        while not bot.job_queue.empty():
            bot.job_queue.get_nowait()

        _run(bot.handle(msg))
        _run(bot.handle(msg))  # повтор того же сообщения — дедуп

        job = _run(_get_job())
        assert job["video_id"] == VIDEO_ID
        assert job["thread_id"] == 555
        assert _run(_queue_drained())

    def test_plain_message_no_job(self):
        assert _run(_handle_returns_none(self.FakeMessage("просто текст")))
        assert bot.job_queue.empty()


async def _get_job():
    return await bot.job_queue.get()


async def _queue_drained():
    return bot.job_queue.empty()


async def _handle_returns_none(msg):
    await bot.handle(msg)
    return True


@pytest.fixture(autouse=True)
def _clean_queue():
    while not bot.job_queue.empty():
        bot.job_queue.get_nowait()
    yield
    while not bot.job_queue.empty():
        bot.job_queue.get_nowait()
