"""E2E-тесты in-process: полный путь «job -> субтитры -> LLM (JSON-отчёт) ->
прогрессивный рендер -> тред/буфер» без сети — fetch_transcript и LLM подменены,
Discord-объекты — фейки из conftest.

Фаза 2: LLM отвечает JSON-структурой, пайплайн отправляет отчёт ПОСООБЩЕНИЯМ
(кратко -> тезисы -> подробно по разделам); первое сообщение несёт заголовок
«**Разбор: <ссылка>**», каждое сообщение отдельно режется чанк-сплиттером
(fence-aware, (i/N) внутри одного сообщения); при сбое отправки в персистентный
буфер уходит ПОЛНЫЙ хвост отчёта: остаток чанков текущего сообщения плюс ВСЕ
последующие сообщения — конец разбора не теряется.
"""

import asyncio
import re
from types import SimpleNamespace

import discord

from bot.analysis.report import Report, render_report_messages
from bot.delivery import render
from bot.pipeline import process_job

VIDEO_ID = "abcdefghijk"
LINK = f"https://youtu.be/{VIDEO_ID}"


def _long_section_body(with_fence):
    if not with_fence:
        return "\n".join(f"абзац {j} подробностей " + "т" * 90 for j in range(25))
    return (
        "вступление раздела\n\n```python\n"
        + "\n".join(f"print({n})" for n in range(250))
        + "\n```\n"
        + "завершение раздела"
    )


# Большой отчёт: тезисы сверх бюджета/порога, длинные разделы, в одном —
# code-fence, чтобы упражнять fence-aware чанк-сплит внутри сообщения-раздела.
BIG_ANALYSIS = {
    "brief": "Суть видео в двух предложениях: о чём оно и зачем его смотреть.",
    "complexity": "simple",
    "context": "",
    "theses": [f"тезис номер {i}: " + "с" * 90 for i in range(14)],
    "sections": [
        {"title": f"Раздел {i}", "start_sec": i * 700, "body": _long_section_body(i == 1)}
        for i in range(6)
    ],
}

MINIMAL_ANALYSIS = {"brief": "Коротко и ясно"}


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
    def test_block_report_delivered_progressively(
        self, monkeypatch, make_deps, make_thread, make_status, make_llm
    ):
        thread = make_thread(4242)
        status = make_status()
        deps = make_deps(thread=thread, llm=make_llm(analysis_json=BIG_ANALYSIS))
        _patch_transcript(monkeypatch)

        asyncio.run(process_job(deps, {"video_id": VIDEO_ID, "thread_id": thread.id, "status": status}))

        sent = thread.sent
        assert sent, "сообщения не отправлены"
        # порядок: кратко (с заголовком разбора) -> тезисы -> подробно по разделам
        assert sent[0].startswith(f"**Разбор: {LINK}**\n\n**Кратко**")
        idx_theses = next(n for n, c in enumerate(sent) if c.startswith("**Тезисы**"))
        assert 0 < idx_theses
        section_starts = [
            next(n for n, c in enumerate(sent) if c.startswith(f"**Раздел {i}**")) for i in range(6)
        ]
        assert section_starts == sorted(section_starts)
        assert all(s > idx_theses for s in section_starts)
        # заголовок разбора ровно один — в первом сообщении
        assert sum(c.count("**Разбор:") for c in sent) == 1
        # все чанки в лимите Discord-сплита
        assert all(len(c) <= deps.config.split_threshold for c in sent)
        # длинные сообщения порезаны с индикаторами (i/N) внутри своего сообщения
        assert any(re.search(r" \(\d+/\d+\)$", c) for c in sent)
        # код-блоки не разорваны: суммарно чётное число фенсов, переоткрытие с языком
        fences = sum(c.count("\n```") + c.startswith("```") for c in sent)
        assert fences % 2 == 0
        assert any(c.startswith("```python") for c in sent)
        # содержимое не потерялось: все разделы и хвост длинного раздела доставлены
        assert all(f"**Раздел {i}**" in "".join(sent) for i in range(6))
        assert "завершение раздела" in "".join(sent)
        assert status.deleted
        # транскрипт дошёл до LLM в неизменном виде
        assert deps.llm.calls == ["субтитры"]

    def test_minimal_report_single_message(self, monkeypatch, make_deps, make_thread, make_llm):
        # отчёт только с brief: ровно одно сообщение — заголовок разбора + кратко
        thread = make_thread(4242)
        deps = make_deps(thread=thread, llm=make_llm(analysis_json=dict(MINIMAL_ANALYSIS)))
        _patch_transcript(monkeypatch)

        asyncio.run(process_job(deps, {"video_id": VIDEO_ID, "thread_id": thread.id, "status": None}))

        assert thread.sent == [f"**Разбор: {LINK}**\n\n**Кратко**\n\nКоротко и ясно"]

    def test_job_without_status_key_ok(self, monkeypatch, make_deps, make_thread, make_llm):
        # поле status в job опционально — его отсутствие не должно падать
        thread = make_thread(4242)
        deps = make_deps(thread=thread, llm=make_llm(analysis_json=dict(MINIMAL_ANALYSIS)))
        _patch_transcript(monkeypatch)

        asyncio.run(process_job(deps, {"video_id": VIDEO_ID, "thread_id": thread.id}))

        assert thread.sent == [f"**Разбор: {LINK}**\n\n**Кратко**\n\nКоротко и ясно"]


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

    def test_error_message_strips_mentions_from_reason(
        self, monkeypatch, make_deps, make_thread, make_status, make_llm
    ):
        # LLM/провайдер могут вернуть текст с mention: фрагмент сырья уходит в
        # {reason}, но бот не должен тегать людей в треде
        thread = make_thread(4242)
        status = make_status()
        deps = make_deps(thread=thread, llm=make_llm(raw="@everyone сломалось, пинг <@123"))
        _patch_transcript(monkeypatch)

        asyncio.run(process_job(deps, {"video_id": VIDEO_ID, "thread_id": thread.id, "status": status}))

        assert len(thread.sent) == 1
        sent = thread.sent[0]
        assert "сломалось" in sent, "причина ошибки попала в сообщение"
        assert "@everyone" not in sent, "mention @everyone вырезан из {reason}"
        assert "<@" not in sent, "начало <@-пинга вырезано из {reason}"
        assert status.deleted


class FailAfterThread:
    """Тред: первые ok_sends отправок проходят, дальше — ретраебельная 5xx Discord."""

    def __init__(self, id, ok_sends):
        self.id = id
        self.sent = []
        self.ok_sends = ok_sends

    async def send(self, content):
        self.sent.append(content)
        if len(self.sent) > self.ok_sends:
            raise discord.HTTPException(SimpleNamespace(status=502, reason="x"), "boom")


def _expected_chunks(deps):
    """Зеркало контракта сборки сообщений: рендер отчёта -> префикс первого
    сообщения -> чанк-сплит каждого сообщения по отдельности."""
    report = Report.from_analysis(BIG_ANALYSIS)
    msgs = render_report_messages(report, LINK, budgets=deps.prompts.get("budgets"))
    msgs[0] = f"**Разбор: {LINK}**\n\n{msgs[0]}"
    return [
        render.truncate_message(render.format_for_discord(msg), deps.config.split_threshold)[
            : deps.config.max_split_messages
        ]
        for msg in msgs
    ]


class TestOfflineDiscord:
    def test_send_failure_buffers_full_tail_of_report(
        self, monkeypatch, make_deps, make_client, make_status, make_llm
    ):
        # кратко и тезисы доставлены; первое сообщение-раздел падает на первом
        # чанке — в буфер уходит ПОЛНЫЙ хвост отчёта: все чанки упавшего сообщения
        # плюс ВСЕ последующие сообщения, в порядке рендера (конец не теряется)
        ok_sends = 2
        thread = FailAfterThread(4242, ok_sends=ok_sends)
        status = make_status()
        deps = make_deps(
            client=make_client({4242: thread}),
            llm=make_llm(analysis_json=BIG_ANALYSIS),
        )
        _patch_transcript(monkeypatch)

        asyncio.run(process_job(deps, {"video_id": VIDEO_ID, "thread_id": thread.id, "status": status}))

        chunks_per_msg = _expected_chunks(deps)
        flat = [c for chunks in chunks_per_msg for c in chunks]

        # первые два сообщения доставлены целиком
        assert thread.sent[:ok_sends] == flat[:ok_sends]
        # упавший чанк — первый чанк следующего (многоблочного) сообщения-раздела
        assert thread.sent[ok_sends] == chunks_per_msg[2][0]
        # в буфере полный хвост: упавшее сообщение целиком + все последующие
        items = deps.outbox.load()
        buffered = [i["content"] for i in items]
        assert buffered == chunks_per_msg[2] + [c for cs in chunks_per_msg[3:] for c in cs]
        assert len(buffered) > len(chunks_per_msg[2]), "буферизованы и последующие сообщения отчёта"
        # порядок: первый буферизованный — упавший чанк; последний содержит текст
        # ПОСЛЕДНЕГО сообщения отчёта (хвост раздела 5) — конец разбора не потерян
        assert buffered[0] == chunks_per_msg[2][0]
        assert buffered[-1] == flat[-1]
        assert "абзац 24 подробностей" in buffered[-1]
        assert all(i["thread_id"] == thread.id for i in items)
        # отправка не завершена — статусное сообщение остаётся
        assert not status.deleted

    def test_send_failure_on_first_chunk_buffers_entire_report(
        self, monkeypatch, make_deps, make_client, make_status, make_llm
    ):
        # Discord лежит с самого начала: буферизуются ВСЕ чанки ВСЕХ сообщений
        thread = FailAfterThread(4242, ok_sends=0)
        status = make_status()
        deps = make_deps(
            client=make_client({4242: thread}),
            llm=make_llm(analysis_json=BIG_ANALYSIS),
        )
        _patch_transcript(monkeypatch)

        asyncio.run(process_job(deps, {"video_id": VIDEO_ID, "thread_id": thread.id, "status": status}))

        flat = [c for chunks in _expected_chunks(deps) for c in chunks]

        # каждая попытка ретрая падает на первом же чанке
        assert thread.sent == [flat[0]] * deps.config.send_max_attempts
        items = deps.outbox.load()
        assert [i["content"] for i in items] == flat, "буфер — все чанки всех сообщений"
        assert all(i["thread_id"] == thread.id for i in items)
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
