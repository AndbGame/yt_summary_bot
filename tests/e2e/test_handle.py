"""E2E-тесты handle(): роутинг ссылки, дедуп, создание/переиспользование треда, очередь.

Дедуп в discord_app — in-memory set; тесты изолируются уникальными id сообщений
(фикстура-фабрика выдаёт их автоматически), поэтому глобальный сброс не нужен.
"""

import asyncio
from types import SimpleNamespace

import discord

from bot.discord_app import handle

VIDEO_ID = "abcdefghijk"


class ThreadAlreadyExistsError(discord.HTTPException):
    """Фейк ошибки Discord 160004 «тред под этим сообщением уже существует»."""

    def __init__(self):
        self.response = SimpleNamespace(status=400, reason="x")
        self.status = 400
        self.code = 160004
        self.text = "тред уже существует"


class TestRoutingAndQueue:
    def test_link_enqueues_single_job(self, make_deps, make_message, make_thread):
        deps = make_deps()
        thread = make_thread(555)
        msg = make_message(f"глянь https://youtu.be/{VIDEO_ID}", thread=thread)

        asyncio.run(handle(msg, deps))
        asyncio.run(handle(msg, deps))  # повтор того же сообщения — дедуп

        job = asyncio.run(deps.queue.get())
        assert job["video_id"] == VIDEO_ID
        assert job["thread_id"] == 555
        assert job["message_id"] == msg.id
        assert job["status"] is msg.status
        assert deps.queue.empty()  # второй раз задача не поставилась

    def test_plain_message_no_job(self, make_deps, make_message):
        deps = make_deps()
        msg = make_message("просто текст, без ссылок")

        asyncio.run(handle(msg, deps))

        assert deps.queue.empty()

    def test_thread_already_exists_160004_reused(self, make_deps, make_message, make_thread):
        # тред создан до рестарта бота: Discord вернёт 160004 — переиспользуем его
        deps = make_deps()
        existing = make_thread(2002)
        channel = SimpleNamespace(id=10, threads=[existing])
        msg = make_message(
            f"ссылка https://youtu.be/{VIDEO_ID}",
            id=2002,
            channel=channel,
            thread=ThreadAlreadyExistsError(),
        )

        asyncio.run(handle(msg, deps))

        job = asyncio.run(deps.queue.get())
        assert job["thread_id"] == existing.id == msg.id
        assert deps.queue.empty()  # одна задача

    def test_no_thread_permission_no_job_and_warning(self, make_deps, make_message):
        deps = make_deps()
        forbidden = discord.Forbidden(SimpleNamespace(status=403, reason="x"), "нет прав")
        msg = make_message(f"ссылка https://youtu.be/{VIDEO_ID}", thread=forbidden)

        asyncio.run(handle(msg, deps))

        assert deps.queue.empty()
        assert msg.status.edits, "пользователю не сообщили о проблеме"

    def test_queue_position_displayed_when_busy(self, make_deps, make_message, make_thread):
        deps = make_deps()
        asyncio.run(deps.queue.put({"video_id": "ранее принятое"}))
        msg = make_message(f"https://youtu.be/{VIDEO_ID}", thread=make_thread(555))

        asyncio.run(handle(msg, deps))

        # позиция в очереди показана в статусе (наш job второй)
        assert any(
            content and "позиция" in content and "2" in content
            for content in msg.status.edits
        )
