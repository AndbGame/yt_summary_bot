"""E2E-тесты in-process: полный путь «job -> extract -> LLM (JSON-отчёт) ->
прогрессивный рендер -> тред/буфер» без сети — экстрактор и LLM подменены,
Discord-объекты — фейки из conftest.

Фаза 4: пайплайн не знает про YouTube — джоба несёт kind/url/canonical_id,
контент добывает экстрактор из deps.router (extract -> SourceDocument, контент
в doc.content), canonical_id берётся из джобы, ссылка для рендера — doc.url.
monkeypatch bot.pipeline.fetch_transcript больше не нужен: контент подменяется
фейк-экстрактором в роутере. LLM отвечает JSON-структурой, отчёт отправляется
ПОСООБЩЕНИЯМ (кратко -> тезисы -> подробно по разделам); при сбое отправки в
персистентный буфер уходит ПОЛНЫЙ хвост отчёта.

Ошибка добычи контента/LLM: пользователь получает error-сообщение (error-путь),
а джоба помечается упавшей — process_job пробрасывает исключение после
доставки, llm_worker ставит mark_failed (см. tests/e2e/test_recovery.py).
"""

import asyncio
from types import SimpleNamespace

import discord
import pytest

from bot.analysis.report import Report, ReportParseError, render_report_messages
from bot.delivery import render
from bot.pipeline import process_job

VIDEO_ID = "abcdefghijk"
URL = f"https://youtu.be/{VIDEO_ID}"
LINK = URL  # рендер-ссылка = doc.url экстрактора


# Компактный отчёт (UX-итерация): тайминги — по одной строке, «Подробно»
# на 20 секциях не влезает в один пост (упаковывается в два); тезисы —
# под промпт-цель (1100) и бюджет (1300), без маркера обрезки.
BIG_ANALYSIS = {
    "brief": "Суть видео в двух предложениях: о чём оно и зачем его смотреть.",
    "complexity": "simple",
    "context": "",
    "theses": [f"тезис номер {i}: " + "с" * 90 for i in range(12)],
    "sections": [
        {"title": f"Раздел {i}", "start_sec": i * 140, "body": f"короткое описание {i} " + "х" * 80}
        for i in range(20)
    ],
}

MINIMAL_ANALYSIS = {"brief": "Коротко и ясно"}


def _job(extractor, thread_id, **extra):
    """Джоба нового формата (фаза 4): canonical_id считает экстрактор."""

    job = {
        "kind": extractor.name,
        "url": URL,
        "canonical_id": extractor.canonical_id(URL),
        "thread_id": thread_id,
    }
    job.update(extra)
    return job


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
        self, make_deps, make_thread, make_status, make_llm, make_extractor, make_router
    ):
        thread = make_thread(4242)
        status = make_status()
        extractor = make_extractor(transcripts={URL: "субтитры"})
        deps = make_deps(
            thread=thread,
            llm=make_llm(analysis_json=BIG_ANALYSIS),
            router=make_router([extractor]),
        )

        asyncio.run(process_job(deps, _job(extractor, thread.id, status=status)))

        sent = thread.sent
        assert sent, "сообщения не отправлены"
        # компактность: 20 таймингов по одной строке — 4 сообщения
        # (Разбор+Кратко, Тезисы, Подробно ×2), а не пост на каждый тайминг
        assert len(sent) == 4
        assert sent[0].startswith(f"**Разбор: <{LINK}>**\n\n**Кратко**")
        assert sent[1].startswith("**Тезисы**")
        # Подробно: шапка + по строке на тайминг, ссылки маскированы
        podrobno = [s for s in sent if s.startswith("**Подробно**")]
        assert len(podrobno) == 2, "тайминги упакованы в два поста"
        lines = "\n".join(p.split("\n\n", 1)[1] for p in podrobno).split("\n")
        assert len(lines) == 20
        for i, line in enumerate(lines):
            sec = i * 140
            stamp = f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}" if sec >= 3600 else f"{sec // 60:02d}:{sec % 60:02d}"
            assert line.startswith(f"[{stamp}](<{LINK}?t={sec}>)"), line
            assert f"**Раздел {i}**: короткое описание {i}" in line
        # заголовок разбора ровно один; всё в лимите Discord-сплита
        assert sum(c.count("**Разбор:") for c in sent) == 1
        assert all(len(c) <= deps.config.split_threshold for c in sent)
        assert status.deleted
        # контент экстрактора дошёл до LLM в неизменном виде
        assert deps.llm.calls == ["субтитры"]
        assert extractor.extract_calls == [URL], "контент добыт по url джобы"

    def test_minimal_report_single_message(
        self, make_deps, make_thread, make_llm, make_extractor, make_router
    ):
        # отчёт только с brief: ровно одно сообщение — заголовок разбора + кратко
        thread = make_thread(4242)
        extractor = make_extractor(transcripts={URL: "субтитры"})
        deps = make_deps(
            thread=thread,
            llm=make_llm(analysis_json=dict(MINIMAL_ANALYSIS)),
            router=make_router([extractor]),
        )

        asyncio.run(process_job(deps, _job(extractor, thread.id, status=None)))

        assert thread.sent == [f"**Разбор: <{LINK}>**\n\n**Кратко**\n\nКоротко и ясно"]

    def test_job_without_status_key_ok(self, make_deps, make_thread, make_llm, make_extractor, make_router):
        # поле status в job опционально — его отсутствие не должно падать
        thread = make_thread(4242)
        extractor = make_extractor(transcripts={URL: "субтитры"})
        deps = make_deps(
            thread=thread,
            llm=make_llm(analysis_json=dict(MINIMAL_ANALYSIS)),
            router=make_router([extractor]),
        )

        asyncio.run(process_job(deps, _job(extractor, thread.id)))

        assert thread.sent == [f"**Разбор: <{LINK}>**\n\n**Кратко**\n\nКоротко и ясно"]

    def test_header_uses_display_metadata_without_link(
        self, make_deps, make_thread, make_llm, make_extractor, make_router
    ):
        # UX-итерация: заголовок «Разбор: Название (Автор)» — без ссылки
        # (голый URL даёт превью-встройку в Discord)
        thread = make_thread(4242)
        extractor = make_extractor(transcripts={URL: "субтитры"})
        deps = make_deps(
            thread=thread,
            llm=make_llm(analysis_json=dict(MINIMAL_ANALYSIS)),
            router=make_router([extractor]),
        )
        job = _job(
            extractor, thread.id, status=None,
            display_title="Призмы анимо", display_author="Мир Aniimo",
        )

        asyncio.run(process_job(deps, job))

        assert thread.sent == ["**Разбор: Призмы анимо (Мир Aniimo)**\n\n**Кратко**\n\nКоротко и ясно"]
        assert LINK not in thread.sent[0], "ссылки в заголовке нет"

    def test_header_clips_long_title_keeps_author(
        self, make_deps, make_thread, make_llm, make_extractor, make_router
    ):
        from bot.pipeline import TITLE_CLIP

        thread = make_thread(4242)
        extractor = make_extractor(transcripts={URL: "субтитры"})
        deps = make_deps(
            thread=thread,
            llm=make_llm(analysis_json=dict(MINIMAL_ANALYSIS)),
            router=make_router([extractor]),
        )
        job = _job(
            extractor, thread.id, status=None,
            display_title="Б" * (TITLE_CLIP + 10), display_author="Канал",
        )

        asyncio.run(process_job(deps, job))

        header = thread.sent[0].split("\n")[0]
        assert header == f"**Разбор: {'Б' * TITLE_CLIP}… (Канал)**"

    def test_header_without_author_title_only(
        self, make_deps, make_thread, make_llm, make_extractor, make_router
    ):
        thread = make_thread(4242)
        extractor = make_extractor(transcripts={URL: "субтитры"})
        deps = make_deps(
            thread=thread,
            llm=make_llm(analysis_json=dict(MINIMAL_ANALYSIS)),
            router=make_router([extractor]),
        )
        job = _job(extractor, thread.id, status=None, display_title="Название", display_author="")

        asyncio.run(process_job(deps, job))

        assert thread.sent[0].startswith("**Разбор: Название**\n\n")


class TestErrorPath:
    def test_extraction_failure_posts_error_message(
        self, make_deps, make_thread, make_status, make_extractor, make_router
    ):
        # экстрактор не добыл контент: error-сообщение пользователю, статус закрыт;
        # исключение пробрасывается — воркер пометит джобу failed
        thread = make_thread(4242)
        status = make_status()
        extractor = make_extractor(extract_error=RuntimeError("нет доступных субтитров"))
        deps = make_deps(thread=thread, router=make_router([extractor]))

        with pytest.raises(RuntimeError):
            asyncio.run(process_job(deps, _job(extractor, thread.id, status=status)))

        assert len(thread.sent) == 1
        # причина попадает в шаблон ошибки из prompts.yaml ({reason})
        assert "нет доступных субтитров" in thread.sent[0]
        assert status.deleted

    def test_llm_failure_posts_error_message(
        self, make_deps, make_thread, make_status, make_llm, make_extractor, make_router
    ):
        thread = make_thread(4242)
        status = make_status()
        extractor = make_extractor(transcripts={URL: "субтитры"})
        deps = make_deps(
            thread=thread,
            llm=make_llm(error=RuntimeError("LLM недоступна")),
            router=make_router([extractor]),
        )

        with pytest.raises(RuntimeError):
            asyncio.run(process_job(deps, _job(extractor, thread.id, status=status)))

        assert len(thread.sent) == 1
        assert "LLM недоступна" in thread.sent[0]
        assert status.deleted

    def test_error_message_without_disk_prompts_read(
        self, monkeypatch, make_deps, make_thread, make_status, make_llm, make_extractor, make_router
    ):
        # битый prompts.yaml на диске не мешает error-сообщению: шаблон берётся
        # из Deps.prompts (в памяти), диск error-ветка не перечитывает
        thread = make_thread(4242)
        status = make_status()
        extractor = make_extractor(transcripts={URL: "субтитры"})
        deps = make_deps(
            thread=thread,
            llm=make_llm(error=RuntimeError("LLM недоступна")),
            prompts={"error": "упс: {reason}"},
            router=make_router([extractor]),
        )

        disk_reads = []

        def _broken_yaml(*args, **kwargs):
            # эмуляция битого prompts.yaml + счётчик обращений к диску
            disk_reads.append(1)
            raise RuntimeError("битый prompts.yaml")

        monkeypatch.setattr("bot.pipeline.load_prompts", _broken_yaml)

        with pytest.raises(RuntimeError):
            asyncio.run(process_job(deps, _job(extractor, thread.id, status=status)))

        assert len(thread.sent) == 1
        assert "упс: LLM недоступна" in thread.sent[0]
        assert status.deleted
        assert disk_reads == [], "error-путь не должен перечитывать prompts.yaml"

    def test_error_message_strips_mentions_from_reason(
        self, make_deps, make_thread, make_status, make_llm, make_extractor, make_router
    ):
        # LLM/провайдер могут вернуть текст с mention: фрагмент сырья уходит в
        # {reason}, но бот не должен тегать людей в треде
        thread = make_thread(4242)
        status = make_status()
        extractor = make_extractor(transcripts={URL: "субтитры"})
        deps = make_deps(
            thread=thread,
            llm=make_llm(raw="@everyone сломалось, пинг <@123"),
            router=make_router([extractor]),
        )

        with pytest.raises(ReportParseError):
            asyncio.run(process_job(deps, _job(extractor, thread.id, status=status)))

        assert len(thread.sent) == 1
        sent = thread.sent[0]
        assert "сломалось" in sent, "причина ошибки попала в сообщение"
        assert "@everyone" not in sent, "mention @everyone вырезан из {reason}"
        assert "<@" not in sent, "начало <@-пинга вырезано из {reason}"
        assert status.deleted

    def test_unknown_kind_posts_error_and_raises(
        self, make_deps, make_thread, make_status, make_extractor, make_router
    ):
        # kind джобы нет в реестре экстракторов: error-путь с человеческим
        # текстом (а не AttributeError на None.extract); исключение
        # пробрасывается — воркер пометит джобу failed
        thread = make_thread(4242)
        status = make_status()
        extractor = make_extractor(transcripts={URL: "субтитры"})
        deps = make_deps(thread=thread, router=make_router([extractor]))
        job = _job(extractor, thread.id, status=status)
        job["kind"] = "gone"

        with pytest.raises(RuntimeError):
            asyncio.run(process_job(deps, job))

        assert any("неизвестный тип источника: gone" in c for c in thread.sent), (
            "человеческий текст причины в треде"
        )
        assert status.deleted

    def test_send_failure_on_error_buffers_message_and_keeps_status(
        self, make_deps, make_client, make_status, make_extractor, make_router
    ):
        # Discord лежит И экстрактор упал: error-сообщение буферизуется, статус
        # НЕ закрывается (симметрично success-ветке), исключение пробрасывается —
        # воркер пометит джобу failed
        thread = FailAfterThread(4242, ok_sends=0)
        status = make_status()
        extractor = make_extractor(extract_error=RuntimeError("нет доступных субтитров"))
        deps = make_deps(client=make_client({4242: thread}), router=make_router([extractor]))

        with pytest.raises(RuntimeError):
            asyncio.run(process_job(deps, _job(extractor, thread.id, status=status)))

        assert thread.sent, "попытки отправки error-сообщения были"
        items = deps.outbox.load()
        assert len(items) == 1 and "нет доступных субтитров" in items[0]["content"], (
            "error-сообщение целиком в буфере"
        )
        assert all(i["thread_id"] == thread.id for i in items)
        assert not status.deleted, "при буферизации статусное сообщение остаётся"


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
    msgs[0] = f"**Разбор: <{LINK}>**\n\n{msgs[0]}"
    return [
        render.truncate_message(render.format_for_discord(msg), deps.config.split_threshold)[
            : deps.config.max_split_messages
        ]
        for msg in msgs
    ]


class TestOfflineDiscord:
    def test_send_failure_buffers_full_tail_of_report(
        self, make_deps, make_client, make_status, make_llm, make_extractor, make_router
    ):
        # кратко и тезисы доставлены; первое сообщение-раздел падает на первом
        # чанке — в буфер уходит ПОЛНЫЙ хвост отчёта: все чанки упавшего сообщения
        # плюс ВСЕ последующие сообщения, в порядке рендера (конец не теряется)
        ok_sends = 2
        thread = FailAfterThread(4242, ok_sends=ok_sends)
        status = make_status()
        extractor = make_extractor(transcripts={URL: "субтитры"})
        deps = make_deps(
            client=make_client({4242: thread}),
            llm=make_llm(analysis_json=BIG_ANALYSIS),
            router=make_router([extractor]),
        )

        asyncio.run(process_job(deps, _job(extractor, thread.id, status=status)))

        chunks_per_msg = _expected_chunks(deps)
        flat = [c for chunks in chunks_per_msg for c in chunks]

        # первые два сообщения доставлены целиком
        assert thread.sent[:ok_sends] == flat[:ok_sends]
        # упавший чанк — первый чанк следующего сообщения (первое «Подробно»)
        assert thread.sent[ok_sends] == chunks_per_msg[2][0]
        # в буфере полный хвост: упавшее сообщение целиком + все последующие
        items = deps.outbox.load()
        buffered = [i["content"] for i in items]
        assert buffered == chunks_per_msg[2] + [c for cs in chunks_per_msg[3:] for c in cs]
        assert len(buffered) > len(chunks_per_msg[2]), "буферизованы и последующие сообщения отчёта"
        # порядок: первый буферизованный — упавший чанк; последний содержит текст
        # ПОСЛЕДНЕГО сообщения отчёта (хвост раздела 19) — конец разбора не потерян
        assert buffered[0] == chunks_per_msg[2][0]
        assert buffered[-1] == flat[-1]
        assert "короткое описание 19" in buffered[-1]
        assert all(i["thread_id"] == thread.id for i in items)
        # отправка не завершена — статусное сообщение остаётся
        assert not status.deleted

    def test_send_failure_on_first_chunk_buffers_entire_report(
        self, make_deps, make_client, make_status, make_llm, make_extractor, make_router
    ):
        # Discord лежит с самого начала: буферизуются ВСЕ чанки ВСЕХ сообщений
        thread = FailAfterThread(4242, ok_sends=0)
        status = make_status()
        extractor = make_extractor(transcripts={URL: "субтитры"})
        deps = make_deps(
            client=make_client({4242: thread}),
            llm=make_llm(analysis_json=BIG_ANALYSIS),
            router=make_router([extractor]),
        )

        asyncio.run(process_job(deps, _job(extractor, thread.id, status=status)))

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
