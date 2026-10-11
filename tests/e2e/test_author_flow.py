"""E2E автор-пути (фазы 3-4): ref автора -> факты -> обзор LLM -> кеш с TTL -> фон.

Контракт (docs/ARCHITECTURE.md §5-§9; MISS-путь обновлён фазой 4):
- MISS (после analyze_content, до reports.put): провайдер даёт AuthorRef по url
  джобы (get_author_ref(url)), факты канала — отдельно (get_channel(ref));
  свежая строка authors -> обзор из кеша (LLM не зовётся); иначе review_author,
  строка обновляется, author-блок добавляется в отчёт ДО кеширования (кеш хранит
  отчёт уже с автором); сбой ref/фактов/обзора не роняет джобу — отчёт уходит
  без автора;
- HIT: провайдер и LLM не зовутся вовсе; свежая строка — ничего; отсутствующая
  или устаревшая — фоновая задача {platform, handle} в background_queue,
  пользователь СРАЗУ получает старый body (никогда не ждёт); author-блока нет —
  тихо пропустить;
- author_refresh_worker: {platform, handle} -> get_channel -> review -> authors.put
  (analyzed_at свежее), Discord не трогается; сбой — лог, воркер живёт;
- llm_lock: analyze_content конкурентных джоб не пересекаются (интервалы вызовов).

Модули, которых тесты касаются через контекст джобы фазы 4 (router), идут через
фикстуры conftest; сети нет: контент добывает FakeExtractor в deps.router,
провайдер/LLM — фейки, автор-хранилище и кеш отчётов — настоящий Store во
временном файле; устаревание строк — UPDATE отдельным соединением (put
настоящего хранилища штампует now).
"""

import asyncio
import hashlib
import logging
import sqlite3
import threading
import time
from types import SimpleNamespace

import pytest

from bot.analysis.report import Report, ReportBlock
from bot.config import BotConfig
from bot.pipeline import REPORT_SCHEMA, process_job
from bot.store.db import Store

VIDEO_ID = "authorvid01"
URL = f"https://youtu.be/{VIDEO_ID}"
TRANSCRIPT = f"субтитры {VIDEO_ID}"
CHANNEL_HANDLE = "UC1234567"
AUTHOR_URL = f"https://www.youtube.com/channel/{CHANNEL_HANDLE}"
STALE_ANALYZED_AT = int(time.time()) - 200 * 86400  # сильно старше любого TTL


def _content_hash(transcript):
    """Хэш контента ровно по контракту пайплайна (зеркало логики)."""
    return hashlib.sha256(transcript.encode("utf-8")).hexdigest()[:16]


def _cache_key(deps, extractor):
    """Ключ кеша отчётов: (canonical от экстрактора, prompts_ver, schema, content_hash)."""
    return (
        extractor.canonical_id(URL),
        str(deps.prompts.get("version", "0")),
        REPORT_SCHEMA,
        _content_hash(TRANSCRIPT),
    )


def _job(extractor, thread_id, **extra):
    """Джоба нового формата (фаза 4)."""
    job = {
        "kind": extractor.name,
        "url": URL,
        "canonical_id": extractor.canonical_id(URL),
        "thread_id": thread_id,
    }
    job.update(extra)
    return job


def _seed_report(deps, extractor, payload):
    canonical, prompts_ver, schema_ver, content_hash = _cache_key(deps, extractor)
    deps.reports.put(canonical, prompts_ver, schema_ver, payload, content_hash)


def _ref_facts(handle=CHANNEL_HANDLE, name="Имя Канала"):
    """(ref, facts) утиной типизации: пайплайн читает только атрибуты
    platform/handle и name/url — настоящие dataclass'ы фазы 3 совместимы."""
    ref = SimpleNamespace(platform="youtube", handle=handle)
    facts = SimpleNamespace(
        name=name, url=f"https://www.youtube.com/{handle}", description="о канале", extra={}
    )
    return ref, facts


def _payload_with_author(review="СТАРЫЙ ОБЗОР"):
    report = Report(
        [
            ReportBlock("brief", body_md="Суть видео."),
            ReportBlock("theses", body_md="- тезис один"),
            ReportBlock(
                "author",
                title="Обзор автора",
                body_md=review,
                data={
                    "platform": "youtube",
                    "handle": CHANNEL_HANDLE,
                    "name": "Имя Канала",
                    "url": AUTHOR_URL,
                },
            ),
        ]
    )
    return report.to_json()


def _payload_without_author():
    report = Report([ReportBlock("brief", body_md="Суть видео."), ReportBlock("theses", body_md="- тезис один")])
    return report.to_json()


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "authors_flow.sqlite3")


@pytest.fixture
def store(db_path):
    s = Store(db_path)
    yield s
    s.close()


def _age_author(db_path, platform, handle, analyzed_at):
    """Состаривает строку автора UPDATE'ом из отдельного соединения: put настоящего
    AuthorStore штампует now, «в прошлое» он писать не умеет (и не должен)."""
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "UPDATE authors SET analyzed_at = ?, refreshed_at = ? WHERE platform = ? AND handle = ?",
            (analyzed_at, analyzed_at, platform, handle),
        )
        conn.commit()
    finally:
        conn.close()


class _FailingGetAuthorStore:
    """Обёртка над автор-хранилищем: get/is_fresh бросают RuntimeError (сбой кеша).

    put делегируется настоящему store: сбой ЧТЕНИЯ не должен мешать записи
    нового обзора на MISS-пути.
    """

    def __init__(self, inner):
        self._inner = inner

    def get(self, platform, handle):
        raise RuntimeError("кеш автора недоступен")

    def is_fresh(self, row, ttl_seconds, now=None):
        raise RuntimeError("кеш автора недоступен")

    def put(self, *args, **kwargs):
        return self._inner.put(*args, **kwargs)


class TestAuthorStoreFailureDegradation:
    """Сбой authors.get/is_fresh — деградация до без-автор-блока, а не падение джобы."""

    def test_miss_get_failure_still_reviews_and_delivers(
        self, make_deps, make_thread, make_llm, make_author_facts, make_extractor, make_router, store
    ):
        # MISS: сбой чтения кеша трактуется как «строки нет» — обзор строится в LLM
        thread = make_thread(4242)
        ref, facts = _ref_facts()
        provider = make_author_facts(get_author_ref_result=ref, get_channel_result=facts)
        llm = make_llm()
        extractor = make_extractor(transcripts={URL: TRANSCRIPT})
        deps = make_deps(
            thread=thread,
            llm=llm,
            reports=store.reports,
            authors=_FailingGetAuthorStore(store.authors),
            author_facts=provider,
            router=make_router([extractor]),
        )

        asyncio.run(process_job(deps, _job(extractor, thread.id)))

        assert any("**Кратко**" in c for c in thread.sent), "отчёт доставлен"
        assert all("Не смог разобрать" not in c for c in thread.sent), "error-сообщения нет"
        assert llm.review_calls == [facts], "сбой кеша автора — как промах: обзор построен"
        assert thread.sent[-1] == "**Обзор автора**\n\nОбзор автора: тест"
        # запись нового обзора сквозь обёртку дошла до настоящего store
        assert store.authors.get("youtube", CHANNEL_HANDLE) is not None

    def test_hit_get_failure_delivers_without_background_task(
        self, make_deps, make_thread, make_llm, make_author_facts, make_extractor, make_router, store
    ):
        # HIT: свежесть строки неизвестна — фон не ставим, отчёт уже доставлен
        thread = make_thread(4242)
        ref, _ = _ref_facts()
        provider = make_author_facts(get_author_ref_result=ref)
        llm = make_llm()
        extractor = make_extractor(transcripts={URL: TRANSCRIPT})
        deps = make_deps(
            thread=thread,
            llm=llm,
            reports=store.reports,
            authors=_FailingGetAuthorStore(store.authors),
            author_facts=provider,
            router=make_router([extractor]),
        )
        _seed_report(deps, extractor, _payload_with_author("СТАРЫЙ ОБЗОР"))

        asyncio.run(process_job(deps, _job(extractor, thread.id)))

        assert thread.sent[-1] == "**Обзор автора**\n\nСТАРЫЙ ОБЗОР", "отчёт доставлен"
        assert deps.background_queue.qsize() == 0, "не зная свежести строки, фон не ставим"
        assert llm.calls == [] and llm.review_calls == []


class TestMissPath:
    """Первое видео канала: промах кеша отчётов — автор-обзор строится и кешируется."""

    def test_first_video_builds_review_and_caches_report_with_author_block(
        self, make_deps, make_thread, make_llm, make_author_facts, make_extractor, make_router, store
    ):
        thread = make_thread(4242)
        ref, facts = _ref_facts()
        provider = make_author_facts(get_author_ref_result=ref, get_channel_result=facts)
        llm = make_llm()  # review по умолчанию: «Обзор автора: тест»
        extractor = make_extractor(transcripts={URL: TRANSCRIPT})
        deps = make_deps(
            thread=thread,
            llm=llm,
            reports=store.reports,
            authors=store.authors,
            author_facts=provider,
            router=make_router([extractor]),
        )

        asyncio.run(process_job(deps, _job(extractor, thread.id)))

        # финальное сообщение треда — обзор автора (§9: автор последним)
        assert thread.sent, "сообщения не отправлены"
        assert thread.sent[-1] == "**Обзор автора**\n\nОбзор автора: тест"
        assert sum("**Обзор автора**" in c for c in thread.sent) == 1
        # MISS-путь фазы 4: ref по url джобы, факты канала отдельно
        assert provider.ref_calls == [URL]
        assert len(provider.channel_calls) == 1
        assert provider.channel_calls[0] is ref
        # LLM-обзор автора вызван ровно один раз с фактами от провайдера
        assert llm.review_calls == [facts]
        # строка автора записана в store
        row = deps.authors.get("youtube", CHANNEL_HANDLE)
        assert row is not None, "обзор автора закеширован в authors"
        assert row["review_json"] == "Обзор автора: тест"
        assert row["display_name"] == "Имя Канала"
        assert row["prompts_ver"] == str(deps.prompts["version"])
        # в кеш отчётов попал отчёт УЖЕ с автор-блоком (блок добавлен до кеширования)
        payload = deps.reports.get(*_cache_key(deps, extractor))
        assert payload is not None
        cached = Report.from_json(payload)
        assert [b.kind for b in cached.blocks] == ["brief", "theses", "author"]
        author = cached.blocks[-1]
        assert author.title == "Обзор автора"
        assert author.body_md == "Обзор автора: тест"
        assert author.data == {
            "platform": "youtube",
            "handle": CHANNEL_HANDLE,
            "name": "Имя Канала",
            "url": f"https://www.youtube.com/{CHANNEL_HANDLE}",
        }

    def test_fresh_author_row_reused_without_llm_review(
        self, make_deps, make_thread, make_llm, make_author_facts, make_extractor, make_router, store
    ):
        thread = make_thread(4242)
        ref, facts = _ref_facts()
        provider = make_author_facts(get_author_ref_result=ref, get_channel_result=facts)
        llm = make_llm()
        extractor = make_extractor(transcripts={URL: TRANSCRIPT})
        deps = make_deps(
            thread=thread,
            llm=llm,
            reports=store.reports,
            authors=store.authors,
            author_facts=provider,
            router=make_router([extractor]),
        )
        store.authors.put(
            "youtube",
            CHANNEL_HANDLE,
            "Имя Канала",
            f'{{"name": "Имя Канала", "url": "{AUTHOR_URL}"}}',
            "СВЕЖИЙ ОБЗОР ИЗ КЕША",
            "2",
        )

        asyncio.run(process_job(deps, _job(extractor, thread.id)))

        assert llm.review_calls == [], "свежая строка автора — LLM не зовётся"
        assert provider.channel_calls == [], (
            "свежая строка спасает: страница канала (get_channel) не качается"
        )
        assert thread.sent[-1] == "**Обзор автора**\n\nСВЕЖИЙ ОБЗОР ИЗ КЕША"
        assert deps.background_queue.qsize() == 0, "MISS-путь не ставит фоновых задач"
        # в кеш отчётов ушёл блок с фактами ИЗ строки кеша (display_name, facts_json)
        payload = deps.reports.get(*_cache_key(deps, extractor))
        assert payload is not None
        author = Report.from_json(payload).blocks[-1]
        assert author.data == {
            "platform": "youtube",
            "handle": CHANNEL_HANDLE,
            "name": "Имя Канала",
            "url": AUTHOR_URL,
        }

    def test_stale_author_row_rebuilt_with_llm(
        self, make_deps, make_thread, make_llm, make_author_facts, make_extractor, make_router, store, db_path
    ):
        thread = make_thread(4242)
        ref, facts = _ref_facts()
        provider = make_author_facts(get_author_ref_result=ref, get_channel_result=facts)
        llm = make_llm(review="НОВЫЙ ОБЗОР")
        extractor = make_extractor(transcripts={URL: TRANSCRIPT})
        deps = make_deps(
            thread=thread,
            llm=llm,
            reports=store.reports,
            authors=store.authors,
            author_facts=provider,
            router=make_router([extractor]),
        )
        store.authors.put("youtube", CHANNEL_HANDLE, "Имя Канала", "{}", "СТАРЫЙ ОБЗОР", "2")
        _age_author(db_path, "youtube", CHANNEL_HANDLE, STALE_ANALYZED_AT)

        asyncio.run(process_job(deps, _job(extractor, thread.id)))

        assert llm.review_calls == [facts], "устаревшая строка на промахе — обзор строится сейчас"
        assert thread.sent[-1] == "**Обзор автора**\n\nНОВЫЙ ОБЗОР"
        row = deps.authors.get("youtube", CHANNEL_HANDLE)
        assert row["review_json"] == "НОВЫЙ ОБЗОР"
        assert row["analyzed_at"] > STALE_ANALYZED_AT, "строка обновлена свежим штампом"

    def test_author_ref_none_report_without_author_block(
        self, make_deps, make_thread, make_llm, make_author_facts, make_extractor, make_router, store
    ):
        thread = make_thread(4242)
        provider = make_author_facts(get_author_ref_result=None)
        llm = make_llm()
        extractor = make_extractor(transcripts={URL: TRANSCRIPT})
        deps = make_deps(
            thread=thread,
            llm=llm,
            reports=store.reports,
            authors=store.authors,
            author_facts=provider,
            router=make_router([extractor]),
        )

        asyncio.run(process_job(deps, _job(extractor, thread.id)))

        assert all("**Обзор автора**" not in c for c in thread.sent), "ref'а нет — блока нет"
        assert provider.ref_calls == [URL], "ref добывается по url джобы"
        assert provider.channel_calls == [], "без ref факты канала не запрашиваются"
        assert llm.review_calls == []
        assert deps.authors.get("youtube", CHANNEL_HANDLE) is None
        payload = deps.reports.get(*_cache_key(deps, extractor))
        assert payload is not None
        assert [b.kind for b in Report.from_json(payload).blocks] == ["brief", "theses"]

    def test_channel_facts_none_on_miss_leaves_report_without_author(
        self, make_deps, make_thread, make_llm, make_author_facts, make_extractor, make_router, store
    ):
        # ref есть, фактов канала нет (сеть упала на get_channel): отчёт без автора
        thread = make_thread(4242)
        ref, _ = _ref_facts()
        provider = make_author_facts(get_author_ref_result=ref, get_channel_result=None)
        llm = make_llm()
        extractor = make_extractor(transcripts={URL: TRANSCRIPT})
        deps = make_deps(
            thread=thread,
            llm=llm,
            reports=store.reports,
            authors=store.authors,
            author_facts=provider,
            router=make_router([extractor]),
        )

        asyncio.run(process_job(deps, _job(extractor, thread.id)))

        assert any("**Кратко**" in c for c in thread.sent), "сам отчёт доставлен"
        assert all("**Обзор автора**" not in c for c in thread.sent)
        assert llm.review_calls == []
        assert deps.authors.get("youtube", CHANNEL_HANDLE) is None
        payload = deps.reports.get(*_cache_key(deps, extractor))
        assert [b.kind for b in Report.from_json(payload).blocks] == ["brief", "theses"]

    def test_review_failure_delivers_report_without_author_block(
        self, make_deps, make_thread, make_llm, make_author_facts, make_extractor, make_router, store
    ):
        # сбой обзора автора не роняет джобу: отчёт доставляется и кешируется без автора
        thread = make_thread(4242)
        ref, facts = _ref_facts()
        provider = make_author_facts(get_author_ref_result=ref, get_channel_result=facts)
        llm = make_llm(review_error=RuntimeError("обзор не вышел"))
        extractor = make_extractor(transcripts={URL: TRANSCRIPT})
        deps = make_deps(
            thread=thread,
            llm=llm,
            reports=store.reports,
            authors=store.authors,
            author_facts=provider,
            router=make_router([extractor]),
        )

        asyncio.run(process_job(deps, _job(extractor, thread.id)))

        assert any("**Кратко**" in c for c in thread.sent), "сам отчёт доставлен"
        assert all("**Обзор автора**" not in c for c in thread.sent)
        assert deps.authors.get("youtube", CHANNEL_HANDLE) is None
        payload = deps.reports.get(*_cache_key(deps, extractor))
        assert payload is not None
        assert [b.kind for b in Report.from_json(payload).blocks] == ["brief", "theses"]

    def test_missing_provider_skips_author_path(
        self, make_deps, make_thread, make_llm, make_extractor, make_router, store
    ):
        # deps.author_facts может быть None (None-безопасность пайплайна)
        thread = make_thread(4242)
        llm = make_llm()
        extractor = make_extractor(transcripts={URL: TRANSCRIPT})
        deps = make_deps(
            thread=thread,
            llm=llm,
            reports=store.reports,
            authors=store.authors,
            router=make_router([extractor]),
        )
        deps.author_facts = None

        asyncio.run(process_job(deps, _job(extractor, thread.id)))

        assert any("**Кратко**" in c for c in thread.sent)
        assert all("**Обзор автора**" not in c for c in thread.sent)
        assert llm.review_calls == []
        assert deps.authors.get("youtube", CHANNEL_HANDLE) is None


class TestHitPath:
    """Повторная ссылка: отчёт из кеша; автор-блок — из payload, LLM/сеть не трогаются."""

    def test_hit_with_author_block_no_provider_no_llm(
        self, make_deps, make_thread, make_llm, make_author_facts, make_extractor, make_router, store
    ):
        thread = make_thread(4242)
        ref, _ = _ref_facts()
        provider = make_author_facts(get_author_ref_result=ref)  # ошибочный поход в сеть виден по счётчику
        llm = make_llm()
        extractor = make_extractor(transcripts={URL: TRANSCRIPT})
        deps = make_deps(
            thread=thread,
            llm=llm,
            reports=store.reports,
            authors=store.authors,
            author_facts=provider,
            router=make_router([extractor]),
        )
        _seed_report(deps, extractor, _payload_with_author("СТАРЫЙ ОБЗОР"))
        store.authors.put("youtube", CHANNEL_HANDLE, "Имя Канала", "{}", "СТАРЫЙ ОБЗОР", "2")

        asyncio.run(process_job(deps, _job(extractor, thread.id)))

        assert provider.ref_calls == [], "на кеш-хите провайдер фактов не зовётся"
        assert llm.calls == [] and llm.review_calls == [], "на кеш-хите LLM не зовётся"
        assert thread.sent[-1] == "**Обзор автора**\n\nСТАРЫЙ ОБЗОР"
        assert deps.background_queue.qsize() == 0, "свежая строка автора — фон не нужен"

    def test_hit_with_stale_row_user_gets_old_body_and_background_refresh(
        self, make_deps, make_thread, make_llm, make_author_facts, make_extractor, make_router, store, db_path
    ):
        thread = make_thread(4242)
        ref, _ = _ref_facts()
        provider = make_author_facts(get_author_ref_result=ref)
        llm = make_llm()
        extractor = make_extractor(transcripts={URL: TRANSCRIPT})
        deps = make_deps(
            thread=thread,
            llm=llm,
            reports=store.reports,
            authors=store.authors,
            author_facts=provider,
            router=make_router([extractor]),
        )
        _seed_report(deps, extractor, _payload_with_author("СТАРЫЙ ОБЗОР"))
        store.authors.put("youtube", CHANNEL_HANDLE, "Имя Канала", "{}", "СТАРЫЙ ОБЗОР", "2")
        _age_author(db_path, "youtube", CHANNEL_HANDLE, STALE_ANALYZED_AT)

        asyncio.run(process_job(deps, _job(extractor, thread.id)))

        # юзер сразу получает старый обзор, обновление — в фоне (никогда не ждёт)
        assert thread.sent[-1] == "**Обзор автора**\n\nСТАРЫЙ ОБЗОР"
        assert llm.review_calls == [], "обзор обновляется фоном, не синхронно"
        assert deps.background_queue.qsize() == 1
        task = deps.background_queue.get_nowait()
        assert task == {"platform": "youtube", "handle": CHANNEL_HANDLE}

    def test_hit_without_author_row_enqueues_background_refresh(
        self, make_deps, make_thread, make_llm, make_author_facts, make_extractor, make_router, store
    ):
        thread = make_thread(4242)
        ref, _ = _ref_facts()
        provider = make_author_facts(get_author_ref_result=ref)
        llm = make_llm()
        extractor = make_extractor(transcripts={URL: TRANSCRIPT})
        deps = make_deps(
            thread=thread,
            llm=llm,
            reports=store.reports,
            authors=store.authors,
            author_facts=provider,
            router=make_router([extractor]),
        )
        _seed_report(deps, extractor, _payload_with_author("СТАРЫЙ ОБЗОР"))

        asyncio.run(process_job(deps, _job(extractor, thread.id)))

        assert thread.sent[-1] == "**Обзор автора**\n\nСТАРЫЙ ОБЗОР"
        assert deps.background_queue.qsize() == 1, "строки автора нет — фоновая задача поставлена"

    def test_hit_without_author_block_is_silent(
        self, make_deps, make_thread, make_llm, make_author_facts, make_extractor, make_router, store
    ):
        thread = make_thread(4242)
        ref, _ = _ref_facts()
        provider = make_author_facts(get_author_ref_result=ref)  # сеть бы понадобилась при ошибке пайплайна
        llm = make_llm()
        extractor = make_extractor(transcripts={URL: TRANSCRIPT})
        deps = make_deps(
            thread=thread,
            llm=llm,
            reports=store.reports,
            authors=store.authors,
            author_facts=provider,
            router=make_router([extractor]),
        )
        _seed_report(deps, extractor, _payload_without_author())

        asyncio.run(process_job(deps, _job(extractor, thread.id)))

        assert provider.ref_calls == [], "нет author-блока — провайдер не зовётся (без сети)"
        assert llm.calls == [] and llm.review_calls == []
        assert all("**Обзор автора**" not in c for c in thread.sent)
        assert deps.background_queue.qsize() == 0

    def test_hit_author_block_without_platform_handle_is_silent(
        self, make_deps, make_thread, make_llm, make_author_facts, make_extractor, make_router, store
    ):
        # author-блок без platform/handle в data (битый payload из старого кеша):
        # ключ фоновой задачи собрать нельзя — тишина, отчёт доставляется как есть
        thread = make_thread(4242)
        ref, _ = _ref_facts()
        provider = make_author_facts(get_author_ref_result=ref)
        llm = make_llm()
        extractor = make_extractor(transcripts={URL: TRANSCRIPT})
        deps = make_deps(
            thread=thread,
            llm=llm,
            reports=store.reports,
            authors=store.authors,
            author_facts=provider,
            router=make_router([extractor]),
        )
        report = Report(
            [
                ReportBlock("brief", body_md="Суть видео."),
                ReportBlock("author", title="Обзор автора", body_md="СТАРЫЙ ОБЗОР", data={}),
            ]
        )
        _seed_report(deps, extractor, report.to_json())

        asyncio.run(process_job(deps, _job(extractor, thread.id)))

        assert provider.ref_calls == [] and llm.calls == [] and llm.review_calls == []
        assert thread.sent[-1] == "**Обзор автора**\n\nСТАРЫЙ ОБЗОР"
        assert deps.background_queue.qsize() == 0

    def test_ttl_taken_from_config(
        self, make_deps, make_thread, make_llm, make_author_facts, make_extractor, make_router, store, db_path
    ):
        # TTL приходит из config.author_ttl_days: строка старше 1 дня устарела,
        # хотя при дефолтных 90 днях она была бы свежей
        thread = make_thread(4242)
        ref, _ = _ref_facts()
        provider = make_author_facts(get_author_ref_result=ref)
        llm = make_llm()
        config = BotConfig(bot_token="t", llm_api_key="k", author_ttl_days=1)
        extractor = make_extractor(transcripts={URL: TRANSCRIPT})
        deps = make_deps(
            thread=thread,
            llm=llm,
            reports=store.reports,
            authors=store.authors,
            author_facts=provider,
            config=config,
            router=make_router([extractor]),
        )
        _seed_report(deps, extractor, _payload_with_author("СТАРЫЙ ОБЗОР"))
        store.authors.put("youtube", CHANNEL_HANDLE, "Имя Канала", "{}", "СТАРЫЙ ОБЗОР", "2")
        _age_author(db_path, "youtube", CHANNEL_HANDLE, int(time.time()) - 2 * 86400)

        asyncio.run(process_job(deps, _job(extractor, thread.id)))

        assert llm.review_calls == []
        assert deps.background_queue.qsize() == 1, "TTL из config.author_ttl_days применён"


class TestAuthorBudgetValidation:
    """Бюджет author-блока проверяется при добавлении блока (после общей валидации отчёта)."""

    def test_long_review_logged_with_author_budget(
        self, make_deps, make_thread, make_llm, make_author_facts, make_extractor, make_router, store, caplog
    ):
        # обзор длиннее бюджета author (900): превышение не фатально — рендер
        # укоротит блок, но в логе остаётся след для настройки промптов
        thread = make_thread(4242)
        ref, facts = _ref_facts()
        provider = make_author_facts(get_author_ref_result=ref, get_channel_result=facts)
        llm = make_llm(review="ДЛИННЫЙ ОБЗОР " * 100)  # raw ~1400 > 900
        extractor = make_extractor(transcripts={URL: TRANSCRIPT})
        deps = make_deps(
            thread=thread,
            llm=llm,
            reports=store.reports,
            authors=store.authors,
            author_facts=provider,
            router=make_router([extractor]),
        )

        with caplog.at_level(logging.WARNING, logger="yt-summary"):
            asyncio.run(process_job(deps, _job(extractor, thread.id)))

        assert "author" in caplog.text, "превышение бюджета author-блока отмечено в логе"
        assert any("**Кратко**" in c for c in thread.sent), "доставка не зависит от превышения"


def _run_worker_once(deps, tasks):
    """Кормит author_refresh_worker задачами и гасит его после опустошения очереди."""
    from bot.jobs import author_refresh_worker

    async def _scenario():
        # каждый asyncio.run() — новая петля, а asyncio.Queue привязывается к петле
        # при первом использовании; поэтому на каждый прогон — свежая очередь
        deps.background_queue = asyncio.Queue()
        for task in tasks:
            await deps.background_queue.put(task)
        worker = asyncio.create_task(author_refresh_worker(deps.background_queue, deps))
        await asyncio.wait_for(deps.background_queue.join(), timeout=5)
        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker

    asyncio.run(_scenario())


class TestAuthorRefreshWorker:
    def test_refresh_task_updates_author_row_without_discord(
        self, make_deps, make_thread, make_llm, make_author_facts, make_extractor, make_router, store, db_path
    ):
        thread = make_thread(4242)
        channel_facts = SimpleNamespace(name="Имя Канала", url=AUTHOR_URL, description="о канале", extra={})
        provider = make_author_facts(get_channel_result=channel_facts)
        llm = make_llm(review="СВЕЖИЙ ОБЗОР")
        extractor = make_extractor(transcripts={URL: TRANSCRIPT})
        deps = make_deps(
            thread=thread,
            llm=llm,
            reports=store.reports,
            authors=store.authors,
            author_facts=provider,
            router=make_router([extractor]),
        )
        store.authors.put("youtube", CHANNEL_HANDLE, "Имя Канала", "{}", "СТАРЫЙ ОБЗОР", "2")
        _age_author(db_path, "youtube", CHANNEL_HANDLE, STALE_ANALYZED_AT)

        _run_worker_once(deps, [{"platform": "youtube", "handle": CHANNEL_HANDLE}])

        row = deps.authors.get("youtube", CHANNEL_HANDLE)
        assert row["review_json"] == "СВЕЖИЙ ОБЗОР"
        assert row["analyzed_at"] > STALE_ANALYZED_AT, "put воркера обновил analyzed_at"
        ref = provider.channel_calls[0]
        assert (ref.platform, ref.handle) == ("youtube", CHANNEL_HANDLE)
        assert provider.ref_calls == [], "воркер зовёт только get_channel, не get_author_ref"
        assert llm.review_calls == [channel_facts]
        # Discord не тронут: ни сообщений в тред, ни буфера
        assert thread.sent == []
        assert deps.outbox.load() == []

    def test_get_channel_none_leaves_row_and_worker_alive(
        self, make_deps, make_thread, make_llm, make_author_facts, make_extractor, make_router, store, db_path
    ):
        thread = make_thread(4242)
        provider = make_author_facts(get_channel_result=None)
        llm = make_llm(review="СВЕЖИЙ ОБЗОР")
        extractor = make_extractor(transcripts={URL: TRANSCRIPT})
        deps = make_deps(
            thread=thread,
            llm=llm,
            reports=store.reports,
            authors=store.authors,
            author_facts=provider,
            router=make_router([extractor]),
        )
        store.authors.put("youtube", CHANNEL_HANDLE, "Имя Канала", "{}", "СТАРЫЙ ОБЗОР", "2")
        # строка должна быть устаревшей: свежую воркер теперь рассасывает без LLM
        # (дедуп фоновых обновлений), а этот тест — про выживание при сбоях
        _age_author(db_path, "youtube", CHANNEL_HANDLE, STALE_ANALYZED_AT)
        before = dict(deps.authors.get("youtube", CHANNEL_HANDLE))

        _run_worker_once(deps, [{"platform": "youtube", "handle": CHANNEL_HANDLE}])

        row = deps.authors.get("youtube", CHANNEL_HANDLE)
        assert row["review_json"] == "СТАРЫЙ ОБЗОР", "без фактов строка не меняется"
        assert row["analyzed_at"] == before["analyzed_at"]

        # воркер жив: следующая задача обрабатывается штатно
        provider.get_channel_result = SimpleNamespace(
            name="Имя Канала", url=AUTHOR_URL, description="о канале", extra={}
        )
        _run_worker_once(deps, [{"platform": "youtube", "handle": CHANNEL_HANDLE}])
        assert deps.authors.get("youtube", CHANNEL_HANDLE)["review_json"] == "СВЕЖИЙ ОБЗОР"

    def test_review_error_leaves_row_and_worker_alive(
        self, make_deps, make_thread, make_llm, make_author_facts, make_extractor, make_router, store, db_path
    ):
        thread = make_thread(4242)
        provider = make_author_facts(
            get_channel_result=SimpleNamespace(name="Имя Канала", url=AUTHOR_URL, description="д", extra={})
        )
        llm = make_llm(review_error=RuntimeError("обзор не вышел"))
        extractor = make_extractor(transcripts={URL: TRANSCRIPT})
        deps = make_deps(
            thread=thread,
            llm=llm,
            reports=store.reports,
            authors=store.authors,
            author_facts=provider,
            router=make_router([extractor]),
        )
        store.authors.put("youtube", CHANNEL_HANDLE, "Имя Канала", "{}", "СТАРЫЙ ОБЗОР", "2")
        # строка должна быть устаревшей: свежую воркер теперь рассасывает без LLM
        # (дедуп фоновых обновлений), а этот тест — про выживание при сбоях
        _age_author(db_path, "youtube", CHANNEL_HANDLE, STALE_ANALYZED_AT)
        before = dict(deps.authors.get("youtube", CHANNEL_HANDLE))

        # исключение обзора воркер глотает (лог) — join завершается, строка не тронута
        _run_worker_once(deps, [{"platform": "youtube", "handle": CHANNEL_HANDLE}])
        row = deps.authors.get("youtube", CHANNEL_HANDLE)
        assert row["review_json"] == "СТАРЫЙ ОБЗОР"
        assert row["analyzed_at"] == before["analyzed_at"]

        # воркер жив: следующая задача после сбоя обрабатывается
        llm.review_error = None
        _run_worker_once(deps, [{"platform": "youtube", "handle": CHANNEL_HANDLE}])
        assert deps.authors.get("youtube", CHANNEL_HANDLE)["review_json"] == "Обзор автора: тест"

    def test_second_task_on_fresh_row_skips_llm(
        self, make_deps, make_thread, make_llm, make_author_facts, make_extractor, make_router, store, db_path
    ):
        # дедуп фоновых refresh: первая задача обновляет строку, вторая (пока она
        # ждала в очереди, строка стала свежей) рассасывается без LLM-вызова
        thread = make_thread(4242)
        channel_facts = SimpleNamespace(name="Имя Канала", url=AUTHOR_URL, description="о канале", extra={})
        provider = make_author_facts(get_channel_result=channel_facts)
        llm = make_llm(review="СВЕЖИЙ ОБЗОР")
        extractor = make_extractor(transcripts={URL: TRANSCRIPT})
        deps = make_deps(
            thread=thread,
            llm=llm,
            reports=store.reports,
            authors=store.authors,
            author_facts=provider,
            router=make_router([extractor]),
        )
        store.authors.put("youtube", CHANNEL_HANDLE, "Имя Канала", "{}", "СТАРЫЙ ОБЗОР", "2")
        _age_author(db_path, "youtube", CHANNEL_HANDLE, STALE_ANALYZED_AT)

        _run_worker_once(
            deps,
            [
                {"platform": "youtube", "handle": CHANNEL_HANDLE},
                {"platform": "youtube", "handle": CHANNEL_HANDLE},
            ],
        )

        assert llm.review_calls == [channel_facts], "вторая задача не дублирует LLM-обзор"
        row = deps.authors.get("youtube", CHANNEL_HANDLE)
        assert row["review_json"] == "СВЕЖИЙ ОБЗОР"
        assert provider.channel_calls, "первая задача дошла до провайдера фактов"
        assert len(provider.channel_calls) == 1


class _IntervalLLM:
    """LLM-фейк с медленными analyze_content и review_author: пишет интервалы
    [начало, конец] вызовов каждого метода (в отдельные списки).

    Детерминизм проверки: без блокировки два конкурентных вызова стартуют почти
    одновременно и перекрываются (0.15с реального сна в потоках); с llm_lock —
    строго последовательно. Ложных срабатываний нет: при корректной блокировке
    пересечение невозможно, при её отсутствии — почти наверняка есть.
    """

    def __init__(self):
        self.intervals = []
        self.review_intervals = []
        self._rec = threading.Lock()

    def analyze_content(self, transcript):
        start = time.monotonic()
        time.sleep(0.15)
        end = time.monotonic()
        with self._rec:
            self.intervals.append((start, end))
        from bot.analysis.report import Report

        return Report.from_analysis({"brief": "Суть.", "theses": ["тезис"]})

    def review_author(self, facts):
        start = time.monotonic()
        time.sleep(0.15)
        end = time.monotonic()
        with self._rec:
            self.review_intervals.append((start, end))
        return "Обзор автора: тест"


class TestLlmLock:
    def test_concurrent_jobs_serialize_analyze_content(
        self, make_deps, make_thread, make_client, make_extractor, make_router
    ):
        llm = _IntervalLLM()
        t1, t2 = make_thread(1), make_thread(2)
        extractor = make_extractor()
        deps = make_deps(
            client=make_client({1: t1, 2: t2}),
            llm=llm,
            llm_lock=asyncio.Lock(),
            router=make_router([extractor]),
        )

        async def _scenario():
            await asyncio.gather(
                process_job(deps, _job(extractor, 1)),
                process_job(deps, _job(extractor, 2)),
            )

        asyncio.run(_scenario())

        assert len(llm.intervals) == 2, "оба разбора выполнены"
        intervals = sorted(llm.intervals)
        assert intervals[1][0] >= intervals[0][1], (
            "вызовы analyze_content не пересекаются: llm_lock сериализует LLM"
        )

    def test_concurrent_analyze_and_review_do_not_overlap(
        self, make_deps, make_thread, make_client, make_author_facts, make_extractor, make_router, store
    ):
        # llm_lock — барьер и между воркерами: analyze_content джобы и review_author
        # фонового воркера выполняются в разных задачах, но не пересекаются по времени
        from bot.jobs import author_refresh_worker

        llm = _IntervalLLM()
        t1, t2 = make_thread(1), make_thread(2)
        ref, _ = _ref_facts()
        provider = make_author_facts(
            get_author_ref_result=ref,
            get_channel_result=SimpleNamespace(
                name="Имя Канала", url=AUTHOR_URL, description="о канале", extra={}
            ),
        )
        extractor = make_extractor(transcripts={URL: TRANSCRIPT})
        deps = make_deps(
            client=make_client({1: t1, 2: t2}),
            llm=llm,
            reports=store.reports,
            authors=store.authors,
            author_facts=provider,
            llm_lock=asyncio.Lock(),
            router=make_router([extractor]),
        )

        async def _scenario():
            worker = asyncio.create_task(author_refresh_worker(deps.background_queue, deps))
            await deps.background_queue.put({"platform": "youtube", "handle": CHANNEL_HANDLE})
            await asyncio.gather(
                process_job(deps, _job(extractor, 1)),
                deps.background_queue.join(),
            )
            worker.cancel()
            with pytest.raises(asyncio.CancelledError):
                await worker

        asyncio.run(_scenario())

        assert len(llm.intervals) == 1 and len(llm.review_intervals) == 1, "оба вызова выполнены"
        a_start, a_end = llm.intervals[0]
        r_start, r_end = llm.review_intervals[0]
        assert (a_end <= r_start) or (r_end <= a_start), (
            "analyze_content и review_author не пересекаются: llm_lock сериализует LLM"
        )


class TestBuildDepsWiring:
    def test_build_deps_wires_author_components(self, tmp_path):
        # композиция фазы 3: authors-репозиторий Store'а, провайдер фактов,
        # фоновая очередь и общий llm_lock в Deps (контракт bot/main.build_deps)
        from bot.analysis.author import YouTubeAuthorFactsProvider
        from bot.jobs import create_background_queue
        from bot.main import build_deps
        from bot.store.authors import AuthorStore

        assert isinstance(create_background_queue(), asyncio.Queue)

        config = BotConfig(bot_token="t", llm_api_key="k", db_path=str(tmp_path / "db.sqlite3"))
        deps = build_deps(config)
        try:
            assert isinstance(deps.author_facts, YouTubeAuthorFactsProvider)
            assert isinstance(deps.authors, AuthorStore)
            assert isinstance(deps.background_queue, asyncio.Queue)
            assert isinstance(deps.llm_lock, asyncio.Lock)
        finally:
            deps.reports.db.close()
