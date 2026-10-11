"""Юнит-тесты YouTube-источника (фаза 4): регекс, экстрактор за портом.

TestExtractVideoId — парсер ссылок (не менялся фазой 4). TestYoutubeExtractor —
YoutubeExtractor, первый экстрактор за портом ExtractorPort (ARCHITECTURE.md
§5-§6): find(text) -> (start, end) | None — основной метод (границы для Router),
matches(url) производен от find, canonical_id(url) -> "youtube:video:<id>" | None,
extract(url) -> SourceDocument через инжектируемый fetch_transcript (сеть только
за ним: oEmbed-метаданные ушли провайдеру автора, title в extract не добывается).
Сети в тестах нет. Имя треда (thread_name_from_message) переехало в e2e
(tests/e2e/test_handle.py) — оно пользовательски видимое и теперь принимает Route.
"""

import pytest

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


class TestYoutubeExtractor:
    """Экстрактор за портом: find/matches/canonical_id/extract; DI транскрипта."""

    URL = "https://youtu.be/abcdefghijk"
    VIDEO_ID = "abcdefghijk"

    def _extractor(self, transcript="субтитры", exc=None):
        from bot.sources.youtube import YoutubeExtractor

        calls = []

        def _fetch(video_id):
            calls.append(video_id)
            if exc is not None:
                raise exc
            return transcript

        extractor = YoutubeExtractor(fetch_transcript=_fetch)
        extractor.fetch_calls = calls
        return extractor

    def test_name_is_video(self):
        assert self._extractor().name == "video"

    def test_matches_true_for_supported_forms(self):
        extractor = self._extractor()
        for url in (
            self.URL,
            "https://www.youtube.com/watch?v=abcdefghijk",
            "https://www.youtube.com/shorts/abcdefghijk",
            "youtu.be/abcdefghijk",
            f"смотри {self.URL} в конце",
        ):
            assert extractor.matches(url) is True, url

    def test_matches_false_for_other_sites_and_empty(self):
        extractor = self._extractor()
        for text in ("", "https://vimeo.com/12345", "просто текст"):
            assert extractor.matches(text) is False, repr(text)

    def test_matches_is_derived_from_find(self):
        # зафиксировано контрактом фазы 4: matches(url) == (find(url) is not None)
        extractor = self._extractor()
        for text in (self.URL, "https://vimeo.com/1", "", f"текст {self.URL} хвост"):
            assert extractor.matches(text) == (extractor.find(text) is not None), repr(text)

    def test_find_returns_url_span(self):
        extractor = self._extractor()
        text = f"глянь {self.URL} потом"

        span = extractor.find(text)

        assert span is not None
        start, end = span
        assert text[start:end] == self.URL

    def test_find_none_for_plain_text(self):
        assert self._extractor().find("текст без ссылок") is None

    def test_canonical_id_formats(self):
        extractor = self._extractor()
        for url in (
            self.URL,
            "https://www.youtube.com/watch?v=abcdefghijk",
            "https://www.youtube.com/shorts/abcdefghijk",
        ):
            assert extractor.canonical_id(url) == f"youtube:video:{self.VIDEO_ID}", url

    def test_canonical_id_none_without_link(self):
        assert self._extractor().canonical_id("https://vimeo.com/12345") is None

    def test_module_canonical_video_id_is_thin_wrapper(self):
        # модульная функция остаётся для ключей кеша (tests/unit/test_canonical.py)
        # и совпадает с методом экстрактора
        assert youtube.canonical_video_id(self.VIDEO_ID) == self._extractor().canonical_id(self.URL)

    def test_extract_builds_source_document(self):
        extractor = self._extractor(transcript="субтитры видео")

        doc = extractor.extract(self.URL)

        assert doc.canonical_id == f"youtube:video:{self.VIDEO_ID}"
        assert doc.kind == "video"
        assert doc.url == self.URL
        assert doc.content == "субтитры видео"
        # метаданных в extract нет: oEmbed ушел провайдеру автора (фаза 4) —
        # сети кроме транскрипта не будет, title остаётся пустым
        assert doc.title == ""
        assert doc.author_ref is None
        assert extractor.fetch_calls == [self.VIDEO_ID], "транскрипт качается по video_id"

    def test_extract_propagates_content_failure(self):
        # контракт порта: сбой добычи контента — исключение наружу (error-путь
        # пайплайна и mark_failed джобы решают, что с ним делать)
        extractor = self._extractor(exc=RuntimeError("нет доступных субтитров"))

        with pytest.raises(RuntimeError):
            extractor.extract(self.URL)

    def test_constructs_without_injection(self):
        # дефолт — модульный fetch_transcript; создаётся без сети, вызовов не делаем
        from bot.sources.youtube import YoutubeExtractor

        assert callable(YoutubeExtractor().extract)

    def test_conforms_to_extractor_port(self):
        from bot.sources.base import ExtractorPort

        assert isinstance(self._extractor(), ExtractorPort)


def test_fetch_transcript_is_available():
    # сигнатура закреплена (дефолт экстрактора), но саму сеть не тестируем
    assert callable(youtube.fetch_transcript)
