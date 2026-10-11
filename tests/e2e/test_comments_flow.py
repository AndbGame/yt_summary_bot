"""E2E-тесты блока «Комментарии» (UX-итерация): ознакомительный обзор реакций.

Контракт:
- MISS-путь: провайдер даёт топ комментариев -> comments_review (LLM) ->
  comments-блок в отчёте ДО кеширования; на кеш-хите блок приходит из payload
  без повторной добычи;
- рендер: «**Комментарии**» после «Подробно», перед «**Об авторе**»;
- любой сбой (провайдер None/исключение, LLM упал) — отчёт без блока,
  доставка не страдает.
"""

import asyncio
import hashlib
import json
from types import SimpleNamespace

from bot.pipeline import REPORT_SCHEMA, process_job

VIDEO_ID = "commentsvid01"
URL = f"https://youtu.be/{VIDEO_ID}"
TRANSCRIPT = f"субтитры {VIDEO_ID}"
COMMENTS = ["Уточнение по поводу халявы (👍 1200)", "Про кооп-прокачку (👍 300)"]


def _job(extractor, thread_id, **extra):
    job = {
        "kind": extractor.name,
        "url": URL,
        "canonical_id": extractor.canonical_id(URL),
        "thread_id": thread_id,
    }
    job.update(extra)
    return job


def _cache_key(deps, extractor):
    canonical = extractor.canonical_id(URL)
    content_hash = hashlib.sha256(TRANSCRIPT.encode()).hexdigest()[:16]
    return (canonical, str(deps.prompts.get("version", "0")), REPORT_SCHEMA, content_hash)


class TestCommentsFlow:
    def test_miss_attaches_comments_block_and_caches(
        self, make_deps, make_thread, make_llm, make_extractor, make_router, make_comments_provider,
        make_author_facts,
    ):
        thread = make_thread(4242)
        extractor = make_extractor(transcripts={URL: TRANSCRIPT})
        provider = make_comments_provider(result=COMMENTS)
        deps = make_deps(
            thread=thread,
            llm=make_llm(
                analysis_json={
                    "brief": "Суть.",
                    "theses": ["тезис"],
                    "sections": [{"title": "Раздел", "start_sec": 10, "body": "описание"}],
                }
            ),
            comments=provider,
            author_facts=make_author_facts(
                get_author_ref_result=SimpleNamespace(platform="youtube", handle="UC1"),
                get_channel_result=SimpleNamespace(name="Канал", url="https://youtube.com/channel/UC1", description="д", extra={}),
            ),
            router=make_router([extractor]),
        )

        asyncio.run(process_job(deps, _job(extractor, thread.id, status=None)))

        # блок в треде: между Подробно и Об авторе
        joined = "\n".join(thread.sent)
        assert "**Комментарии**\n\nТестовый обзор комментариев" in joined
        idx_details = joined.find("**Подробно**")
        idx_comments = joined.find("**Комментарии**")
        idx_author = joined.find("**Об авторе**")
        assert -1 < idx_details < idx_comments < idx_author, "комментарии после подробного, до автора"
        # LLM получил именно выборку комментариев
        assert deps.llm.comments_calls == [COMMENTS]
        # в кеш отчётов попал отчёт с comments-блоком
        payload = deps.reports.get(*_cache_key(deps, extractor))
        assert payload is not None
        kinds = [b["kind"] for b in json.loads(payload)["blocks"]]
        assert "comments" in kinds and "author" in kinds and "author" in kinds

    def test_hit_serves_block_from_payload_without_provider(
        self, make_deps, make_thread, make_llm, make_extractor, make_router, make_comments_provider
    ):
        thread = make_thread(4242)
        extractor = make_extractor(transcripts={URL: TRANSCRIPT})
        provider = make_comments_provider(result=COMMENTS)
        deps = make_deps(
            thread=thread,
            llm=make_llm(),
            comments=provider,
            router=make_router([extractor]),
        )
        # первый прогон: MISS, блок закеширован вместе с отчётом
        asyncio.run(process_job(deps, _job(extractor, thread.id, status=None)))
        first_calls = list(deps.llm.comments_calls)
        assert first_calls == [COMMENTS]
        sent_before = list(thread.sent)

        # второй прогон того же видео: HIT — провайдер и LLM не зовутся
        asyncio.run(process_job(deps, _job(extractor, thread.id, status=None)))

        assert deps.llm.comments_calls == first_calls, "обзор комментариев не перестраивается"
        assert provider.calls == [URL], "провайдер звался один раз"
        assert thread.sent == sent_before + sent_before, "тот же разбор доставлен повторно"

    def test_provider_none_no_block(self, make_deps, make_thread, make_llm, make_extractor, make_router, make_comments_provider):
        thread = make_thread(4242)
        extractor = make_extractor(transcripts={URL: TRANSCRIPT})
        deps = make_deps(
            thread=thread,
            llm=make_llm(),
            comments=make_comments_provider(result=None),
            router=make_router([extractor]),
        )

        asyncio.run(process_job(deps, _job(extractor, thread.id, status=None)))

        assert thread.sent, "разбор доставлен"
        assert all("**Комментарии**" not in c for c in thread.sent)
        assert deps.llm.comments_calls == [], "обзор без комментариев не строится"

    def test_llm_failure_no_block_job_survives(
        self, make_deps, make_thread, make_llm, make_extractor, make_router, make_comments_provider
    ):
        thread = make_thread(4242)
        extractor = make_extractor(transcripts={URL: TRANSCRIPT})
        llm = make_llm()
        llm.comments_error = RuntimeError("обзор не вышел")
        deps = make_deps(
            thread=thread,
            llm=llm,
            comments=make_comments_provider(result=COMMENTS),
            router=make_router([extractor]),
        )

        asyncio.run(process_job(deps, _job(extractor, thread.id, status=None)))

        assert thread.sent, "джоба пережила сбой обзора комментариев"
        assert all("**Комментарии**" not in c for c in thread.sent)
