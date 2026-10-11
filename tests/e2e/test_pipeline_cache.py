"""E2E кеша отчётов (фаза 1): повторная ссылка отвечает из SQLite-кеша мгновенно.

Контракт ключа: (canonical_video_id(video_id), prompts_ver, REPORT_SCHEMA),
где prompts_ver = str(deps.prompts.get("version", "0")). Попадание — без
скачивания транскрипта и без вызова LLM; ошибки транскрипта/LLM не кешируются;
отправка/буферизация/статусы — без изменений.

В этих тестах deps.reports — настоящий Store во временном файле: кеш проверяется
через ReportStore.get (контракт) и переживает «перезапуск» — новый Store на том
же файле. Дискорд/LLM/транскрипт подменены, сети нет.
"""

import asyncio

import pytest

from bot.pipeline import REPORT_SCHEMA, process_job
from bot.sources.youtube import canonical_video_id
from bot.store.db import Store

VIDEO_ID = "cachevid001"
THREAD_ID = 4242
PROMPTS_V1 = {"version": "1", "error": "упс: {reason}"}


def _patch_transcript_counter(monkeypatch, fail_for=None):
    """Подменяет fetch_transcript счётчиком; возвращает список скачанных video_id."""

    calls = []

    def _fake(video_id):
        calls.append(video_id)
        if fail_for and video_id in fail_for:
            raise fail_for[video_id]
        return f"субтитры {video_id}"

    monkeypatch.setattr("bot.pipeline.fetch_transcript", _fake)
    return calls


def _cache_key(deps, video_id):
    """Ключ кеша ровно по контракту фазы 1 (зеркало логики пайплайна)."""
    return (canonical_video_id(video_id), str(deps.prompts.get("version", "0")), REPORT_SCHEMA)


@pytest.fixture
def store(tmp_path):
    """Настоящий Store во временном файле — его репозитории уходят в deps."""
    s = Store(str(tmp_path / "cache.sqlite3"))
    yield s
    s.close()


class TestCacheMiss:
    def test_first_job_calls_llm_and_stores_report(
        self, monkeypatch, make_deps, make_thread, make_llm, store
    ):
        thread = make_thread(THREAD_ID)
        deps = make_deps(
            thread=thread,
            llm=make_llm(summary="итог разбора"),
            prompts=dict(PROMPTS_V1),
            reports=store.reports,
        )
        fetches = _patch_transcript_counter(monkeypatch)

        asyncio.run(process_job(deps, {"video_id": VIDEO_ID, "thread_id": THREAD_ID}))

        assert deps.llm.calls == [f"субтитры {VIDEO_ID}"], "LLM вызван ровно один раз"
        assert fetches == [VIDEO_ID], "транскрипт скачан"
        assert store.reports.get(*_cache_key(deps, VIDEO_ID)) == "итог разбора"


class TestCacheHit:
    def test_second_job_served_from_cache(
        self, monkeypatch, make_deps, make_thread, make_llm, store
    ):
        thread = make_thread(THREAD_ID)
        deps = make_deps(
            thread=thread,
            llm=make_llm(summary="итог разбора"),
            prompts=dict(PROMPTS_V1),
            reports=store.reports,
        )
        fetches = _patch_transcript_counter(monkeypatch)

        asyncio.run(process_job(deps, {"video_id": VIDEO_ID, "thread_id": THREAD_ID}))
        first_sent = list(thread.sent)

        asyncio.run(process_job(deps, {"video_id": VIDEO_ID, "thread_id": THREAD_ID}))

        assert len(deps.llm.calls) == 1, "повторный job не должен вызывать LLM"
        assert fetches == [VIDEO_ID], "повторный job не должен скачивать транскрипт"
        assert thread.sent == first_sent * 2, "сообщение из кеша идентично первому"

    def test_cache_survives_restart(self, monkeypatch, make_deps, make_thread, make_llm, tmp_path):
        # кеш живёт в файле БД: новый Store на том же файле (аналог перезапуска
        # процесса) отвечает из кеша — в этом смысл фазы 1
        path = str(tmp_path / "cache.sqlite3")
        thread = make_thread(THREAD_ID)
        llm = make_llm(summary="итог разбора")
        prompts = dict(PROMPTS_V1)
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
            assert fetches == [VIDEO_ID], "транскрипт не перекачивается"
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
            llm=make_llm(summary="итог"),
            prompts=dict(PROMPTS_V1),
            reports=store.reports,
        )
        fetches = _patch_transcript_counter(monkeypatch)

        asyncio.run(process_job(deps, {"video_id": "aaaaaaaaaaa", "thread_id": THREAD_ID}))
        asyncio.run(process_job(deps, {"video_id": "bbbbbbbbbbb", "thread_id": THREAD_ID}))

        assert len(deps.llm.calls) == 2, "разные видео — оба мимо кеша"
        assert fetches == ["aaaaaaaaaaa", "bbbbbbbbbbb"]
        assert store.reports.get(*_cache_key(deps, "aaaaaaaaaaa")) == "итог"
        assert store.reports.get(*_cache_key(deps, "bbbbbbbbbbb")) == "итог"

    def test_prompts_version_change_invalidates_cache(
        self, monkeypatch, make_deps, make_thread, make_llm, store
    ):
        thread = make_thread(THREAD_ID)
        llm = make_llm(summary="итог")
        _patch_transcript_counter(monkeypatch)

        deps_v1 = make_deps(
            thread=thread,
            llm=llm,
            prompts={"version": "1", "error": "упс: {reason}"},
            reports=store.reports,
        )
        asyncio.run(process_job(deps_v1, {"video_id": VIDEO_ID, "thread_id": THREAD_ID}))

        deps_v2 = make_deps(
            thread=thread,
            llm=llm,
            prompts={"version": "2", "error": "упс: {reason}"},
            reports=store.reports,
        )
        asyncio.run(process_job(deps_v2, {"video_id": VIDEO_ID, "thread_id": THREAD_ID}))

        assert len(llm.calls) == 2, "новая версия промптов — обязательный промах"
        # обе версии отчёта лежат в кеше под своими ключами
        assert store.reports.get(*_cache_key(deps_v1, VIDEO_ID)) == "итог"
        assert store.reports.get(*_cache_key(deps_v2, VIDEO_ID)) == "итог"


class TestErrorsNotCached:
    def test_llm_failure_is_not_cached(
        self, monkeypatch, make_deps, make_thread, make_llm, store
    ):
        thread = make_thread(THREAD_ID)
        deps = make_deps(
            thread=thread,
            llm=make_llm(error=RuntimeError("LLM недоступна")),
            prompts=dict(PROMPTS_V1),
            reports=store.reports,
        )
        _patch_transcript_counter(monkeypatch)

        asyncio.run(process_job(deps, {"video_id": VIDEO_ID, "thread_id": THREAD_ID}))

        assert len(thread.sent) == 1, "пользователь получил сообщение об ошибке"
        assert store.reports.get(*_cache_key(deps, VIDEO_ID)) is None, "ошибка не кешируется"

    def test_transcript_failure_is_not_cached(
        self, monkeypatch, make_deps, make_thread, make_llm, store
    ):
        thread = make_thread(THREAD_ID)
        deps = make_deps(
            thread=thread,
            llm=make_llm(summary="итог"),
            prompts=dict(PROMPTS_V1),
            reports=store.reports,
        )
        _patch_transcript_counter(monkeypatch, fail_for={VIDEO_ID: RuntimeError("нет субтитров")})

        asyncio.run(process_job(deps, {"video_id": VIDEO_ID, "thread_id": THREAD_ID}))

        assert len(thread.sent) == 1
        assert store.reports.get(*_cache_key(deps, VIDEO_ID)) is None, "ошибка не кешируется"


def test_report_schema_constant_pinned():
    # фаза 2 сменит формат отчёта — константу поднимут, и старый кеш
    # инвалидируется сам; пока формат один: summary-1
    assert REPORT_SCHEMA == "summary-1"
