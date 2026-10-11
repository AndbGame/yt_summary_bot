"""E2E кеша отчётов (фаза 2): повторная ссылка отвечает из SQLite-кеша мгновенно.

Контракт ключа: (canonical_video_id(video_id), prompts_ver, REPORT_SCHEMA,
content_hash), где content_hash = sha256(транскрипта).hexdigest()[:16].
Транскрипт теперь скачивается ВСЕГДА (нужен для хэша), но попадание в кеш не
зовёт LLM и не зовёт его повторно; payload в кеше — JSON отчёта (Report.to_json,
читается обратно через Report.from_json). Ошибки транскрипта/LLM не кешируются;
при НЕдоступном транскрипте отдаётся последний разбор из кеша (фолбэк по тройке
ключей, без content_hash). Отправка/буферизация/статусы — без изменений.

В этих тестах deps.reports — настоящий Store во временном файле: кеш проверяется
через ReportStore.get (контракт) и переживает «перезапуск» — новый Store на том
же файле. Дискорд/LLM/транскрипт подменены, сети нет.
"""

import asyncio
import hashlib

import pytest

from bot.pipeline import REPORT_SCHEMA, process_job
from bot.sources.youtube import canonical_video_id
from bot.store.db import Store

VIDEO_ID = "cachevid001"
THREAD_ID = 4242
PROMPTS = {"version": "2", "error": "упс: {reason}"}


def _content_hash(transcript: str) -> str:
    """Хэш транскрипта ровно по контракту пайплайна (зеркало логики)."""
    return hashlib.sha256(transcript.encode()).hexdigest()[:16]


def _patch_transcript_counter(monkeypatch, transcripts=None, fail_for=None):
    """Подменяет fetch_transcript счётчиком; возвращает список скачанных video_id.

    transcripts — необязательная очередь ответов (для сценария «сменился
    транскрипт»); по умолчанию f"субтитры {video_id}".
    """
    calls = []
    answers = list(transcripts) if transcripts is not None else None

    def _fake(video_id):
        calls.append(video_id)
        if fail_for and video_id in fail_for:
            raise fail_for[video_id]
        if answers:
            return answers.pop(0)
        return f"субтитры {video_id}"

    monkeypatch.setattr("bot.pipeline.fetch_transcript", _fake)
    return calls


def _cache_key(deps, video_id, transcript=None):
    """Ключ кеша ровно по контракту фазы 2 (зеркало логики пайплайна)."""
    if transcript is None:
        transcript = f"субтитры {video_id}"
    return (
        canonical_video_id(video_id),
        str(deps.prompts.get("version", "0")),
        REPORT_SCHEMA,
        _content_hash(transcript),
    )


@pytest.fixture
def store(tmp_path):
    """Настоящий Store во временном файле — его репозитории уходят в deps."""
    s = Store(str(tmp_path / "cache.sqlite3"))
    yield s
    s.close()


class TestCacheMiss:
    def test_first_job_calls_llm_and_stores_report_json(
        self, monkeypatch, make_deps, make_thread, make_llm, store
    ):
        thread = make_thread(THREAD_ID)
        deps = make_deps(
            thread=thread,
            llm=make_llm(),
            prompts=dict(PROMPTS),
            reports=store.reports,
        )
        fetches = _patch_transcript_counter(monkeypatch)

        asyncio.run(process_job(deps, {"video_id": VIDEO_ID, "thread_id": THREAD_ID}))

        assert deps.llm.calls == [f"субтитры {VIDEO_ID}"], "LLM вызван ровно один раз"
        assert fetches == [VIDEO_ID], "транскрипт скачан"
        payload = store.reports.get(*_cache_key(deps, VIDEO_ID))
        assert payload is not None
        # payload — JSON отчёта: разбирается обратно в Report с блоками кратко и тезисов
        from bot.analysis.report import Report  # лениво: TDD

        report = Report.from_json(payload)
        assert [b.kind for b in report.blocks] == ["brief", "theses"]


class TestCacheHit:
    def test_second_job_served_from_cache(
        self, monkeypatch, make_deps, make_thread, make_llm, store
    ):
        thread = make_thread(THREAD_ID)
        deps = make_deps(
            thread=thread,
            llm=make_llm(),
            prompts=dict(PROMPTS),
            reports=store.reports,
        )
        fetches = _patch_transcript_counter(monkeypatch)

        asyncio.run(process_job(deps, {"video_id": VIDEO_ID, "thread_id": THREAD_ID}))
        first_sent = list(thread.sent)

        asyncio.run(process_job(deps, {"video_id": VIDEO_ID, "thread_id": THREAD_ID}))

        assert len(deps.llm.calls) == 1, "повторный job не должен вызывать LLM"
        assert fetches == [VIDEO_ID, VIDEO_ID], (
            "транскрипт скачивается всегда (нужен для content_hash), но без LLM"
        )
        assert thread.sent == first_sent * 2, "сообщения из кеша идентичны первому разу"

    def test_cache_survives_restart(self, monkeypatch, make_deps, make_thread, make_llm, tmp_path):
        # кеш живёт в файле БД: новый Store на том же файле (аналог перезапуска
        # процесса) отвечает из кеша — в этом смысл фазы 1
        path = str(tmp_path / "cache.sqlite3")
        thread = make_thread(THREAD_ID)
        llm = make_llm()
        prompts = dict(PROMPTS)
        fetches = _patch_transcript_counter(monkeypatch)

        first = Store(path)
        deps1 = make_deps(thread=thread, llm=llm, prompts=prompts, reports=first.reports)
        asyncio.run(process_job(deps1, {"video_id": VIDEO_ID, "thread_id": THREAD_ID}))
        first_sent = list(thread.sent)
        first.close()

        second = Store(path)
        try:
            deps2 = make_deps(thread=thread, llm=llm, prompts=prompts, reports=second.reports)
            asyncio.run(process_job(deps2, {"video_id": VIDEO_ID, "thread_id": THREAD_ID}))

            assert len(llm.calls) == 1, "после «перезапуска» LLM не вызывается снова"
            assert fetches == [VIDEO_ID, VIDEO_ID], "LLM не зовётся, но транскрипт нужен для хэша"
            assert thread.sent == first_sent * 2
        finally:
            second.close()


class TestCacheIsolation:
    def test_other_video_is_cache_miss(
        self, monkeypatch, make_deps, make_thread, make_llm, store
    ):
        thread = make_thread(THREAD_ID)
        deps = make_deps(
            thread=thread,
            llm=make_llm(),
            prompts=dict(PROMPTS),
            reports=store.reports,
        )
        fetches = _patch_transcript_counter(monkeypatch)

        asyncio.run(process_job(deps, {"video_id": "aaaaaaaaaaa", "thread_id": THREAD_ID}))
        asyncio.run(process_job(deps, {"video_id": "bbbbbbbbbbb", "thread_id": THREAD_ID}))

        assert len(deps.llm.calls) == 2, "разные видео — оба мимо кеша"
        assert fetches == ["aaaaaaaaaaa", "bbbbbbbbbbb"]
        assert store.reports.get(*_cache_key(deps, "aaaaaaaaaaa")) is not None
        assert store.reports.get(*_cache_key(deps, "bbbbbbbbbbb")) is not None

    def test_prompts_version_change_invalidates_cache(
        self, monkeypatch, make_deps, make_thread, make_llm, store
    ):
        thread = make_thread(THREAD_ID)
        llm = make_llm()
        _patch_transcript_counter(monkeypatch)

        deps_v2 = make_deps(
            thread=thread,
            llm=llm,
            prompts={"version": "2", "error": "упс: {reason}"},
            reports=store.reports,
        )
        asyncio.run(process_job(deps_v2, {"video_id": VIDEO_ID, "thread_id": THREAD_ID}))

        deps_v3 = make_deps(
            thread=thread,
            llm=llm,
            prompts={"version": "3", "error": "упс: {reason}"},
            reports=store.reports,
        )
        asyncio.run(process_job(deps_v3, {"video_id": VIDEO_ID, "thread_id": THREAD_ID}))

        assert len(llm.calls) == 2, "новая версия промптов — обязательный промах"
        # обе версии отчёта лежат в кеше под своими ключами
        assert store.reports.get(*_cache_key(deps_v2, VIDEO_ID)) is not None
        assert store.reports.get(*_cache_key(deps_v3, VIDEO_ID)) is not None

    def test_transcript_change_invalidates_cache_for_same_video(
        self, monkeypatch, make_deps, make_thread, make_llm, store
    ):
        # тот же video_id, но субтитры изменились -> другой content_hash -> промах;
        # новый отчёт замещает прежний (PK в reports — тройка ключей, content_hash
        # — обычная колонка сравнения)
        thread = make_thread(THREAD_ID)
        llm = make_llm()
        _patch_transcript_counter(
            monkeypatch, transcripts=["первые субтитры", "обновлённые субтитры"]
        )

        deps1 = make_deps(thread=thread, llm=llm, prompts=dict(PROMPTS), reports=store.reports)
        asyncio.run(process_job(deps1, {"video_id": VIDEO_ID, "thread_id": THREAD_ID}))

        deps2 = make_deps(thread=thread, llm=llm, prompts=dict(PROMPTS), reports=store.reports)
        asyncio.run(process_job(deps2, {"video_id": VIDEO_ID, "thread_id": THREAD_ID}))

        assert len(llm.calls) == 2, "новый транскрипт — обязательный промах при том же video_id"
        assert store.reports.get(*_cache_key(deps2, VIDEO_ID, "обновлённые субтитры")) is not None
        assert store.reports.get(*_cache_key(deps1, VIDEO_ID, "первые субтитры")) is None, (
            "старый отчёт замещён записью с новым content_hash"
        )


class TestErrorsNotCached:
    def test_llm_failure_is_not_cached(
        self, monkeypatch, make_deps, make_thread, make_llm, store
    ):
        thread = make_thread(THREAD_ID)
        deps = make_deps(
            thread=thread,
            llm=make_llm(error=RuntimeError("LLM недоступна")),
            prompts=dict(PROMPTS),
            reports=store.reports,
        )
        _patch_transcript_counter(monkeypatch)

        asyncio.run(process_job(deps, {"video_id": VIDEO_ID, "thread_id": THREAD_ID}))

        assert len(thread.sent) == 1, "пользователь получил сообщение об ошибке"
        assert store.reports.get(*_cache_key(deps, VIDEO_ID)) is None, "ошибка не кешируется"

    def test_transcript_failure_is_not_cached(
        self, monkeypatch, make_deps, make_thread, make_llm, store
    ):
        # кеш пуст: фолбэк взять неоткуда — обычная error-ветка
        thread = make_thread(THREAD_ID)
        deps = make_deps(
            thread=thread,
            llm=make_llm(),
            prompts=dict(PROMPTS),
            reports=store.reports,
        )
        _patch_transcript_counter(monkeypatch, fail_for={VIDEO_ID: RuntimeError("нет субтитров")})

        asyncio.run(process_job(deps, {"video_id": VIDEO_ID, "thread_id": THREAD_ID}))

        assert len(thread.sent) == 1
        assert "упс" in thread.sent[0], "пустой кеш — обычное error-сообщение"
        assert store.reports.get(*_cache_key(deps, VIDEO_ID)) is None, "ошибка не кешируется"


class TestTranscriptUnavailableFallback:
    """Субтитры исчезли (fetch_transcript падает) — отдаём последний разбор из кеша.

    Фолбэк ищет запись по тройке (canonical, prompts_ver, schema) с ЛЮБЫМ
    content_hash: свежий хэш посчитать не из чего, субтитров больше нет.
    Трейд-офф «свежесть vs доступность» зафиксирован комментарием в pipeline.py.
    """

    def test_transcript_failure_serves_latest_cache_report(
        self, monkeypatch, make_deps, make_thread, make_llm, store
    ):
        thread = make_thread(THREAD_ID)
        deps = make_deps(
            thread=thread,
            llm=make_llm(),
            prompts=dict(PROMPTS),
            reports=store.reports,
        )
        # в кеше лежит разбор под content_hash ПРЕЖНИХ (теперь недоступных) субтитров
        from bot.analysis.report import Report  # лениво: TDD

        cached_payload = Report.from_analysis(
            {
                "brief": "Краткая суть из кеша",
                "complexity": "simple",
                "context": "",
                "theses": ["тезис из кеша"],
                "sections": [],
            }
        ).to_json()
        store.reports.put(
            canonical_video_id(VIDEO_ID),
            str(deps.prompts.get("version", "0")),
            REPORT_SCHEMA,
            cached_payload,
            content_hash=_content_hash("прежние субтитры"),
        )
        _patch_transcript_counter(monkeypatch, fail_for={VIDEO_ID: RuntimeError("нет субтитров")})

        asyncio.run(process_job(deps, {"video_id": VIDEO_ID, "thread_id": THREAD_ID}))

        assert deps.llm.calls == [], "LLM не вызывается — отчёт из кеша"
        assert thread.sent, "пользователь получил сообщения"
        sent_text = "\n".join(thread.sent)
        assert "упс" not in sent_text, "это отчёт из кеша, а не error-сообщение"
        assert "**Кратко**" in sent_text and "Краткая суть из кеша" in sent_text
        assert "тезис из кеша" in sent_text
        # кеш не перезаписан: запись с прежним content_hash осталась последней
        assert (
            store.reports.get_latest(canonical_video_id(VIDEO_ID), "2", REPORT_SCHEMA)
            == cached_payload
        )


def test_report_schema_constant_pinned():
    # фаза 2 перевела отчёт на блоки — константа поднята, старый кеш
    # (summary-1, без content_hash в ключе) инвалидируется сам
    assert REPORT_SCHEMA == "blocks-1"
