"""E2E фазы 5: ссылка на статью идёт через тот же пайплайн, что и видео.

Контракт (docs/ARCHITECTURE.md §5-§6, §12 фаза 5):
- реестр [YoutubeExtractor, ArticleExtractor] — article последним (fallback):
  не-YouTube ссылку забирает ArticleExtractor (kind="article"), YouTube-экстрактор
  с транскриптами не зовётся вовсе;
- полный путь: тред получает «Разбор» + блоки (кратко, тезисы, подробно) БЕЗ
  таймкод-ссылок, LLM получает ТЕКСТ статьи (doc.content без [mm:ss]-разметки),
  кеш отчётов пишется под canonical "article:...";
- повторная ссылка с utm-хвостом — другое сообщение (дедуп пропускает), но тот же
  canonical: extract качает страницу снова (content_hash), а LLM зовётся один раз;
- NotAnArticle (мало текста) с пустым кешем: статус отредактирован вежливым
  отказом, в треде НЕТ error-сообщения, джоба завершается штатно (mark_done),
  тред пуст; если разбор этой ссылки уже лежит в кеше (текст исчез/стал
  не-статьёй позже) — доставляется отчёт из кеша, а не отказ;
- сетевой сбой сайта (500): error-сообщение «страница недоступна», mark_failed;
- автор-блока у статей нет даже при meta author: автор живёт в doc.data["author"]
  и не попадает в обзор автора (author_ref None).

Экстрактор — НАСТОЯЩИЙ ArticleExtractor с инжектом fetch на синтетические
фикстуры (tests/fixtures/), сети нет: LLM/Discord — фейки из conftest, jobs —
in-memory с трассировкой исходов. Импорт bot.sources.article — ленивый (TDD):
до реализации фазы падают конкретные тесты, а не коллекция файла.
"""

import asyncio
from pathlib import Path

import pytest

from bot.analysis.report import Report
from bot.discord_app import handle
from bot.pipeline import REPORT_SCHEMA

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures"

ARTICLE_URL = "https://example.com/news/extractor-note"
# та же статья с tracking-хвостом: другой вид ссылки, тот же canonical
UTM_URL = ARTICLE_URL + "/?utm_source=tw&utm_medium=im#top"

# Ответ LLM с разделом без start_sec: у статей таймкодов не бывает — рендер
# не должен добавлять [mm:ss]-ссылок к заголовкам разделов
ARTICLE_ANALYSIS = {
    "brief": "Суть статьи в двух предложениях.",
    "complexity": "simple",
    "context": "",
    "theses": ["первый тезис статьи", "второй тезис статьи"],
    "sections": [
        {"title": "Заголовок раздела", "start_sec": None, "body": "разбор раздела статьи"}
    ],
}

POLITE_REJECTION = (
    "🤔 По ссылке не нашлось статьи с текстом. Если это видео — дай ссылку на YouTube."
)


def _fixture_html(name: str) -> str:
    return (FIXTURES_DIR / name).read_text(encoding="utf-8")


def _norm(text: str) -> str:
    return " ".join(text.split())


def _article_extractor(fetch):
    from bot.sources.article import ArticleExtractor

    return ArticleExtractor(fetch=fetch)


def _youtube_no_network():
    """YoutubeExtractor с fetch_transcript-заглушкой: для статьи он не зовётся."""
    from bot.sources.youtube import YoutubeExtractor

    def _must_not_fetch(video_id):
        raise AssertionError("YouTube-экстрактор не должен зваться для не-YouTube ссылки")

    return YoutubeExtractor(fetch_transcript=_must_not_fetch)


def _run_worker_once(deps):
    """Обрабатывает очередь настоящим llm_worker и гасит его после опустошения."""
    from bot.jobs import llm_worker

    async def _scenario():
        worker = asyncio.create_task(llm_worker(deps.queue, deps))
        await asyncio.wait_for(deps.queue.join(), timeout=5)
        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker

    asyncio.run(_scenario())


class TestArticleFlow:
    def test_article_message_full_flow(
        self, make_deps, make_message, make_thread, make_llm, make_article_fetch, make_job_store,
        make_router,
    ):
        thread = make_thread(4242)
        fetch = make_article_fetch(_fixture_html("article_full.html"))
        article = _article_extractor(fetch)
        deps = make_deps(
            thread=thread,
            llm=make_llm(analysis_json=dict(ARTICLE_ANALYSIS)),
            router=make_router([_youtube_no_network(), article]),
            jobs=make_job_store(),
        )
        msg = make_message(f"почитай {ARTICLE_URL}", thread=thread)

        asyncio.run(handle(msg, deps))

        rows = deps.jobs.take_new()
        assert len(rows) == 1
        assert rows[0]["kind"] == "article"
        assert rows[0]["url"] == ARTICLE_URL
        assert rows[0]["canonical_id"] == article.canonical_id(ARTICLE_URL)
        assert rows[0]["canonical_id"].startswith("article:")

        _run_worker_once(deps)

        # тред получил «Разбор» + блоки прогрессивной детализации; у статей нет
        # describe-метаданных — заголовок из маскированной ссылки (без превью)
        assert thread.sent, "сообщения не отправлены"
        assert thread.sent[0].startswith(f"**Разбор: <{ARTICLE_URL}>**\n\n**Кратко**")
        sent_text = "\n".join(thread.sent)
        assert "**Тезисы**" in sent_text
        assert "**Заголовок раздела**" in sent_text, "подробно по разделам на месте"
        # у статей нет таймкодов: никаких ссылок с ?t= и никаких youtu.be
        assert "?t=" not in sent_text and "youtu.be" not in sent_text, (
            "без таймкод-ссылок для статей"
        )
        # LLM получил ТЕКСТ статьи (не субтитры): абзацы фикстуры, без [mm:ss]
        assert len(deps.llm.calls) == 1
        llm_input = deps.llm.calls[0]
        assert "Первый абзац объясняет" in _norm(llm_input), "текст фикстуры ушёл в анализ"
        assert "[" not in llm_input, "таймкод-разметки в тексте статьи нет"
        # кеш отчётов записан под canonical статьи
        cached = deps.reports.get_latest(
            article.canonical_id(ARTICLE_URL), str(deps.prompts["version"]), REPORT_SCHEMA
        )
        assert cached is not None, "отчёт закеширован с canonical article:..."
        # джоба завершена штатно
        assert deps.jobs.count_new() == 0
        assert deps.jobs.failed_calls == []
        assert msg.status.deleted

    def test_repeat_link_with_utm_hits_cache(
        self, make_deps, make_message, make_thread, make_llm, make_article_fetch, make_job_store,
        make_router,
    ):
        thread = make_thread(4242)
        fetch = make_article_fetch(_fixture_html("article_full.html"))
        article = _article_extractor(fetch)
        deps = make_deps(
            thread=thread,
            llm=make_llm(analysis_json=dict(ARTICLE_ANALYSIS)),
            router=make_router([_youtube_no_network(), article]),
            jobs=make_job_store(),
        )
        msg1 = make_message(f"почитай {ARTICLE_URL}", id=4001, thread=thread)
        msg2 = make_message(f"почитай {UTM_URL}", id=4002, thread=thread)

        # оба цикла «handle -> воркер» в одной петле: asyncio.Queue привязывается
        # к петле при ожидании (см. комментарий в tests/e2e/test_author_flow.py)
        from bot.jobs import llm_worker

        async def _scenario():
            worker = asyncio.create_task(llm_worker(deps.queue, deps))
            await handle(msg1, deps)
            await asyncio.wait_for(deps.queue.join(), timeout=5)
            first_sent = list(thread.sent)
            # utm-хвост: дедуп по message_id пропустит (другое сообщение), кеш — по canonical
            await handle(msg2, deps)
            await asyncio.wait_for(deps.queue.join(), timeout=5)
            worker.cancel()
            with pytest.raises(asyncio.CancelledError):
                await worker
            return first_sent

        first_sent = asyncio.run(_scenario())

        assert len(fetch.calls) == 2, "контент добывается всегда (нужен content_hash)"
        assert [url for url, _ in fetch.calls] == [ARTICLE_URL, UTM_URL]
        assert len(deps.llm.calls) == 1, "тот же canonical — второй разбор из кеша, LLM один"
        assert len(thread.sent) == 2 * len(first_sent), "отчёт доставлен повторно"
        assert sum("Суть статьи в двух предложениях." in c for c in thread.sent) == 2, (
            "оба разбора — один и тот же отчёт из кеша"
        )
        assert sum(c.startswith("**Разбор:") for c in thread.sent) == 2
        assert deps.jobs.count_new() == 0
        assert deps.jobs.failed_calls == []

    def test_junk_link_gets_polite_rejection(
        self, make_deps, make_message, make_thread, make_llm, make_article_fetch, make_job_store,
        make_router,
    ):
        # «не-статья» (мало текста) — вежливый отказ вместо error-сообщения:
        # джоба завершается нормально, тред остаётся пустым
        thread = make_thread(4242)
        fetch = make_article_fetch(_fixture_html("junk_page.html"))
        article = _article_extractor(fetch)
        deps = make_deps(
            thread=thread,
            llm=make_llm(),
            router=make_router([_youtube_no_network(), article]),
            jobs=make_job_store(),
        )
        msg = make_message(f"глянь {ARTICLE_URL}", thread=thread)

        asyncio.run(handle(msg, deps))
        _run_worker_once(deps)

        assert POLITE_REJECTION in msg.status.edits, "статус отредактирован вежливым отказом"
        assert thread.sent == [], "тред остался пустым (error-сообщение не отправлялось)"
        assert deps.llm.calls == [], "LLM для не-статьи не зовётся"
        assert deps.jobs.count_new() == 0, "джоба завершена штатно (mark_done)"
        assert deps.jobs.failed_calls == [], "вежливый отказ — не ошибка джобы"
        assert deps.outbox.load() == []

    def test_not_an_article_with_cached_report_delivers_cache(
        self, make_deps, make_message, make_thread, make_llm, make_article_fetch, make_job_store,
        make_router,
    ):
        # страница стала не-статьёй, но разбор этой ссылки уже лежит в кеше:
        # отдаётся прошлый разбор (как при сетевом сбое), а не вежливый отказ
        thread = make_thread(4242)
        fetch = make_article_fetch(_fixture_html("junk_page.html"))
        article = _article_extractor(fetch)
        deps = make_deps(
            thread=thread,
            llm=make_llm(analysis_json=dict(ARTICLE_ANALYSIS)),
            router=make_router([_youtube_no_network(), article]),
            jobs=make_job_store(),
        )
        canonical = article.canonical_id(ARTICLE_URL)
        deps.reports.put(
            canonical,
            str(deps.prompts["version"]),
            REPORT_SCHEMA,
            Report.from_analysis(dict(ARTICLE_ANALYSIS)).to_json(),
            "любой-старый-хэш",
        )
        msg = make_message(f"почитай {ARTICLE_URL}", thread=thread)

        asyncio.run(handle(msg, deps))
        _run_worker_once(deps)

        assert thread.sent, "отчёт из кеша доставлен вместо отказа"
        assert "Суть статьи в двух предложениях." in "\n".join(thread.sent)
        assert POLITE_REJECTION not in msg.status.edits, "вежливого отказа нет"
        assert msg.status.deleted, "статус закрыт после доставки"
        assert deps.llm.calls == [], "LLM не зовётся — отчёт уже в кеше"
        assert deps.jobs.count_new() == 0
        assert deps.jobs.failed_calls == [], "доставка из кеша — не ошибка джобы"

    def test_site_500_posts_error_and_marks_job_failed(
        self, make_deps, make_message, make_thread, make_llm, make_article_fetch, make_job_store,
        make_router,
    ):
        thread = make_thread(4242)
        fetch = make_article_fetch(_fixture_html("article_full.html"), status=500)
        article = _article_extractor(fetch)
        deps = make_deps(
            thread=thread,
            llm=make_llm(),
            router=make_router([_youtube_no_network(), article]),
            jobs=make_job_store(),
        )
        msg = make_message(f"глянь {ARTICLE_URL}", thread=thread)

        asyncio.run(handle(msg, deps))
        _run_worker_once(deps)

        # сетевой сбой сайта — обычный error-путь, а не вежливый отказ
        assert len(thread.sent) == 1
        assert "страница недоступна" in thread.sent[0]
        assert POLITE_REJECTION not in thread.sent[0]
        assert msg.status.deleted
        assert len(deps.jobs.failed_calls) == 1, "джоба помечена failed"
        assert "страница недоступна" in deps.jobs.failed_calls[0][1]
        assert deps.llm.calls == []

    def test_no_author_block_for_articles(
        self, make_deps, make_message, make_thread, make_llm, make_article_fetch, make_job_store,
        make_router,
    ):
        # у статьи есть meta author, но автор-блока быть не должно: автор статьи
        # живёт в doc.data["author"] и не попадает в обзор автора (author_ref None)
        thread = make_thread(4242)
        fetch = make_article_fetch(_fixture_html("article_full.html"))
        article = _article_extractor(fetch)
        from bot.analysis.author import YouTubeAuthorFactsProvider

        network_calls = []

        def _no_network(url, *args, **kwargs):
            network_calls.append(url)
            raise AssertionError("провайдер автора YouTube не должен ходить в сеть для статьи")

        provider = YouTubeAuthorFactsProvider(fetch=_no_network)
        deps = make_deps(
            thread=thread,
            llm=make_llm(analysis_json=dict(ARTICLE_ANALYSIS)),
            author_facts=provider,
            router=make_router([_youtube_no_network(), article]),
            jobs=make_job_store(),
        )
        msg = make_message(f"почитай {ARTICLE_URL}", thread=thread)

        asyncio.run(handle(msg, deps))
        _run_worker_once(deps)

        assert network_calls == [], "для не-YouTube ссылки сеть провайдера не трогается"
        assert all("**Обзор автора**" not in c for c in thread.sent), (
            "автор-блока у статей нет даже при meta author"
        )
        assert deps.llm.review_calls == []
        cached = deps.reports.get_latest(
            article.canonical_id(ARTICLE_URL), str(deps.prompts["version"]), REPORT_SCHEMA
        )
        assert cached is not None
        assert [b.kind for b in Report.from_json(cached).blocks] == ["brief", "theses", "detailed"], (
            "в кеше отчёт без author-блока"
        )
