"""Юнит-тесты модели отчёта фазы 2 (bot/analysis/report.py).

Контракт (docs/ARCHITECTURE.md §5-6, §8-9):
- Report — список ReportBlock (kind из KINDS); сериализация to_json/from_json
  для кеша отчётов (блоки в JSON — list[dict]);
- Report.from_analysis собирает отчёт из JSON-ответа LLM
  {brief, complexity, context, theses, sections[{title, start_sec, body}]};
- parse_report_json — терпеливый парсер сырья LLM: прямой JSON / ```json-фенс /
  подстрока от первого "{" до последнего "}"; мимо — ReportParseError,
  чей текст включает фрагмент сырья;
- validate_budgets — человекочитаемые предупреждения о превышении бюджетов
  (применяются к brief, context, сумме theses и каждому detailed; бюджет
  detailed-блоков лежит в ключе budgets["section"], как в prompts.yaml);
- render_report_messages — прогрессивный рендер: кратко (+контекст для сложных
  тем) -> тезисы -> подробно по разделам с таймкод-ссылками -> обзор автора.
  Заголовок разбора рендер НЕ добавляет (это делает pipeline), detailed-блоки
  рендер не режет (режет позже чанк-сплиттер пайплайна).
"""

import json

import pytest

from bot.analysis.report import (
    KINDS,
    Report,
    ReportBlock,
    ReportParseError,
    parse_report_json,
    render_report_messages,
    validate_budgets,
)

URL = "https://youtu.be/testvid01"
TRUNCATED = " …(обрезано)"

FULL_ANALYSIS = {
    "brief": "Суть видео.",
    "complexity": "simple",
    "context": "это разбор устройства сервера для новичков",
    "theses": ["тезис один", "тезис два"],
    "sections": [
        {"title": "Вступление", "start_sec": 75, "body": "тело вступления"},
        {"title": "Детали", "body": "тело деталей"},
    ],
}


def _sig(report):
    """Подпись отчёта: кортежи полей блоков (сравнение без __eq__)."""
    return [(b.kind, b.title, b.body_md, dict(b.data)) for b in report.blocks]


class TestReportBlock:
    def test_kinds_fixed_list(self):
        assert KINDS == ["brief", "context", "theses", "detailed", "comments", "author"]

    def test_defaults_are_empty(self):
        block = ReportBlock("brief")
        assert block.title == ""
        assert block.body_md == ""
        assert block.data == {}

    def test_data_defaults_not_shared_between_blocks(self):
        # mutable default не должен быть общим объектом между экземплярами
        b1 = ReportBlock("brief")
        b2 = ReportBlock("brief")
        b1.data["x"] = 1
        assert b2.data == {}


class TestFromAnalysis:
    def test_full_analysis_maps_to_blocks_in_order(self):
        report = Report.from_analysis(FULL_ANALYSIS)

        assert [b.kind for b in report.blocks] == ["brief", "context", "theses", "detailed", "detailed"]
        brief, context, theses, d1, d2 = report.blocks
        assert brief.body_md == "Суть видео."
        assert context.body_md == FULL_ANALYSIS["context"]
        assert theses.body_md == "- тезис один\n- тезис два"
        assert (d1.title, d1.body_md) == ("Вступление", "тело вступления")
        assert (d2.title, d2.body_md) == ("Детали", "тело деталей")

    def test_brief_data_carries_complexity(self):
        report = Report.from_analysis({"brief": "Б", "complexity": "specialized"})
        assert report.blocks[0].kind == "brief"
        assert report.blocks[0].data == {"complexity": "specialized"}

    def test_complexity_defaults_to_empty_string(self):
        report = Report.from_analysis({"brief": "Б"})
        assert report.blocks[0].data == {"complexity": ""}

    def test_empty_context_skipped(self):
        # пустой context не даёт пустого блока (и пустого сообщения в рендере)
        report = Report.from_analysis({"brief": "Б", "context": ""})
        assert [b.kind for b in report.blocks] == ["brief"]

    def test_missing_theses_skipped(self):
        report = Report.from_analysis({"brief": "Б"})
        assert [b.kind for b in report.blocks] == ["brief"]

    def test_empty_theses_skipped(self):
        # пустой список тезисов эквивалентен отсутствию — блок с пустым телом не нужен
        report = Report.from_analysis({"brief": "Б", "theses": []})
        assert [b.kind for b in report.blocks] == ["brief"]

    def test_theses_joined_as_bullets(self):
        report = Report.from_analysis({"brief": "Б", "theses": ["один", "два", "три"]})
        theses = report.blocks[1]
        assert theses.kind == "theses"
        assert theses.body_md == "- один\n- два\n- три"

    def test_sections_become_detailed_blocks_with_start_sec(self):
        report = Report.from_analysis(
            {"brief": "Б", "sections": [{"title": "S", "start_sec": 75, "body": "б"}]}
        )
        section = report.blocks[1]
        assert section.kind == "detailed"
        assert section.title == "S"
        assert section.body_md == "б"
        assert section.data == {"start_sec": 75}

    def test_sections_without_start_sec_get_none(self):
        report = Report.from_analysis({"brief": "Б", "sections": [{"title": "S", "body": "б"}]})
        assert report.blocks[1].data == {"start_sec": None}

    def test_empty_sections_list_no_detailed(self):
        report = Report.from_analysis({"brief": "Б", "theses": ["т"], "sections": []})
        assert [b.kind for b in report.blocks] == ["brief", "theses"]

    def test_empty_dict_gives_empty_report(self):
        assert Report.from_analysis({}).blocks == []


class TestJsonRoundtrip:
    def test_to_json_from_json_roundtrip(self):
        report = Report.from_analysis(FULL_ANALYSIS)
        restored = Report.from_json(report.to_json())
        assert _sig(restored) == _sig(report)

    def test_to_json_returns_str_with_unicode(self):
        report = Report([ReportBlock("brief", body_md="суть 🎬 «кавычки»")])
        raw = report.to_json()
        assert isinstance(raw, str)
        assert _sig(Report.from_json(raw)) == _sig(report)

    def test_blocks_serialized_as_list_of_dicts(self):
        # блоки в JSON — list[dict] с полями блока; верхний уровень — список или
        # обёртка с ключом blocks (контракт форму не фиксирует, проверяем блоки)
        report = Report.from_analysis({"brief": "Б", "theses": ["т"]})
        parsed = json.loads(report.to_json())
        raw_blocks = parsed if isinstance(parsed, list) else parsed["blocks"]
        assert isinstance(raw_blocks, list)
        assert all(isinstance(b, dict) for b in raw_blocks)
        assert [b["kind"] for b in raw_blocks] == ["brief", "theses"]
        assert all("body_md" in b for b in raw_blocks)


class TestParseReportJson:
    def test_plain_json(self):
        report = parse_report_json('{"brief": "Б", "theses": ["т"]}')
        assert [b.kind for b in report.blocks] == ["brief", "theses"]

    def test_json_fence_stripped(self):
        raw = '```json\n{"brief": "Б", "theses": ["т"]}\n```'
        assert [b.kind for b in parse_report_json(raw).blocks] == ["brief", "theses"]

    def test_text_around_json(self):
        raw = 'Конечно! Вот отчёт: {"brief": "Б"} Чем ещё помочь?'
        report = parse_report_json(raw)
        assert [b.kind for b in report.blocks] == ["brief"]
        assert report.blocks[0].body_md == "Б"

    def test_nested_braces_with_surrounding_text(self):
        raw = 'преамбула {"brief": "Б", "sections": [{"title": "Т", "body": "б"}]} эпилог'
        report = parse_report_json(raw)
        assert [b.kind for b in report.blocks] == ["brief", "detailed"]
        assert report.blocks[1].title == "Т"

    def test_plain_text_raises(self):
        with pytest.raises(ReportParseError):
            parse_report_json("Просто текст без JSON")

    def test_empty_input_raises(self):
        with pytest.raises(ReportParseError):
            parse_report_json("")
        with pytest.raises(ReportParseError):
            parse_report_json("   ")

    def test_broken_json_raises(self):
        with pytest.raises(ReportParseError):
            parse_report_json('```json\n{"brief": \n```')

    def test_error_includes_truncated_raw_fragment(self):
        raw = "Х" * 500
        with pytest.raises(ReportParseError) as excinfo:
            parse_report_json(raw)
        message = str(excinfo.value)
        assert raw[:100] in message, "текст ошибки включает фрагмент сырья (~200 символов)"
        assert raw not in message, "сырьё длиннее ~200 символов не входит целиком"

    def test_error_includes_short_raw_entirely(self):
        with pytest.raises(ReportParseError) as excinfo:
            parse_report_json("нет здесь отчёта")
        assert "нет здесь отчёта" in str(excinfo.value)


class TestValidateBudgets:
    def test_brief_over_budget_exact_warning(self):
        report = Report([ReportBlock("brief", body_md="Б" * 350)])
        assert validate_budgets(report, {"brief": 240}) == ["block 'brief': 350 > 240 символов"]

    def test_context_over_budget(self):
        report = Report([ReportBlock("context", body_md="К" * 401)])
        assert validate_budgets(report, {"context": 400}) == ["block 'context': 401 > 400 символов"]

    def test_theses_sum_over_budget(self):
        # бюджет применяется к сумме тезисов (телу блока theses целиком)
        report = Report([ReportBlock("theses", body_md="т" * 1405)])
        assert validate_budgets(report, {"theses": 1200}) == ["block 'theses': 1405 > 1200 символов"]

    def test_each_detailed_checked_against_section_budget(self):
        # бюджет detailed-блоков лежит в ключе "section" (prompts.yaml)
        report = Report([ReportBlock("detailed", title="S", body_md="Д" * 1300)])
        warnings = validate_budgets(report, {"brief": 240, "context": 400, "theses": 1200, "section": 1200})
        assert len(warnings) == 1
        assert "1300" in warnings[0] and "1200" in warnings[0]

    def test_two_over_budget_sections_two_warnings(self):
        report = Report(
            [
                ReportBlock("detailed", title="A", body_md="Д" * 1300),
                ReportBlock("detailed", title="B", body_md="Д" * 1500),
            ]
        )
        warnings = validate_budgets(report, {"section": 1200})
        assert len(warnings) == 2
        assert all("1200" in w for w in warnings)

    def test_no_exceeds_no_warnings(self):
        report = Report.from_analysis(FULL_ANALYSIS)
        budgets = {"brief": 240, "context": 400, "theses": 1200, "section": 1200}
        assert validate_budgets(report, budgets) == []

    def test_empty_budgets_no_warnings(self):
        report = Report([ReportBlock("brief", body_md="Б" * 500)])
        assert validate_budgets(report, {}) == []

    def test_missing_budget_key_skips_check(self):
        # нет ключа бюджета — нечего превышать
        report = Report([ReportBlock("detailed", body_md="Д" * 5000)])
        assert validate_budgets(report, {"brief": 240}) == []


class TestRenderMessages:
    def test_brief_only_single_message(self):
        report = Report.from_analysis({"brief": "Суть."})
        assert render_report_messages(report, URL) == ["**Кратко**\n\nСуть."]

    def test_full_report_message_order(self):
        report = Report.from_analysis(FULL_ANALYSIS)
        msgs = render_report_messages(report, URL)
        # кратко(+контекст) -> тезисы -> тайминги одной строкой под «Подробно»
        assert msgs[0] == (
            "**Кратко**\n\nСуть видео.\n\n> *Простыми словами:* это разбор устройства сервера для новичков"
        )
        assert msgs[1] == "**Тезисы**\n\n- тезис один\n- тезис два"
        assert msgs[2] == (
            "**Подробно**\n\n"
            f"[01:15](<{URL}?t=75>) — **Вступление**: тело вступления\n"
            "**Детали**: тело деталей"
        )
        assert len(msgs) == 3

    def test_sections_packed_into_one_message_when_fit(self):
        # компактность: три коротких раздела — одно сообщение, а не три поста
        report = Report.from_analysis(
            {
                "brief": "Б",
                "sections": [
                    {"title": "S1", "body": "а"},
                    {"title": "S2", "body": "б"},
                    {"title": "S3", "body": "в"},
                ],
            }
        )
        msgs = render_report_messages(report, URL)
        assert len(msgs) == 2
        assert "**S1**" in msgs[1] and "**S2**" in msgs[1] and "**S3**" in msgs[1]
        assert msgs[1].index("**S1**") < msgs[1].index("**S2**") < msgs[1].index("**S3**")

    def test_sections_split_when_exceeding_budget(self):
        # в пачку набирается до section_budget; превышение — новое сообщение
        report = Report.from_analysis(
            {
                "brief": "Б",
                "sections": [
                    {"title": "S1", "body": "а" * 600},
                    {"title": "S2", "body": "б" * 600},
                    {"title": "S3", "body": "в" * 600},
                ],
            }
        )
        packed = render_report_messages(report, URL, section_budget=700)[1:]
        assert len(packed) == 3
        for message in packed:
            assert len(message) <= 700

    def test_specialized_adds_warning_line_before_context(self):
        report = Report.from_analysis(
            {"brief": "Суть.", "complexity": "specialized", "context": "объяснение для новичков"}
        )
        first = render_report_messages(report, URL)[0]
        assert first.startswith("**Кратко**\n\nСуть.")
        assert "⚠️ Узкоспециальная тема." in first
        assert "> *Простыми словами:* объяснение для новичков" in first
        # метка сложности идёт ДО контекста
        assert first.index("⚠️") < first.index("*Простыми словами:*")

    def test_context_without_specialized_no_warning(self):
        report = Report.from_analysis({"brief": "Суть.", "complexity": "simple", "context": "К"})
        first = render_report_messages(report, URL)[0]
        assert "> *Простыми словами:* К" in first
        assert "⚠️" not in first

    def test_specialized_without_context_still_warns(self):
        report = Report.from_analysis({"brief": "Суть.", "complexity": "specialized"})
        first = render_report_messages(report, URL)[0]
        assert "⚠️ Узкоспециальная тема." in first
        assert "*Простыми словами:*" not in first

    def test_start_sec_becomes_minutes_link(self):
        report = Report.from_analysis(
            {"brief": "Б", "sections": [{"title": "S", "start_sec": 75, "body": "б"}]}
        )
        msg = render_report_messages(report, URL)[1]
        # одна строка на тайминг; ссылка маскируется <url> — иначе Discord
        # рисует превью-встройку видео
        assert msg == f"**Подробно**\n\n[01:15](<{URL}?t=75>) — **S**: б"

    def test_start_sec_becomes_hours_link(self):
        report = Report.from_analysis(
            {"brief": "Б", "sections": [{"title": "S", "start_sec": 3675, "body": "б"}]}
        )
        msg = render_report_messages(report, URL)[1]
        assert f"[1:01:15](<{URL}?t=3675>) — **S**: б" in msg

    def test_start_sec_zero_still_links_to_beginning(self):
        # 0 секунд — валидный таймкод начала видео: ссылка со ?t=0, метка 00:00
        report = Report.from_analysis(
            {"brief": "Б", "sections": [{"title": "S", "start_sec": 0, "body": "б"}]}
        )
        msg = render_report_messages(report, URL)[1]
        assert f"[00:00](<{URL}?t=0>) — **S**: б" in msg

    def test_section_without_start_sec_no_link(self):
        report = Report.from_analysis({"brief": "Б", "sections": [{"title": "S", "body": "б"}]})
        assert render_report_messages(report, URL)[1] == "**Подробно**\n\n**S**: б"

    def test_section_body_collapsed_to_one_line(self):
        # LLM вернул переносы — рендер схлопывает: строка есть строка
        report = Report.from_analysis(
            {"brief": "Б", "sections": [{"title": "S", "start_sec": 10, "body": "первая\n\nвторая\nтретья"}]}
        )
        msg = render_report_messages(report, URL)[1]
        assert "первая вторая третья" in msg
        assert "\nвторая" not in msg

    def test_author_block_rendered_last_regardless_of_position(self):
        report = Report(
            [
                ReportBlock("author", body_md="текст обзора"),
                ReportBlock("brief", body_md="Суть."),
            ]
        )
        assert render_report_messages(report, URL) == [
            "**Кратко**\n\nСуть.",
            "**Об авторе**\n\nтекст обзора",
        ]

    def test_missing_blocks_skipped(self):
        # нет theses -> нет сообщения тезисов; есть только то, что есть
        report = Report.from_analysis({"sections": [{"title": "S", "body": "б"}]})
        assert render_report_messages(report, URL) == ["**Подробно**\n\n**S**: б"]

    def test_empty_report_renders_no_messages(self):
        assert render_report_messages(Report([]), URL) == []

    def test_budgets_truncate_brief_context_theses_with_marker(self):
        budgets = {"brief": 20, "context": 15, "theses": 25, "section": 1200}
        report = Report.from_analysis(
            {
                "brief": "Б" * 40,
                "context": "К" * 40,
                "theses": ["т" * 30, "т" * 30],  # тело 64 символа > 25
            }
        )
        msgs = render_report_messages(report, URL, budgets=budgets)
        first, theses_msg = msgs[0], msgs[1]

        assert TRUNCATED in first
        assert first.count(TRUNCATED) == 2, "обрезаны и brief, и context"
        assert "Б" * 40 not in first and "К" * 40 not in first
        assert "Б" * 5 in first and "К" * 5 in first

        assert TRUNCATED in theses_msg
        assert ("- " + "т" * 30) not in theses_msg, "тезисы обрезаны до бюджета"

    def test_budget_truncates_section_line_with_marker(self):
        # секция — одна строка: бюджет секции режет тело до лимита с маркером
        budgets = {"brief": 240, "context": 400, "theses": 1300, "section": 50}
        report = Report.from_analysis({"brief": "Б", "sections": [{"title": "S", "body": "Д" * 200}]})
        msgs = render_report_messages(report, URL, budgets=budgets)
        assert msgs[1] == f"**Подробно**\n\n**S**: {'Д' * 50}{TRUNCATED}"
        assert "Д" * 51 not in msgs[1]

    def test_no_budgets_no_truncation(self):
        report = Report.from_analysis({"brief": "Б" * 500})
        assert render_report_messages(report, URL, budgets=None) == [f"**Кратко**\n\n{'Б' * 500}"]
