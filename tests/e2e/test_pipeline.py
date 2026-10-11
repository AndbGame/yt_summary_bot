"""E2E-тесты in-process: полный путь «job -> субтитры -> LLM -> формат -> тред/буфер»
без сети — fetch_transcript и LLM подменены, Discord-объекты — фейки из conftest.

Это «ходячий скелет» (walking skeleton): проверяет пользовательски видимое
поведение (последовательность и формат сообщений в треде), а не внутренности.
В отличие от монолита, зависимости передаются явно через Deps — глобальных
подмен минимум (только fetch_transcript в bot.pipeline).
"""

import asyncio
from types import SimpleNamespace

import discord

from bot.delivery import render
from bot.pipeline import process_job

VIDEO_ID = "abcdefghijk"

LONG_SUMMARY = (
    "**Заголовок разбора**\n\n"
    + "\n".join(f"- тезис {i}: " + "с" * 90 for i in range(12))
    + "\n\n| Метрика | Значение |\n|---|---|\n| Длина | 42 мин |\n| Сложность | средняя |\n\n"
    + "```python\n" + "\n".join(f"print({i})" for i in range(60)) + "\n```\n"
    + "\n".join(f"абзац {i} подробностей " + "т" * 80 for i in range(20))
)


def _patch_transcript(monkeypatch, transcript="субтитры", exc=None):
    """Подменяет fetch_transcript в пространстве имён bot.pipeline (контракт пайплайна)."""

    def _fake(video_id):
        if exc is not None:
            raise exc
        assert video_id == VIDEO_ID
        return transcript

    monkeypatch.setattr("bot.pipeline.fetch_transcript", _fake)


class BrokenThread:
    """Тред, чей send всегда падает ретраебельной 5xx-ошибкой Discord."""

    def __init__(self, id):
        self.id = id
        self.sent = []

    async def send(self, content):
        self.sent.append(content)
        raise discord.HTTPException(SimpleNamespace(status=502, reason="x"), "boom")


class TestHappyPath:
    def test_long_summary_chunked_and_formatted(self, monkeypatch, make_deps, make_thread, make_status, make_llm):
        thread = make_thread(4242)
        status = make_status()
        deps = make_deps(thread=thread, llm=make_llm(summary=LONG_SUMMARY))
        _patch_transcript(monkeypatch)

        asyncio.run(process_job(deps, {"video_id": VIDEO_ID, "thread_id": thread.id, "status": status}))

        assert thread.sent, "сообщения не отправлены"
        assert all(len(c) <= deps.config.split_threshold for c in thread.sent)
        if len(thread.sent) > 1:
            assert thread.sent[0].endswith(f"(1/{len(thread.sent)})")
        # GFM-таблицы конвертированы (сепараторов вне код-блоков нет)
        assert not any("|---|" in c for c in thread.sent)
        # код-блоки не разорваны: суммарно чётное число фенсов
        fences = sum(c.count("\n```") + c.startswith("```") for c in thread.sent)
        assert fences % 2 == 0
        # заголовок с ссылкой на видео присутствует
        assert f"https://youtu.be/{VIDEO_ID}" in thread.sent[0]
        assert status.deleted
        # транскрипт дошёл до LLM в неизменном виде
        assert deps.llm.calls == ["субтитры"]

    def test_short_summary_single_message(self, monkeypatch, make_deps, make_thread, make_llm):
        thread = make_thread(4242)
        deps = make_deps(thread=thread, llm=make_llm(summary="**Суть:** всё хорошо"))
        _patch_transcript(monkeypatch)

        asyncio.run(process_job(deps, {"video_id": VIDEO_ID, "thread_id": thread.id, "status": None}))

        assert len(thread.sent) == 1

    def test_job_without_status_key_ok(self, monkeypatch, make_deps, make_thread):
        # поле status в job опционально — его отсутствие не должно падать
        thread = make_thread(4242)
        deps = make_deps(thread=thread)
        _patch_transcript(monkeypatch)

        asyncio.run(process_job(deps, {"video_id": VIDEO_ID, "thread_id": thread.id}))

        assert len(thread.sent) == 1


class TestErrorPath:
    def test_transcript_failure_posts_error_message(self, monkeypatch, make_deps, make_thread, make_status):
        thread = make_thread(4242)
        status = make_status()
        deps = make_deps(thread=thread)
        _patch_transcript(monkeypatch, exc=RuntimeError("нет доступных субтитров"))

        asyncio.run(process_job(deps, {"video_id": VIDEO_ID, "thread_id": thread.id, "status": status}))

        assert len(thread.sent) == 1
        # причина попадает в шаблон ошибки из prompts.yaml ({reason})
        assert "нет доступных субтитров" in thread.sent[0]
        assert status.deleted

    def test_llm_failure_posts_error_message(self, monkeypatch, make_deps, make_thread, make_status, make_llm):
        thread = make_thread(4242)
        status = make_status()
        deps = make_deps(
            thread=thread,
            llm=make_llm(error=RuntimeError("LLM недоступна")),
        )
        _patch_transcript(monkeypatch)

        asyncio.run(process_job(deps, {"video_id": VIDEO_ID, "thread_id": thread.id, "status": status}))

        assert len(thread.sent) == 1
        assert "LLM недоступна" in thread.sent[0]
        assert status.deleted

    def test_error_message_without_disk_prompts_read(self, monkeypatch, make_deps, make_thread, make_status, make_llm):
        # битый prompts.yaml на диске не мешает error-сообщению: шаблон берётся
        # из Deps.prompts (в памяти), диск error-ветка не перечитывает
        thread = make_thread(4242)
        status = make_status()
        deps = make_deps(
            thread=thread,
            llm=make_llm(error=RuntimeError("LLM недоступна")),
            prompts={"error": "упс: {reason}"},
        )
        _patch_transcript(monkeypatch)

        disk_reads = []

        def _broken_yaml(*args, **kwargs):
            # эмуляция битого prompts.yaml + счётчик обращений к диску
            disk_reads.append(1)
            raise RuntimeError("битый prompts.yaml")

        monkeypatch.setattr("bot.pipeline.load_prompts", _broken_yaml)

        asyncio.run(process_job(deps, {"video_id": VIDEO_ID, "thread_id": thread.id, "status": status}))

        assert len(thread.sent) == 1
        assert "упс: LLM недоступна" in thread.sent[0]
        assert status.deleted
        assert disk_reads == [], "error-путь не должен перечитывать prompts.yaml"


class TestOfflineDiscord:
    def test_send_failure_buffers_all_chunks_in_order(
        self, monkeypatch, make_deps, make_client, make_status, make_llm
    ):
        thread = BrokenThread(4242)
        status = make_status()
        deps = make_deps(
            client=make_client({4242: thread}),
            llm=make_llm(summary=LONG_SUMMARY),
        )
        _patch_transcript(monkeypatch)

        asyncio.run(process_job(deps, {"video_id": VIDEO_ID, "thread_id": thread.id, "status": status}))

        # в буфер попали ровно те чанки, что должны были уйти, и в том же порядке
        full = render.format_for_discord(f"**Тезисы: https://youtu.be/{VIDEO_ID}**\n\n{LONG_SUMMARY}")
        expected = render.truncate_message(full, deps.config.split_threshold)[: deps.config.max_split_messages]
        items = deps.outbox.load()
        assert [i["content"] for i in items] == expected
        assert len(items) > 1
        assert all(i["thread_id"] == thread.id for i in items)
        # отправка не завершена — статусное сообщение остаётся
        assert not status.deleted

    def test_flush_delivers_buffer_in_order(self, make_deps, make_thread):
        thread = make_thread(777)
        deps = make_deps(thread=thread)
        deps.outbox.append(777, "первое")
        deps.outbox.append(777, "второе")

        asyncio.run(deps.sender.flush_pending())

        assert thread.sent == ["первое", "второе"]
        assert deps.outbox.load() == []

    def test_flush_drops_messages_for_dead_threads(self, make_deps):
        # клиент без каналов: get_channel -> None, fetch_channel -> NotFound
        deps = make_deps()
        deps.outbox.append(999, "в никуда")

        asyncio.run(deps.sender.flush_pending())

        assert deps.outbox.load() == []

    def test_flush_stops_on_failure_and_keeps_remainder(self, make_deps, make_thread):
        good = make_thread(1)
        deps = make_deps(thread=good)
        deps.outbox.append(1, "первое")
        # второй тред «пал» между буферизацией и досылкой: send всегда 5xx
        broken = BrokenThread(2)
        deps.client.channels[2] = broken
        deps.outbox.append(2, "второе")
        deps.outbox.append(1, "третье")

        asyncio.run(deps.sender.flush_pending())

        # сеть снова легла: доставлено до сбоя, остальное — в буфере в порядке
        assert good.sent == ["первое"]
        assert [i["content"] for i in deps.outbox.load()] == ["второе", "третье"]
