"""Юнит-тесты Discord-форматирования: таблицы -> буллеты, сплит длинных сообщений с code-fence."""

from bot.delivery import render


def test_max_message_length_constant():
    assert render.MAX_MESSAGE_LENGTH == 2000


class TestConvertTableToBullets:
    def test_simple_table_becomes_heading_plus_bullets(self):
        md = "| Платформа | Подписчики |\n|---|---|\n| YouTube | 120k |"
        out = render.convert_table_to_bullets(md)
        assert "|" not in out
        assert "**YouTube**" in out
        assert "• Подписчики: 120k" in out

    def test_text_without_tables_untouched(self):
        md = "Просто текст\n- пункт один\n- пункт два"
        assert render.convert_table_to_bullets(md) == md

    def test_table_inside_code_fence_preserved(self):
        md = "```\n| a | b |\n|---|---|\n| 1 | 2 |\n```"
        assert render.convert_table_to_bullets(md) == md

    def test_mixed_fence_and_table(self):
        md = (
            "текст до\n```\nвнутри фенса\n```\n"
            "| Колонка | Значение |\n|---|---|\n| X | 1 |\nтекст после"
        )
        out = render.convert_table_to_bullets(md)
        assert "внутри фенса" in out
        assert "• Значение: 1" in out


class TestFormatForDiscord:
    def test_converts_tables(self):
        md = "| a | b |\n|---|---|\n| 1 | 2 |"
        assert "|" not in render.format_for_discord(md)

    def test_empty_string_passthrough(self):
        assert render.format_for_discord("") == ""


class TestFenceState:
    def test_open_fence_with_lang(self):
        assert render._fence_state_after("```python\nx=1", False, "") == (True, "python")

    def test_opened_and_closed(self):
        assert render._fence_state_after("```python\nx=1\n```", False, "") == (False, "python")


class TestTruncateMessage:
    def test_short_message_single_chunk(self):
        assert render.truncate_message("коротко") == ["коротко"]

    def test_long_message_chunks_within_limit_with_indicators(self):
        content = "\n".join(f"строка {i} " + "x" * 100 for i in range(80))  # ~8900 символов
        chunks = render.truncate_message(content)
        assert len(chunks) > 1
        for c in chunks:
            assert len(c) <= 2000
            assert c.endswith(f"({chunks.index(c) + 1}/{len(chunks)})")
        # содержимое не теряется (маркеры и переводы строк — единственные добавки)
        glued = " ".join(c.rsplit(" (", 1)[0] for c in chunks)
        original_words = set(content.split())
        assert original_words <= set(glued.split())

    def test_split_inside_code_fence_reopens_it(self):
        code = "```python\n" + "\n".join(f"print({i})" for i in range(400)) + "\n```"
        chunks = render.truncate_message(code)
        assert len(chunks) > 1
        # первый чанк закрывает фенс, второй — переоткрывает с тем же языком
        # (в конце чанков стоит индикатор " (i/N)", учитываем его)
        first_body = chunks[0].removesuffix(f" (1/{len(chunks)})")
        assert first_body.rstrip().endswith("```")
        assert "```python" in chunks[1]
        # склейка чанков восстанавливает валидный markdown: чётное число фенсов
        fences = sum(c.count("\n```") + c.startswith("```") for c in chunks)
        assert fences % 2 == 0

    def test_unsplittable_wall_of_chars_hard_split(self):
        content = "а" * 5000  # ни переводов строки, ни пробелов
        chunks = render.truncate_message(content)
        for c in chunks:
            assert len(c) <= 2000

    def test_max_length_lower_threshold(self):
        chunks = render.truncate_message("y" * 3000, 1000)
        assert all(len(c) <= 1000 for c in chunks)
        assert len(chunks) >= 3
