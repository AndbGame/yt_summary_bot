"""E2E фазы 2 (+джобы фазы 4): полный путь JSON-отчёта LLM в треде.

Сценарии:
- валидный JSON-ответ LLM -> тред получает [кратко(+контекст сложной темы),
  тезисы, разделы с таймкод-ссылками];
- превышенный бюджет блока -> рендер помечает сообщение маркером обрезки,
  доставка не падает;
- LLM вернул мусор (не JSON) после всех попыток -> бот молчит (сообщений об
  ошибках больше нет), в кеш ничего не попадает; process_job пробрасывает
  исключение (воркер пометит джобу failed, фаза 4).

Отдельного «текстового» фоллбека в контракте НЕТ: непарсимый ответ — это ретраи
LLMClient и, после исчерпания попыток, исключение -> тишина (здесь эмулируется
фейком через raw=..., ретраи покрыты tests/unit/test_llm.py).
"""

import asyncio

import pytest

from bot.analysis.report import ReportParseError
from bot.pipeline import process_job

VIDEO_ID = "abcdefghijk"
URL = f"https://youtu.be/{VIDEO_ID}"
LINK = URL

ANALYSIS = {
    "brief": "Видео про настройку домашнего сервера.",
    "complexity": "specialized",
    "context": "это пошаговая инструкция по установке, объяснённая для новичков",
    "theses": ["первый тезис", "второй тезис", "третий тезис"],
    "sections": [
        {"title": "Установка", "start_sec": 75, "body": "Шаги установки системы."},
        {"title": "Настройка", "start_sec": 3675, "body": "Разбор параметров конфига."},
    ],
}

# бюджеты как в prompts.yaml фазы 2
BUDGETS = {"brief": 240, "context": 400, "theses": 1200, "section": 1200, "author": 900}
PROMPTS = {"version": "2", "budgets": BUDGETS}


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


class TestProgressiveDelivery:
    def test_full_report_delivered_block_by_block(
        self, make_deps, make_thread, make_status, make_llm, make_extractor, make_router
    ):
        thread = make_thread(4242)
        status = make_status()
        extractor = make_extractor(transcripts={URL: "субтитры"})
        deps = make_deps(
            thread=thread,
            llm=make_llm(analysis_json=ANALYSIS),
            prompts=dict(PROMPTS),
            router=make_router([extractor]),
        )

        asyncio.run(process_job(deps, _job(extractor, thread.id, status=status)))

        sent = thread.sent
        # порядок сообщений: [кратко(+контекст), тезисы, тайминги одной строкой]
        assert [s.split("\n")[0] for s in sent] == [
            f"**Разбор: <{LINK}>**",
            "**Тезисы**",
            "**Подробно**",
        ]
        # сообщение 1: заголовок разбора + кратко + метка сложности + контекст
        assert sent[0].startswith(f"**Разбор: <{LINK}>**\n\n**Кратко**\n\nВидео про настройку домашнего сервера.")
        assert "⚠️ Узкоспециальная тема." in sent[0]
        assert "> *Простыми словами:* это пошаговая инструкция по установке, объяснённая для новичков" in sent[0]
        # метка сложности идёт ДО контекста, но после краткого
        assert sent[0].index("⚠️") > sent[0].index("Видео про настройку")
        assert sent[0].index("⚠️") < sent[0].index("*Простыми словами:*")
        # сообщение 2: тезисы
        assert sent[1] == "**Тезисы**\n\n- первый тезис\n- второй тезис\n- третий тезис"
        # тайминги — по строке на фрагмент; ссылки маскированы <url>, иначе
        # Discord на каждый тайминг рисует превью
        assert sent[2] == (
            "**Подробно**\n\n"
            f"[01:15](<{LINK}?t=75>) — **Установка**: Шаги установки системы.\n"
            f"[1:01:15](<{LINK}?t=3675>) — **Настройка**: Разбор параметров конфига."
        )

        assert status.deleted
        assert deps.llm.calls == ["субтитры"]

    def test_over_budget_block_truncated_with_marker(
        self, make_deps, make_thread, make_llm, make_extractor, make_router
    ):
        thread = make_thread(4242)
        tight = {
            "version": "2",
            "error": "Не смог: {reason}",
            "budgets": {"brief": 20, "context": 400, "theses": 1200, "section": 1200, "author": 900},
        }
        extractor = make_extractor(transcripts={URL: "субтитры"})
        deps = make_deps(
            thread=thread,
            llm=make_llm(analysis_json={"brief": "Б" * 60, "theses": ["тезис"]}),
            prompts=tight,
            router=make_router([extractor]),
        )

        asyncio.run(process_job(deps, _job(extractor, thread.id, status=None)))

        assert len(thread.sent) == 2
        first = thread.sent[0]
        # brief обрезан до бюджета и помечен маркером; полный текст не ушёл в тред
        assert first.startswith(f"**Разбор: <{LINK}>**\n\n**Кратко**\n\n{'Б' * 20}")
        assert first.endswith(" …(обрезано)")
        assert "Б" * 60 not in first
        # тезисы в бюджете — не обрезаны
        assert thread.sent[1] == "**Тезисы**\n\n- тезис"

    def test_garbage_answer_raises_silently_and_not_cached(
        self, make_deps, make_thread, make_status, make_llm, make_extractor, make_router
    ):
        thread = make_thread(4242)
        status = make_status()
        extractor = make_extractor(transcripts={URL: "субтитры"})
        deps = make_deps(
            thread=thread,
            llm=make_llm(raw="мне жаль, я не понял запрос"),
            prompts=dict(PROMPTS),
            router=make_router([extractor]),
        )

        with pytest.raises(ReportParseError):
            asyncio.run(process_job(deps, _job(extractor, thread.id, status=status)))

        assert thread.sent == [], "бот молчит: сообщений об ошибках нет"
        assert status.deleted, "статус старого формата удаляется даже при ошибке"
        assert not deps.reports._reports, "ошибка не кешируется"
