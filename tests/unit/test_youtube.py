"""Юнит-тесты распознавания YouTube-ссылок и имени треда."""

from bot.sources import youtube


class TestExtractVideoId:
    def test_watch_url_with_params(self):
        text = "глянь https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=30s круто?"
        assert youtube.extract_video_id(text) is not None
        assert youtube.extract_video_id(text)[0] == "dQw4w9WgXcQ"

    def test_bare_domain_without_scheme(self):
        assert youtube.extract_video_id("youtube.com/watch?v=abcdefghijk")[0] == "abcdefghijk"

    def test_short_link(self):
        assert youtube.extract_video_id("https://youtu.be/M7lc1UVf-VE?list=x")[0] == "M7lc1UVf-VE"

    def test_shorts_live_embed(self):
        for url in (
            "https://www.youtube.com/shorts/abcdefghijk",
            "https://www.youtube.com/live/abcdefghijk",
            "https://www.youtube.com/embed/abcdefghijk",
        ):
            assert youtube.extract_video_id(url)[0] == "abcdefghijk", url

    def test_mobile_prefix(self):
        assert youtube.extract_video_id("https://m.youtube.com/watch?v=abcdefghijk")[0] == "abcdefghijk"

    def test_url_inside_multiline_message(self):
        text = "привет\nвот видео:\nhttps://youtu.be/abcdefghijk\nспасибо"
        assert youtube.extract_video_id(text)[0] == "abcdefghijk"

    def test_match_positions_bound_the_url(self):
        text = "до https://youtu.be/abcdefghijk после"
        _, start, end = youtube.extract_video_id(text)
        assert text[start:end].endswith("abcdefghijk")

    def test_no_match_for_other_sites(self):
        for text in ("", None, "https://vimeo.com/12345", "https://example.com/youtube.com/fake"):
            assert youtube.extract_video_id(text) is None, repr(text)

    def test_id_is_exactly_11_chars(self):
        # 10-символьный «id» — это не валидный id, регулярка не должна взять лишнее
        m = youtube.YT_RE.search("https://youtu.be/abcdefghij")
        assert m is None


class TestThreadNameFromMessage:
    def test_strips_link_keeps_text(self):
        name = youtube.thread_name_from_message(
            "смотрите https://youtu.be/abcdefghijk это интересно", "abcdefghijk"
        )
        assert "youtu" not in name and "abcdefghijk" not in name
        assert "смотрите" in name

    def test_fallback_when_only_link(self):
        name = youtube.thread_name_from_message("https://youtu.be/abcdefghijk", "abcdefghijk")
        assert name == "Видео abcdefghijk"

    def test_truncated_to_90(self):
        name = youtube.thread_name_from_message("т" * 300, "abcdefghijk")
        assert len(name) <= 90


def test_fetch_transcript_is_available():
    # сигнатура закреплена (вызывается из пайплайна по video_id), но саму сеть
    # не тестируем — проверяем только наличие точки входа
    assert callable(youtube.fetch_transcript)
