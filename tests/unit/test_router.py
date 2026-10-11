"""Юнит-тесты роутера (фаза 4, bot/router.py): «ссылка -> экстрактор».

Контракт: Router(extractors) обходит экстракторы ПО ПОРЯДКУ — первый, чей
find(text) нашёл совпадение, и побеждает (приоритет = порядок реестра); Route
несёт extractor, url (совпавшая подстрока исходного текста) и границы
start/end, так что text[route.start:route.end] == route.url; by_name находит
экстрактор по имени (джобы ссылаются на экстракторы по kind == extractor.name).
Импорты bot.router ленивые внутри тестов (TDD): до реализации фазы 4 падает
конкретный тест, а не коллекция файла.
"""

import dataclasses


def _router(extractors):
    from bot.router import Router

    return Router(extractors)


class TestRoute:
    def test_route_fields(self):
        from bot.router import Route

        fields = [f.name for f in dataclasses.fields(Route)]
        assert fields == ["extractor", "url", "start", "end"]

    def test_first_matching_extractor_wins(self, make_extractor):
        # порядок реестра = приоритет: оба экстрактора совпадают — побеждает первый
        first = make_extractor()
        second = make_extractor()
        router = _router([first, second])

        route = router.route("глянь https://youtu.be/abcdefghijk")

        assert route.extractor is first

    def test_second_extractor_used_when_first_misses(self, make_extractor):
        # find_span=(0, 0) не проходит start < end -> find возвращает None;
        # find_span=None означало бы «ищи первый URL» и находило бы ссылку
        first = make_extractor(find_span=(0, 0))
        second = make_extractor()
        router = _router([first, second])

        route = router.route("глянь https://youtu.be/abcdefghijk")

        assert route.extractor is second

    def test_route_none_for_plain_text(self, make_extractor):
        router = _router([make_extractor()])

        assert router.route("просто текст, без ссылок") is None

    def test_route_none_for_empty_registry(self):
        router = _router([])

        assert router.route("https://youtu.be/abcdefghijk") is None


class TestRouteBoundaries:
    def test_route_bounds_slice_the_url_out_of_text(self, make_extractor):
        router = _router([make_extractor()])
        text = "глянь https://youtu.be/abcdefghijk потом"

        route = router.route(text)

        assert route.start < route.end
        assert text[route.start : route.end] == "https://youtu.be/abcdefghijk"

    def test_route_url_equals_matched_substring(self, make_extractor):
        router = _router([make_extractor()])
        text = "до https://example.com/page после"

        route = router.route(text)

        assert route.url == text[route.start : route.end] == "https://example.com/page"

    def test_route_respects_configured_span(self, make_extractor):
        # find_span — управляемые границы: роутер берёт их как есть
        extractor = make_extractor(find_span=(4, 30))
        router = _router([extractor])
        text = "----" + "x" * 40

        route = router.route(text)

        assert (route.start, route.end) == (4, 30)
        assert route.url == text[4:30]


class TestByName:
    def test_by_name_returns_extractor(self, make_extractor):
        extractor = make_extractor()
        router = _router([extractor])

        assert router.by_name("video") is extractor

    def test_by_name_unknown_returns_none(self, make_extractor):
        router = _router([make_extractor()])

        assert router.by_name("article") is None

    def test_by_name_across_multiple_extractors(self, make_extractor):
        video = make_extractor()
        article = make_extractor()
        article.name = "article"
        router = _router([video, article])

        assert router.by_name("video") is video
        assert router.by_name("article") is article


class TestRealRegistryPhase5:
    """Фаза 5: боевой реестр [YoutubeExtractor, ArticleExtractor].

    ArticleExtractor — fallback: find находит ЛЮБОЙ http(s)-URL (включая
    YouTube), но приоритет реестра отдаёт YT-ссылку YoutubeExtractor'у.
    Импорт ArticleExtractor ленивый: до реализации фазы 5 падает этот класс,
    а не коллекция файла.
    """

    def _registry(self):
        from bot.sources.article import ArticleExtractor
        from bot.sources.youtube import YoutubeExtractor

        return _router([YoutubeExtractor(), ArticleExtractor()])

    def test_youtube_link_routed_to_youtube_extractor(self):
        route = self._registry().route("глянь https://youtu.be/abcdefghijk")

        assert route is not None
        assert route.extractor.name == "video", "YouTube-ссылку забирает YoutubeExtractor"

    def test_article_link_reaches_article_extractor(self):
        route = self._registry().route("почитай https://example.com/news/post")

        assert route is not None
        assert route.extractor.name == "article"

    def test_article_extractor_is_fallback_for_youtube_url_too(self):
        # сам article-экстрактор YT-ссылку находит и даёт canonical —
        # приоритет решает только порядок в реестре
        from bot.sources.article import ArticleExtractor

        article = ArticleExtractor()
        url = "https://youtu.be/abcdefghijk"
        assert article.find(url) is not None
        assert article.canonical_id(url) is not None
