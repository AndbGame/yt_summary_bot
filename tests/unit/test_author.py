"""Юнит-тесты сборщика фактов автора (bot/analysis/author.py).

Контракт (docs/ARCHITECTURE.md §5-§6; MISS-путь фазы 4):
- AuthorRef — стабильный ключ автора (platform, handle);
- AuthorFacts — факты (name/url/description/extra) с to_dict/from_dict (кеш автора);
- YouTubeAuthorFactsProvider.get_author_ref(url): oEmbed
  https://www.youtube.com/oembed?url=<video-url>&format=json даёт author_url
  (handle — последний сегмент пути: …/channel/UCxxx -> UCxxx, /@name -> @name);
  ЭТО ЕДИНСТВЕННЫЙ запрос — описание канала get_author_ref не добывает, факты
  берутся отдельно через get_channel(ref); ЛЮБОЙ сбой сети/парса -> None
  (метод не бросает), oEmbed-ответ без author_url -> None;
- get_channel(ref): для youtube страница https://www.youtube.com/channel/{handle}
  (UC-хэндл) или https://www.youtube.com/{handle} (@-хэндл); имя из <title>
  до " - YouTube", описание из og:description; сбой -> None.

Сети в тестах нет: провайдер принимает fetch(url, timeout) -> (status, text) | None —
в тестах фейк с двумя маршрутами (oEmbed / страница канала), в проде requests.
Точный HTML/разметку не фиксируем — только контракт данных и отказов.
Импорт bot.analysis.author — ленивый внутри тестов (TDD): пока фаза не
реализована, падает конкретный тест, а не ошибкой коллекции всего прогона.
"""

import json
from urllib.parse import unquote

VIDEO_ID = "abcdefghijk"

CHANNEL_PAGE = (
    "<html><head><title>Название канала - YouTube</title>"
    '<meta property="og:description" content="Описание канала"></head></html>'
)


class FakeFetch:
    """Фейк fetch(url, timeout) -> (status, text) | None с двумя маршрутами: запрос
    к oEmbed (по подстроке 'oembed' в URL) и страница канала — всё остальное.
    По умолчанию оба маршрута «упали» (None) — как сеть, лежащая целиком."""

    def __init__(self, oembed=None, page=None, raise_on_call=False):
        self.oembed = oembed
        self.page = page
        self.raise_on_call = raise_on_call
        self.urls = []

    def __call__(self, url, *args, **kwargs):
        # сигнатура fetch(url, timeout): timeout принимается, но не фиксируется тестами
        self.urls.append(url)
        if self.raise_on_call:
            raise ConnectionError("сеть недоступна")
        if "oembed" in url:
            return self.oembed
        return self.page


def _oembed_json(author_name="Имя Канала", author_url="https://www.youtube.com/channel/UC1234567"):
    return json.dumps({"author_name": author_name, "author_url": author_url})


def _provider(fetch=None):
    """Лениво импортирует и собирает провайдер с подменённым fetch (лениво: TDD)."""
    from bot.analysis.author import YouTubeAuthorFactsProvider

    return YouTubeAuthorFactsProvider(fetch=fetch)


class TestAuthorRef:
    def test_fields(self):
        from bot.analysis.author import AuthorRef  # лениво: TDD

        ref = AuthorRef(platform="youtube", handle="UC123")
        assert ref.platform == "youtube"
        assert ref.handle == "UC123"

    def test_positional_field_order(self):
        from bot.analysis.author import AuthorRef  # лениво: TDD

        # порядок полей контракта: (platform, handle)
        ref = AuthorRef("youtube", "@name")
        assert (ref.platform, ref.handle) == ("youtube", "@name")


class TestAuthorFacts:
    def test_defaults_are_empty(self):
        from bot.analysis.author import AuthorFacts  # лениво: TDD

        facts = AuthorFacts()
        assert facts.name == ""
        assert facts.url == ""
        assert facts.description == ""
        assert facts.extra == {}

    def test_extra_default_not_shared_between_instances(self):
        from bot.analysis.author import AuthorFacts  # лениво: TDD

        # mutable default (extra=dict()) не должен быть общим объектом между экземплярами
        facts = AuthorFacts()
        facts.extra["subscribers"] = 1000
        assert AuthorFacts().extra == {}

    def test_to_dict_from_dict_roundtrip(self):
        from bot.analysis.author import AuthorFacts  # лениво: TDD

        facts = AuthorFacts(
            name="Имя",
            url="https://www.youtube.com/@name",
            description="описание канала",
            extra={"subscribers": 1000},
        )

        restored = AuthorFacts.from_dict(facts.to_dict())

        assert restored.name == "Имя"
        assert restored.url == facts.url
        assert restored.description == "описание канала"
        assert restored.extra == {"subscribers": 1000}


class TestProviderGetAuthorRef:
    """get_author_ref(url): ref из oEmbed по ссылке на видео; один запрос, без страницы канала."""

    VIDEO_URL = f"https://youtu.be/{VIDEO_ID}"

    def test_oembed_builds_ref(self):
        fetch = FakeFetch(oembed=(200, _oembed_json()))

        ref = _provider(fetch).get_author_ref(self.VIDEO_URL)

        assert ref is not None, "успешный oEmbed даёт AuthorRef"
        assert ref.platform == "youtube"
        assert ref.handle == "UC1234567", "handle — последний сегмент пути /channel/UCxxx"

    def test_handle_from_at_url(self):
        fetch = FakeFetch(oembed=(200, _oembed_json(author_url="https://www.youtube.com/@ivan.dev")))

        ref = _provider(fetch).get_author_ref(self.VIDEO_URL)

        assert ref is not None
        assert ref.handle == "@ivan.dev", "handle — последний сегмент пути /@name"

    def test_oembed_url_contains_video_url(self):
        fetch = FakeFetch(oembed=(200, _oembed_json()))

        _provider(fetch).get_author_ref(self.VIDEO_URL)

        requested = unquote(fetch.urls[0])
        assert requested.startswith("https://www.youtube.com/oembed")
        assert f"url=https://youtu.be/{VIDEO_ID}" in requested
        assert "format=json" in requested

    def test_only_oembed_requested_no_channel_page(self):
        # фаза 4: ref добывается без фактов — страницу канала (и её описание)
        # get_author_ref не трогает, факты идут через get_channel(ref)
        fetch = FakeFetch(oembed=(200, _oembed_json()))

        _provider(fetch).get_author_ref(self.VIDEO_URL)

        assert len(fetch.urls) == 1, "ровно один запрос — oEmbed"

    def test_provider_constructs_without_fetch(self):
        # дефолтный провайдер (requests) создаётся без сети; вызовы в тесте не делаем
        assert _provider() is not None

    def test_oembed_404_gives_none(self):
        assert _provider(FakeFetch(oembed=(404, None))).get_author_ref(self.VIDEO_URL) is None

    def test_oembed_broken_json_gives_none(self):
        provider = _provider(FakeFetch(oembed=(200, "<html>не json</html>")))
        assert provider.get_author_ref(self.VIDEO_URL) is None

    def test_oembed_without_author_url_gives_none(self):
        provider = _provider(FakeFetch(oembed=(200, json.dumps({"author_name": "Имя"}))))
        assert provider.get_author_ref(self.VIDEO_URL) is None

    def test_network_failure_gives_none(self):
        # fetch вернул None (сеть легла)
        assert _provider(FakeFetch()).get_author_ref(self.VIDEO_URL) is None

    def test_fetch_exception_gives_none(self):
        # исключение внутри fetch — тоже «сбой сети», наружу не бросается
        assert _provider(FakeFetch(raise_on_call=True)).get_author_ref(self.VIDEO_URL) is None


class TestProviderNonYoutubeUrl:
    """Фаза 5: провайдер YouTube обслуживает только YouTube-ссылки.

    Для не-YT url (статья) get_author_ref возвращает None СРАЗУ — без oEmbed,
    без сети: у статьи нет канала, автор живёт в doc.data["author"], а не в
    обзорах авторов.
    """

    NON_YT_URLS = (
        "https://example.com/news/some-article",
        "https://vimeo.com/12345",
        "не ссылка вообще",
    )

    def test_get_author_ref_none_without_any_network_call(self):
        # raise_on_call + счётчик: любая попытка дойти до oEmbed видна в fetch.urls
        fetch = FakeFetch(raise_on_call=True)
        provider = _provider(fetch)

        for url in self.NON_YT_URLS:
            assert provider.get_author_ref(url) is None, url

        assert fetch.urls == [], "для не-YouTube url fetch не вызывается вовсе"


class TestProviderGetChannel:
    def test_uc_handle_uses_channel_url(self):
        fetch = FakeFetch(page=(200, CHANNEL_PAGE))

        facts = _provider(fetch).get_channel(_ref("UC9876543"))

        assert facts is not None
        assert fetch.urls == ["https://www.youtube.com/channel/UC9876543"]
        assert facts.name == "Название канала", "имя из <title> до ' - YouTube'"
        assert facts.description == "Описание канала", "описание из og:description"

    def test_at_handle_uses_vanity_url(self):
        fetch = FakeFetch(page=(200, CHANNEL_PAGE))

        _provider(fetch).get_channel(_ref("@ivan"))

        assert fetch.urls == ["https://www.youtube.com/@ivan"]

    def test_title_without_suffix_used_whole(self):
        page = "<html><head><title>Просто Канал</title></head></html>"
        provider = _provider(FakeFetch(page=(200, page)))

        facts = provider.get_channel(_ref("UC1"))

        assert facts is not None
        assert facts.name == "Просто Канал"

    def test_network_failure_gives_none(self):
        assert _provider(FakeFetch()).get_channel(_ref("UC1")) is None

    def test_http_failure_gives_none(self):
        assert _provider(FakeFetch(page=(500, None))).get_channel(_ref("UC1")) is None

    def test_fetch_exception_gives_none(self):
        assert _provider(FakeFetch(raise_on_call=True)).get_channel(_ref("UC1")) is None


def _ref(handle):
    """AuthorRef ленивым импортом (лениво: TDD)."""
    from bot.analysis.author import AuthorRef

    return AuthorRef(platform="youtube", handle=handle)

class TestChannelScaleFacts:
    """Масштаб канала (подписчики/видео/с года) — best-effort из разметки шапки."""

    def test_metadata_parts_ru(self):
        from bot.analysis.author import _channel_scale_facts

        page = (
            '"metadataParts":[{"text":{"content":"246 подписчиков"},"accessibilityLabel":"246 подписчиков"},'
            '{"text":{"content":"5 видео"}}]'
        )
        assert _channel_scale_facts(page) == {"подписчики": "246 подписчиков", "видео": "5 видео"}

    def test_english_and_joined(self):
        from bot.analysis.author import _channel_scale_facts

        page = '"content":"1.53M subscribers" ..."content":"812 videos" ...joinedDateText":{"content":"Joined Mar 14, 2011"}'
        extra = _channel_scale_facts(page)
        assert extra["подписчики"] == "1.53M subscribers"
        assert extra["видео"] == "812 videos"
        assert extra["на канале с"] == "Joined Mar 14, 2011"

    def test_empty_page_no_keys(self):
        from bot.analysis.author import _channel_scale_facts

        assert _channel_scale_facts("<html></html>") == {}
