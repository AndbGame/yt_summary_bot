"""E2E фазы 2: полный путь JSON-отчёта LLM в треде (прогрессивный рендер).

Сценарии:
- валидный JSON-ответ LLM -> тред получает [кратко(+контекст сложной темы),
  тезисы, разделы с таймкод-ссылками];
- превышенный бюджет блока -> рендер помечает сообщение маркером обрезки,
  доставка не падает;
- LLM вернул мусор (не JSON) после всех попыток -> error-промпт, в кеш ничего
  не попадает.

Отдельного «текстового» фоллбека в контракте НЕТ: непарсимый ответ — это ретраи
LLMClient и, после исчерпания попыток, исключение -> error-промпт (здесь
эмулируется фейком через raw=..., ретраи покрыты tests/unit/test_llm.py).
"""

import asyncio

from bot.pipeline import process_job

VIDEO_ID = "abcdefghijk"
LINK = f"https://youtu.be/{VIDEO_ID}"

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
PROMPTS = {"version": "2", "error": "Не смог: {reason}", "budgets": BUDGETS}


def _patch_transcript(monkeypatch, transcript="субтитры"):
    monkeypatch.setattr("bot.pipeline.fetch_transcript", lambda video_id: transcript)


class TestProgressiveDelivery:
    def test_full_report_delivered_block_by_block(
        self, monkeypatch, make_deps, make_thread, make_status, make_llm
    ):
        thread = make_thread(4242)
        status = make_status()
        deps = make_deps(
            thread=thread, llm=make_llm(analysis_json=ANALYSIS), prompts=dict(PROMPTS)
        )
        _patch_transcript(monkeypatch)

        asyncio.run(process_job(deps, {"video_id": VIDEO_ID, "thread_id": thread.id, "status": status}))

        sent = thread.sent
        # порядок сообщений: [кратко(+контекст), тезисы, разделы...]
        assert [s.split("\n")[0] for s in sent] == [
            f"**Разбор: {LINK}**",
            "**Тезисы**",
            "**Установка**",
            "**Настройка**",
        ]
        # сообщение 1: заголовок разбора + кратко + метка сложности + контекст
        assert sent[0].startswith(f"**Разбор: {LINK}**\n\n**Кратко**\n\nВидео про настройку домашнего сервера.")
        assert "⚠️ Узкоспециальная тема." in sent[0]
        assert "> *Простыми словами:* это пошаговая инструкция по установке, объяснённая для новичков" in sent[0]
        # метка сложности идёт ДО контекста, но после краткого
        assert sent[0].index("⚠️") > sent[0].index("Видео про настройку")
        assert sent[0].index("⚠️") < sent[0].index("*Простыми словами:*")
        # сообщение 2: тезисы
        assert sent[1] == "**Тезисы**\n\n- первый тезис\n- второй тезис\n- третий тезис"
        # далее разделы с таймкод-ссылками (mm:ss и h:mm:ss)
        assert sent[2].startswith(f"**Установка**\n\n[01:15]({LINK}?t=75)")
        assert "Шаги установки системы." in sent[2]
        assert f"[1:01:15]({LINK}?t=3675)" in sent[3]
        assert "Разбор параметров конфига." in sent[3]

        assert status.deleted
        assert deps.llm.calls == ["субтитры"]

    def test_over_budget_block_truncated_with_marker(
        self, monkeypatch, make_deps, make_thread, make_llm
    ):
        thread = make_thread(4242)
        tight = {
            "version": "2",
            "error": "Не смог: {reason}",
            "budgets": {"brief": 20, "context": 400, "theses": 1200, "section": 1200, "author": 900},
        }
        deps = make_deps(
            thread=thread,
            llm=make_llm(analysis_json={"brief": "Б" * 60, "theses": ["тезис"]}),
            prompts=tight,
        )
        _patch_transcript(monkeypatch)

        asyncio.run(process_job(deps, {"video_id": VIDEO_ID, "thread_id": thread.id, "status": None}))

        assert len(thread.sent) == 2
        first = thread.sent[0]
        # brief обрезан до бюджета и помечен маркером; полный текст не ушёл в тред
        assert first.startswith(f"**Разбор: {LINK}**\n\n**Кратко**\n\n{'Б' * 20}")
        assert first.endswith(" …(обрезано)")
        assert "Б" * 60 not in first
        # тезисы в бюджете — не обрезаны
        assert thread.sent[1] == "**Тезисы**\n\n- тезис"

    def test_garbage_answer_posts_error_and_not_cached(
        self, monkeypatch, make_deps, make_thread, make_status, make_llm
    ):
        thread = make_thread(4242)
        status = make_status()
        deps = make_deps(
            thread=thread,
            llm=make_llm(raw="мне жаль, я не понял запрос"),
            prompts=dict(PROMPTS),
        )
        _patch_transcript(monkeypatch)

        asyncio.run(process_job(deps, {"video_id": VIDEO_ID, "thread_id": thread.id, "status": status}))

        assert len(thread.sent) == 1, "после исчерпания попыток — один error-промпт"
        assert "Не смог" in thread.sent[0]
        # ReportParseError включает фрагмент сырья — он попадает в {reason}
        assert "мне жаль, я не понял запрос" in thread.sent[0]
        assert status.deleted
        assert not deps.reports._reports, "ошибка не кешируется"
