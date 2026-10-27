"""E2E кеша отчётов (фаза 2 + джобы фазы 4): повторная ссылка отвечает из SQLite-кеша.

Контракт ключа: (canonical_id, prompts_ver, REPORT_SCHEMA, content_hash), где
canonical_id джоба получает от экстрактора (фаза 4), а content_hash =
sha256(контента).hexdigest()[:16]. Контент добывается ВСЕГДА (нужен для хэша),
но попадание в кеш не зовёт LLM и не зовёт его повторно; payload в кеше — JSON
отчёта (Report.to_json, читается обратно через Report.from_json). Ошибки
контента/LLM не кешируются; при НЕдоступном контенте отдаётся последний разбор
из кеша (фолбэк по тройке ключей, без content_hash) — и это НЕ ошибка: отчёт
доставлен, джоба завершается успешно. Отправка/буферизация/статусы — без
изменений.

В этих тестах deps.reports — настоящий Store во временном файле: кеш проверяется
через ReportStore.get (контракт) и переживает «перезапуск» — новый Store на том
же файле. Экстрактор — FakeExtractor из conftest в deps.router, LLM/Дискорд
подменены, сети нет.
"""

import asyncio
import hashlib

import pytest

from bot.pipeline import REPORT_SCHEMA, process_job
from bot.store.db import Store

VIDEO_ID = "cachevid001"
URL = f"https://youtu.be/{VIDEO_ID}"
TRANSCRIPT = f"субтитры {VIDEO_ID}"
THREAD_ID = 4242
PROMPTS = {"version": "2"}


def _content_hash(transcript: str) -> str:
    """Хэш контента ровно по контракту пайплайна (зеркало логики)."""
    return hashlib.sha256(transcript.encode()).hexdigest()[:16]


def _cache_key(extractor, deps, url=URL, transcript=TRANSCRIPT):
    """Ключ кеша по контракту (зеркало логики пайплайна фазы 4)."""
    return (
        extractor.canonical_id(url),
        str(deps.prompts.get("version", "0")),
        REPORT_SCHEMA,
        _content_hash(transcript),
    )


def _job(extractor, thread_id=THREAD_ID, url=URL, **extra):
    job = {
        "kind": extractor.name,
        "url": url,
        "canonical_id": extractor.canonical_id(url),
        "thread_id": thread_id,
    }
    job.update(extra)
    return job


@pytest.fixture
def store(tmp_path):
    """Настоящий Store во временном файле — его репозитории уходят в deps."""
    s = Store(str(tmp_path / "cache.sqlite3"))
    yield s
    s.close()


class TestCacheMiss:
    def test_first_job_calls_llm_and_stores_report_json(
        self, make_deps, make_thread, make_llm, make_extractor, make_router, store
    ):
        thread = make_thread(THREAD_ID)
        extractor = make_extractor(transcripts={URL: TRANSCRIPT})
        deps = make_deps(
            thread=thread,
            llm=make_llm(),
            prompts=dict(PROMPTS),
            reports=store.reports,
            router=make_router([extractor]),
        )

        asyncio.run(process_job(deps, _job(extractor)))

        assert deps.llm.calls == [TRANSCRIPT], "LLM вызван ровно один раз"
        assert extractor.extract_calls == [URL], "контент добыт экстрактором"
        payload = store.reports.get(*_cache_key(extractor, deps))
        assert payload is not None
        # payload — JSON отчёта: разбирается обратно в Report с блоками кратко и тезисов
        from bot.analysis.report import Report

        report = Report.from_json(payload)
        assert [b.kind for b in report.blocks] == ["brief", "theses"]


class TestCacheHit:
    def test_second_job_served_from_cache(
        self, make_deps, make_thread, make_llm, make_extractor, make_router, store
    ):
        thread = make_thread(THREAD_ID)
        extractor = make_extractor(transcripts={URL: TRANSCRIPT})
        deps = make_deps(
            thread=thread,
            llm=make_llm(),
            prompts=dict(PROMPTS),
            reports=store.reports,
            router=make_router([extractor]),
        )

        asyncio.run(process_job(deps, _job(extractor)))
        first_sent = list(thread.sent)

        asyncio.run(process_job(deps, _job(extractor)))

        assert len(deps.llm.calls) == 1, "повторный job не должен вызывать LLM"
        assert extractor.extract_calls == [URL, URL], (
            "контент добывается всегда (нужен для content_hash), но без LLM"
        )
        assert thread.sent == first_sent * 2, "сообщения из кеша идентичны первому разу"

    def test_cache_survives_restart(
        self, make_deps, make_thread, make_llm, make_extractor, make_router, tmp_path
    ):
        # кеш живёт в файле БД: новый Store на том же файле (аналог перезапуска
        # процесса) отвечает из кеша — в этом смысл фазы 1
        path = str(tmp_path / "cache.sqlite3")
        thread = make_thread(THREAD_ID)
        llm = make_llm()
        prompts = dict(PROMPTS)
        extractor = make_extractor(transcripts={URL: TRANSCRIPT})
        router = make_router([extractor])

        first = Store(path)
        deps1 = make_deps(
            thread=thread, llm=llm, prompts=prompts, reports=first.reports, router=router
        )
        asyncio.run(process_job(deps1, _job(extractor)))
        first_sent = list(thread.sent)
        first.close()

        second = Store(path)
        try:
            deps2 = make_deps(
                thread=thread, llm=llm, prompts=prompts, reports=second.reports, router=router
            )
            asyncio.run(process_job(deps2, _job(extractor)))

            assert len(llm.calls) == 1, "после «перезапуска» LLM не вызывается снова"
            assert extractor.extract_calls == [URL, URL], (
                "LLM не зовётся, но контент нужен для хэша"
            )
            assert thread.sent == first_sent * 2
        finally:
            second.close()


class TestCacheIsolation:
    def test_other_video_is_cache_miss(
        self, make_deps, make_thread, make_llm, make_extractor, make_router, store
    ):
        thread = make_thread(THREAD_ID)
        url_other = "https://youtu.be/aaaaaaaaaaa"
        extractor = make_extractor(transcripts={URL: "субтитры первого", url_other: "субтитры второго"})
        deps = make_deps(
            thread=thread,
            llm=make_llm(),
            prompts=dict(PROMPTS),
            reports=store.reports,
            router=make_router([extractor]),
        )

        asyncio.run(process_job(deps, _job(extractor, url=URL)))
        asyncio.run(process_job(deps, _job(extractor, url=url_other)))

        assert len(deps.llm.calls) == 2, "разные источники — оба мимо кеша"
        assert extractor.extract_calls == [URL, url_other]
        assert store.reports.get(*_cache_key(extractor, deps, url=URL, transcript="субтитры первого")) is not None
        assert (
            store.reports.get(*_cache_key(extractor, deps, url=url_other, transcript="субтитры второго"))
            is not None
        )

    def test_prompts_version_change_invalidates_cache(
        self, make_deps, make_thread, make_llm, make_extractor, make_router, store
    ):
        thread = make_thread(THREAD_ID)
        llm = make_llm()
        extractor = make_extractor(transcripts={URL: TRANSCRIPT})
        router = make_router([extractor])

        deps_v2 = make_deps(
            thread=thread,
            llm=llm,
            prompts={"version": "2"},
            reports=store.reports,
            router=router,
        )
        asyncio.run(process_job(deps_v2, _job(extractor)))

        deps_v3 = make_deps(
            thread=thread,
            llm=llm,
            prompts={"version": "3"},
            reports=store.reports,
            router=router,
        )
        asyncio.run(process_job(deps_v3, _job(extractor)))

        assert len(llm.calls) == 2, "новая версия промптов — обязательный промах"
        # обе версии отчёта лежат в кеше под своими ключами
        assert store.reports.get(*_cache_key(extractor, deps_v2)) is not None
        assert store.reports.get(*_cache_key(extractor, deps_v3)) is not None

    def test_content_change_invalidates_cache_for_same_source(
        self, make_deps, make_thread, make_llm, make_extractor, make_router, store
    ):
        # тот же url, но контент изменился -> другой content_hash -> промах;
        # новый отчёт замещает прежний (PK в reports — тройка ключей, content_hash
        # — обычная колонка сравнения)
        thread = make_thread(THREAD_ID)
        llm = make_llm()
        extractor = make_extractor(transcripts={URL: "первые субтитры"})
        router = make_router([extractor])

        deps1 = make_deps(
            thread=thread, llm=llm, prompts=dict(PROMPTS), reports=store.reports, router=router
        )
        asyncio.run(process_job(deps1, _job(extractor)))

        extractor.transcripts[URL] = "обновлённые субтитры"  # контент изменился
        deps2 = make_deps(
            thread=thread, llm=llm, prompts=dict(PROMPTS), reports=store.reports, router=router
        )
        asyncio.run(process_job(deps2, _job(extractor)))

        assert len(llm.calls) == 2, "новый контент — обязательный промах при том же url"
        new_key = _cache_key(extractor, deps2, transcript="обновлённые субтитры")
        old_key = _cache_key(extractor, deps1, transcript="первые субтитры")
        assert store.reports.get(*new_key) is not None
        assert store.reports.get(*old_key) is None, "старый отчёт замещён записью с новым content_hash"


class TestErrorsNotCached:
    def test_llm_failure_is_not_cached(
        self, make_deps, make_thread, make_llm, make_extractor, make_router, store
    ):
        thread = make_thread(THREAD_ID)
        extractor = make_extractor(transcripts={URL: TRANSCRIPT})
        deps = make_deps(
            thread=thread,
            llm=make_llm(error=RuntimeError("LLM недоступна")),
            prompts=dict(PROMPTS),
            reports=store.reports,
            router=make_router([extractor]),
        )

        with pytest.raises(RuntimeError):
            asyncio.run(process_job(deps, _job(extractor)))

        assert thread.sent == [], "бот молчит: сообщений об ошибках нет"
        assert store.reports.get(*_cache_key(extractor, deps)) is None, "ошибка не кешируется"

    def test_extraction_failure_is_not_cached(
        self, make_deps, make_thread, make_llm, make_extractor, make_router, store
    ):
        # кеш пуст: фолбэк взять неоткуда — бот молчит, джоба падает (mark_failed)
        thread = make_thread(THREAD_ID)
        extractor = make_extractor(extract_error=RuntimeError("нет субтитров"))
        deps = make_deps(
            thread=thread,
            llm=make_llm(),
            prompts=dict(PROMPTS),
            reports=store.reports,
            router=make_router([extractor]),
        )

        with pytest.raises(RuntimeError):
            asyncio.run(process_job(deps, _job(extractor)))

        assert thread.sent == []
        assert store.reports.get(*_cache_key(extractor, deps)) is None, "ошибка не кешируется"


class TestContentUnavailableFallback:
    """Контент исчез (extract падает) — отдаём последний разбор из кеша.

    Фолбэк ищет запись по тройке (canonical, prompts_ver, schema) с ЛЮБЫМ
    content_hash: свежий хэш посчитать не из чего, контента больше нет.
    Трейд-офф «свежесть vs доступность» зафиксирован комментарием в pipeline.py.
    """

    def test_extraction_failure_serves_latest_cache_report(
        self, make_deps, make_thread, make_llm, make_extractor, make_router, store
    ):
        thread = make_thread(THREAD_ID)
        extractor = make_extractor()
        deps = make_deps(
            thread=thread,
            llm=make_llm(),
            prompts=dict(PROMPTS),
            reports=store.reports,
            router=make_router([extractor]),
        )
        # в кеше лежит разбор под content_hash ПРЕЖНЕГО (теперь недоступного) контента
        from bot.analysis.report import Report

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
            extractor.canonical_id(URL),
            str(deps.prompts.get("version", "0")),
            REPORT_SCHEMA,
            cached_payload,
            content_hash=_content_hash("прежние субтитры"),
        )
        extractor.extract_error = RuntimeError("нет субтитров")  # контент стал недоступен

        asyncio.run(process_job(deps, _job(extractor)))

        assert deps.llm.calls == [], "LLM не вызывается — отчёт из кеша"
        assert thread.sent, "пользователь получил сообщения"
        sent_text = "\n".join(thread.sent)
        assert "упс" not in sent_text, "это отчёт из кеша, а не error-сообщение"
        assert "**Кратко**" in sent_text and "Краткая суть из кеша" in sent_text
        assert "тезис из кеша" in sent_text
        # кеш не перезаписан: запись с прежним content_hash осталась последней
        assert (
            store.reports.get_latest(extractor.canonical_id(URL), "2", REPORT_SCHEMA)
            == cached_payload
        )


def test_report_schema_constant_pinned():
    # фаза 2 перевела отчёт на блоки — константа поднята, старый кеш
    # (summary-1, без content_hash в ключе) инвалидируется сам
    assert REPORT_SCHEMA == "blocks-1"
