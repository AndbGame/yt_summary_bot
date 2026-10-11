"""Юнит-тесты экстрактора статей (фаза 5, bot/sources/article.py).

Контракт (docs/ARCHITECTURE.md §5, §12 фаза 5):
- ArticleExtractor — ПОСЛЕДНИЙ (fallback) экстрактор за портом ExtractorPort:
  ЛЮБОЙ http(s)-URL подходит (matches производен от find), YouTube-ссылки
  забирает YoutubeExtractor просто потому, что он первый в реестре;
- name == "article"; fetch(url, timeout) -> (status, headers, text) | None —
  DI как у провайдера автора, дефолт — requests-обёртка (timeout=15, браузерный UA);
- canonical_id(url) -> "article:<norm>": scheme приводится к https (одна статья
  по http и https — одна), scheme+host в нижнем регистре, fragment удаляется,
  tracking-параметры (utm_*, fbclid, gclid, yclid, mc_cid, mc_eid) удаляются,
  пустой query-знак "?" убирается, хвостовой "/" пути убирается (кроме корня);
- extract(url) -> SourceDocument(kind="article"): статус не 2xx и fetch None —
  RuntimeError «страница недоступна» (сетевой сбой, error-путь пайплайна);
  content-type без text/html и текст короче 500 знаков — NotAnArticle («по
  ссылке нет статьи», вежливый отказ, отдельная ветка пайплайна); прочие сбои
  fetch — обычные исключения наружу;
- title = short_title() readability или og:title; author — og:article:author |
  article:author | <meta name="author">; lang — <html lang>; doc.data =
  {"author": str|"", "site": host}.

Страницы для extract — собственные синтетические фикстуры tests/fixtures/
(никаких чужих статей): article_full (>800 знаков, разметка, meta), article_min
(~600 знаков — валидная статья на грани), junk_page (навигация/кнопки, <500).
Сети нет: fetch инжектируется (кроме теста дефолта — там requests.get подменён
monkeypatch'ем). Импорты bot.sources.article — ленивые внутри тестов (TDD):
до реализации фазы падает конкретный тест, а не коллекция файла.
"""

from pathlib import Path

import pytest

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures"

ARTICLE_URL = "https://example.com/news/extractor-note"

# Тело для inline-страниц метаданных: заведомо длиннее порога в 500 знаков,
# чтобы тест метаданных не падал на проверке длины, а проверял именно мету
LONG_BODY = (
    "Это синтетический абзац для набора длины: страница обязана пройти порог "
    "в пятьсот знаков, чтобы считаться статьёй, а не обрезкой навигации. " * 8
)


def _fixture_html(name: str) -> str:
    return (FIXTURES_DIR / name).read_text(encoding="utf-8")


def _norm(text: str) -> str:
    """Текст без различий в пробелах: содержимое сверяется, разметка — нет."""
    return " ".join(text.split())


def _page(*, metas="", title=None, lang="ru"):
    """Синтетическая страница с достаточной длиной текста и настраиваемой шапкой."""
    title_tag = f"<title>{title}</title>" if title is not None else ""
    return (
        '<!DOCTYPE html><html lang="ru">'
        f"<head><meta charset=\"utf-8\">{title_tag}{metas}</head>"
        f"<body><article><p>{LONG_BODY}</p></article></body></html>"
    ).replace('<html lang="ru">', f'<html lang="{lang}">')


def _extractor(fetch):
    from bot.sources.article import ArticleExtractor

    return ArticleExtractor(fetch=fetch)


class TestCanonicalId:
    """canonical_id -> "article:<norm>": ключ кеша отчётов и дедупа статей."""

    def test_format_is_article_prefix_plus_normalized_url(self, make_article_fetch):
        extractor = _extractor(make_article_fetch())

        assert extractor.canonical_id("https://example.com/A") == "article:https://example.com/A"

    def test_tracking_fragment_slash_and_case_are_one_canonical(self, make_article_fetch):
        extractor = _extractor(make_article_fetch())
        variants = [
            "https://example.com/A",
            "https://EXAMPLE.com/A/",
            "https://example.com/A/?utm_source=x",
            "https://example.com/A?utm_medium=c&utm_campaign=k&utm_term=t&utm_content=n",
            "https://example.com/A#frag",
            "https://example.com/A/?fbclid=1#top",
            "https://example.com/A?gclid=2&yclid=3&mc_cid=4&mc_eid=5",
            "HTTP://EXAMPLE.COM/A",
        ]

        assert {extractor.canonical_id(v) for v in variants} == {
            "article:https://example.com/A"
        }, "utm/fragment/хвостовой слэш/регистр не меняют canonical"

    def test_http_and_https_are_one_canonical(self, make_article_fetch):
        # осознанное решение фазы 5: одна статья по http и https — один источник
        # (scheme для canonical приводится к https, регистр — в нижний)
        extractor = _extractor(make_article_fetch())

        assert extractor.canonical_id("http://example.com/A") == "article:https://example.com/A"
        assert extractor.canonical_id("http://example.com/A") == extractor.canonical_id(
            "https://example.com/A"
        )

    def test_different_paths_are_different_sources(self, make_article_fetch):
        extractor = _extractor(make_article_fetch())

        assert extractor.canonical_id("https://example.com/A") != extractor.canonical_id(
            "https://example.com/B"
        )

    def test_tracking_removed_but_other_params_kept(self, make_article_fetch):
        extractor = _extractor(make_article_fetch())

        assert (
            extractor.canonical_id("https://example.com/A?utm_source=x&id=7")
            == "article:https://example.com/A?id=7"
        )

    def test_empty_query_mark_removed(self, make_article_fetch):
        extractor = _extractor(make_article_fetch())

        assert extractor.canonical_id("https://example.com/A?") == "article:https://example.com/A"

    def test_root_slash_is_kept(self, make_article_fetch):
        # хвостовой "/" убирается у пути, но корень «/» остаётся — иначе canonical
        # главной страницы выглядел бы как хост без пути
        extractor = _extractor(make_article_fetch())

        assert extractor.canonical_id("https://example.com/") == "article:https://example.com/"

    def test_path_params_are_part_of_canonical(self, make_article_fetch):
        # ;jsessionid=... urlparse кладёт в params — это часть пути: canonical
        # обязан сохранять его, иначе сессионные страницы склеятся в одну
        extractor = _extractor(make_article_fetch())

        assert extractor.canonical_id("https://example.com/a;jsessionid=ABC123?id=1") == (
            "article:https://example.com/a;jsessionid=ABC123?id=1"
        )

    def test_different_params_are_different_canonicals(self, make_article_fetch):
        extractor = _extractor(make_article_fetch())

        assert extractor.canonical_id("https://example.com/a;p=1") != extractor.canonical_id(
            "https://example.com/a;p=2"
        )

    def test_userinfo_is_stripped(self, make_article_fetch):
        # user:secret@ в ключе кеша не нужен: одна страница с любым логином —
        # один источник (и секрет не должен утекать в ключи/логи)
        extractor = _extractor(make_article_fetch())

        assert extractor.canonical_id("https://user:secret@example.com/A") == (
            "article:https://example.com/A"
        )


class TestFind:
    """find — основной метод порта: первый http(s)-URL; matches производен."""

    def test_first_url_span(self, make_article_fetch):
        extractor = _extractor(make_article_fetch())
        text = "почитай https://example.com/page?a=1 потом"

        span = extractor.find(text)

        assert span is not None
        start, end = span
        assert text[start:end] == "https://example.com/page?a=1"

    def test_first_of_two_urls_wins(self, make_article_fetch):
        extractor = _extractor(make_article_fetch())
        text = "раз https://first.example.org/x два https://second.example.org/y"

        start, end = extractor.find(text)

        assert text[start:end] == "https://first.example.org/x"

    def test_none_for_text_without_links(self, make_article_fetch):
        extractor = _extractor(make_article_fetch())

        assert extractor.find("текст без ссылок") is None
        assert extractor.find("") is None

    def test_youtube_link_is_found_too(self, make_article_fetch):
        # fallback-роль: article находит ЛЮБОЙ http(s)-URL, включая YouTube, —
        # но в реестре роутера YoutubeExtractor стоит первым и забирает его
        extractor = _extractor(make_article_fetch())

        assert extractor.find("глянь https://youtu.be/abcdefghijk") is not None

    def test_scheme_is_case_insensitive(self, make_article_fetch):
        # схема по RFC нечувствительна к регистру: "HTTP://..." — тоже ссылка,
        # и canonical_id обязан её нормализовать (см. тест эквивалентности выше)
        extractor = _extractor(make_article_fetch())
        text = "СМОТРИ HTTP://EXAMPLE.COM/A"

        span = extractor.find(text)

        assert span is not None
        assert text[span[0] : span[1]] == "HTTP://EXAMPLE.COM/A"

    def test_matches_any_http_url(self, make_article_fetch):
        extractor = _extractor(make_article_fetch())

        assert extractor.matches("https://example.com/anything") is True
        assert extractor.matches("http://other.org/post") is True

    def test_matches_false_for_ftp_and_plain_text(self, make_article_fetch):
        extractor = _extractor(make_article_fetch())

        assert extractor.matches("ftp://example.com/file.zip") is False
        assert extractor.matches("просто текст") is False
        assert extractor.matches("") is False

    def test_matches_is_derived_from_find(self, make_article_fetch):
        extractor = _extractor(make_article_fetch())
        for text in (
            "https://example.com/a",
            "ftp://example.com/f",
            "https://youtu.be/abcdefghijk",
            "",
            "ссылка https://example.com/b в тексте",
        ):
            assert extractor.matches(text) == (extractor.find(text) is not None), repr(text)

    def test_find_match_implies_canonical_id_not_none(self, make_article_fetch):
        # инвариант порта (дедуп и кеш-ключ строятся по canonical_id)
        extractor = _extractor(make_article_fetch())
        for url in ("https://example.com/a", "http://other.org/b?c=d"):
            assert extractor.find(url) is not None
            assert extractor.canonical_id(url) is not None


class TestFindTrailingPunctuation:
    """Хвостовая пунктуация предложения срезается с конца ссылки (фикс P2-4)."""

    def test_angle_bracket_trimmed(self, make_article_fetch):
        extractor = _extractor(make_article_fetch())
        text = "смотри <https://example.com/a> и это"

        start, end = extractor.find(text)

        assert text[start:end] == "https://example.com/a"

    def test_sentence_dot_trimmed(self, make_article_fetch):
        extractor = _extractor(make_article_fetch())
        text = "глянь https://example.com/a."

        start, end = extractor.find(text)

        assert text[start:end] == "https://example.com/a"

    def test_quotes_and_comma_trimmed(self, make_article_fetch):
        extractor = _extractor(make_article_fetch())
        text = "почитай «https://example.com/a», потом обсудим"

        start, end = extractor.find(text)

        assert text[start:end] == "https://example.com/a"

    def test_unbalanced_paren_trimmed(self, make_article_fetch):
        # «(см. url)»: открывающая вне ссылки (span начинается с https),
        # хвостовая «)» — пунктуация предложения
        extractor = _extractor(make_article_fetch())
        text = "(см. https://example.com/a) подробнее"

        start, end = extractor.find(text)

        assert text[start:end] == "https://example.com/a"

    def test_balanced_paren_kept(self, make_article_fetch):
        # скобки сбалансированы внутри URL: «(язык)» — часть адреса вики,
        # срезается только точка за ним
        extractor = _extractor(make_article_fetch())
        text = "вики https://ru.wikipedia.org/wiki/X_(язык)."

        start, end = extractor.find(text)

        assert text[start:end] == "https://ru.wikipedia.org/wiki/X_(язык)"

    def test_extra_unbalanced_closer_trimmed_after_balanced(self, make_article_fetch):
        # «url_(x))»: внутренняя пара сохраняется, лишняя закрывающая срезается
        extractor = _extractor(make_article_fetch())
        text = "см https://example.com/a_(x)) тут"

        start, end = extractor.find(text)

        assert text[start:end] == "https://example.com/a_(x)"


class TestExtractOnFixtures:
    """extract на синтетических фикстурах: SourceDocument kind="article"."""

    def test_full_article_builds_document(self, make_article_fetch):
        fetch = make_article_fetch(_fixture_html("article_full.html"))
        extractor = _extractor(fetch)

        doc = extractor.extract(ARTICLE_URL)

        assert doc.kind == "article"
        assert doc.canonical_id == extractor.canonical_id(ARTICLE_URL)
        assert doc.url == ARTICLE_URL
        assert doc.author_ref is None, "автор статьи живёт в doc.data, author_ref не строится"
        assert doc.title == "Синтетическая статья о проверке экстрактора", (
            "title из short_title readability (хвост сайта отрезан) или og:title"
        )
        assert doc.lang == "ru"
        assert doc.data == {"author": "Автор Тестовый", "site": "example.com"}, (
            "author из meta og:article:author, site — host"
        )
        assert fetch.calls and fetch.calls[0][0] == ARTICLE_URL, "страница качается по url"

    def test_full_article_text_keeps_paragraphs_drops_markup(self, make_article_fetch):
        fetch = make_article_fetch(_fixture_html("article_full.html"))
        content = _extractor(fetch).extract(ARTICLE_URL).content

        flat = _norm(content)
        assert "Первый абзац объясняет" in flat, "текст первого абзаца сохранён"
        assert "Второй абзац показывает разметку" in flat
        assert "Третий абзац добавляет объём" in flat
        assert "первый пункт списка" in flat and "второй пункт списка" in flat, "li сохранены"
        assert "на соседнюю страницу" in flat, "текст ссылки сохранён, адрес — нет"
        assert "https://example.com/other" not in flat, "href срезан"
        assert "жирным" in flat and "строки" in flat, "b и br не теряют слова"
        assert "12345" in flat
        assert "&" in flat and "&amp;" not in flat, "html-сущности раскрываются"
        assert "<p" not in content and "<h2" not in content and "<a " not in content, (
            "теги срезаны"
        )
        assert "\n\n" in content, "абзацы разделены пустой строкой"
        assert "\n\n\n" not in content, "3+ перевода строки схлопнуты до двух"
        assert "[" not in content, "в тексте статьи нет таймкод-разметки"

    def test_min_article_passes_threshold(self, make_article_fetch):
        # ~600 знаков текста — валидная статья на грани порога в 500
        fetch = make_article_fetch(_fixture_html("article_min.html"))

        doc = _extractor(fetch).extract(ARTICLE_URL)

        assert doc.kind == "article"
        assert len(doc.content) >= 500, "статья на грани проходит порог длины"
        assert "Короткая заметка" in _norm(doc.content)

    def test_junk_page_is_not_an_article(self, make_article_fetch):
        from bot.sources.base import NotAnArticle

        fetch = make_article_fetch(_fixture_html("junk_page.html"))

        with pytest.raises(NotAnArticle):
            _extractor(fetch).extract(ARTICLE_URL)


class TestExtractFailures:
    """Отказы extract: сеть — RuntimeError, не-статья — NotAnArticle."""

    @pytest.mark.parametrize("status", [301, 404, 500])
    def test_non_2xx_status_is_runtime_error(self, make_article_fetch, status):
        fetch = make_article_fetch(_fixture_html("article_full.html"), status=status)

        with pytest.raises(RuntimeError) as excinfo:
            _extractor(fetch).extract(ARTICLE_URL)

        assert "страница недоступна" in str(excinfo.value)
        assert str(status) in str(excinfo.value), "код статуса в причине для пользователя"

    def test_fetch_none_is_runtime_error(self, make_article_fetch):
        # сеть легла целиком (fetch вернул None) — обычный сетевой сбой
        fetch = make_article_fetch(returns_none=True)

        with pytest.raises(RuntimeError) as excinfo:
            _extractor(fetch).extract(ARTICLE_URL)

        assert "страница недоступна" in str(excinfo.value)

    def test_pdf_content_type_is_not_an_article(self, make_article_fetch):
        from bot.sources.base import NotAnArticle

        fetch = make_article_fetch(
            _fixture_html("article_full.html"), headers={"content-type": "application/pdf"}
        )

        with pytest.raises(NotAnArticle):
            _extractor(fetch).extract(ARTICLE_URL)

    def test_missing_content_type_is_not_an_article(self, make_article_fetch):
        # headers.get("content-type", "") без ключа — тоже «не text/html»
        from bot.sources.base import NotAnArticle

        fetch = make_article_fetch(_fixture_html("article_full.html"), headers={})

        with pytest.raises(NotAnArticle):
            _extractor(fetch).extract(ARTICLE_URL)

    def test_fetch_exception_propagates_as_is(self, make_article_fetch):
        # сбой внутри fetch — обычное исключение наружу (error-путь пайплайна),
        # а не NotAnArticle: «сеть упала» не равна «статьи нет»
        fetch = make_article_fetch(exc=ConnectionError("сеть недоступна"))

        with pytest.raises(ConnectionError):
            _extractor(fetch).extract(ARTICLE_URL)


class TestContentTypeGate:
    """Гейт content-type (фикс P3-1): html, xhtml и text/plain разбираются."""

    def test_xhtml_is_an_article(self, make_article_fetch):
        # application/xhtml+xml — валидная страница для readability
        fetch = make_article_fetch(
            _fixture_html("article_full.html"),
            headers={"content-type": "application/xhtml+xml; charset=utf-8"},
        )

        doc = _extractor(fetch).extract(ARTICLE_URL)

        assert doc.kind == "article"
        assert "Первый абзац объясняет" in _norm(doc.content)

    def test_text_plain_body_is_content_verbatim(self, make_article_fetch):
        # text/plain: readability не нужен — весь body и есть текст, title
        # берётся из хвоста URL, меты и lang у файла нет
        fetch = make_article_fetch(LONG_BODY, headers={"content-type": "text/plain; charset=utf-8"})

        doc = _extractor(fetch).extract("https://example.com/files/note.txt")

        assert doc.kind == "article"
        assert doc.content == LONG_BODY, "текст отдаётся как есть, без срезания тегов"
        assert doc.title == "note.txt"
        assert doc.lang == ""
        assert doc.data == {"author": "", "site": "example.com"}

    def test_short_text_plain_is_not_an_article(self, make_article_fetch):
        from bot.sources.base import NotAnArticle

        fetch = make_article_fetch("короткая заметка\n", headers={"content-type": "text/plain"})

        with pytest.raises(NotAnArticle):
            _extractor(fetch).extract(ARTICLE_URL)

    def test_json_content_type_is_not_an_article(self, make_article_fetch):
        from bot.sources.base import NotAnArticle

        fetch = make_article_fetch("{}", headers={"content-type": "application/json"})

        with pytest.raises(NotAnArticle):
            _extractor(fetch).extract(ARTICLE_URL)


class TestMetadataParsing:
    """Разбор меты на inline-страницах: приоритеты и дефолты полей документа."""

    def test_title_falls_back_to_og_title(self, make_article_fetch):
        # <title> нет — заголовок берётся из og:title
        fetch = make_article_fetch(
            _page(metas='<meta property="og:title" content="Заголовок из og">')
        )

        doc = _extractor(fetch).extract(ARTICLE_URL)

        assert doc.title == "Заголовок из og"

    def test_author_from_og_article_author(self, make_article_fetch):
        fetch = make_article_fetch(_page(metas='<meta property="og:article:author" content="Ог Автор">'))

        assert _extractor(fetch).extract(ARTICLE_URL).data["author"] == "Ог Автор"

    def test_author_fallback_to_article_author_meta(self, make_article_fetch):
        fetch = make_article_fetch(_page(metas='<meta property="article:author" content="Артикул Автор">'))

        assert _extractor(fetch).extract(ARTICLE_URL).data["author"] == "Артикул Автор"

    def test_author_fallback_to_name_meta(self, make_article_fetch):
        fetch = make_article_fetch(_page(metas='<meta name="author" content="Просто Автор">'))

        assert _extractor(fetch).extract(ARTICLE_URL).data["author"] == "Просто Автор"

    def test_author_priority_first_present_wins(self, make_article_fetch):
        # цепочка og:article:author | article:author | name="author": первый найденный
        metas = (
            '<meta name="author" content="Третий">'
            '<meta property="article:author" content="Второй">'
            '<meta property="og:article:author" content="Первый">'
        )
        fetch = make_article_fetch(_page(metas=metas))

        assert _extractor(fetch).extract(ARTICLE_URL).data["author"] == "Первый"

    def test_author_absent_is_empty_string(self, make_article_fetch):
        fetch = make_article_fetch(_page())

        doc = _extractor(fetch).extract(ARTICLE_URL)

        assert doc.data["author"] == ""
        assert doc.data["site"] == "example.com"

    def test_site_is_host_of_url(self, make_article_fetch):
        fetch = make_article_fetch(_page())

        doc = _extractor(fetch).extract("https://news.example.org/post/42")

        assert doc.data["site"] == "news.example.org"

    def test_lang_from_html_tag(self, make_article_fetch):
        fetch = make_article_fetch(_page(lang="en"))

        assert _extractor(fetch).extract(ARTICLE_URL).lang == "en"

    def test_lang_absent_is_empty_string(self, make_article_fetch):
        fetch = make_article_fetch(_page().replace(' lang="ru"', "", 1))

        assert _extractor(fetch).extract(ARTICLE_URL).lang == ""


class TestExtractorBasics:
    def test_name_is_article(self, make_article_fetch):
        assert _extractor(make_article_fetch()).name == "article"

    def test_conforms_to_extractor_port(self, make_article_fetch):
        from bot.sources.article import ArticleExtractor
        from bot.sources.base import ExtractorPort

        assert isinstance(ArticleExtractor(), ExtractorPort)
        assert isinstance(ArticleExtractor(fetch=make_article_fetch()), ExtractorPort)

    def test_constructs_without_injection_and_network(self):
        # дефолт — requests-обёртка; создаётся без сети, вызовов не делаем
        from bot.sources.article import ArticleExtractor

        extractor = ArticleExtractor()
        assert extractor.name == "article"
        assert callable(extractor.extract)


class _StreamResp:
    """Фейк streaming-ответа requests для тестов дефолтного fetch.

    Форма повторяет настоящий requests.Response при stream=True: url (после
    редиректов), заголовки, кодировка, iter_content и контекст-менеджер.
    """

    def __init__(self, *, url, chunks=b"", headers=None, encoding="utf-8", status_code=200):
        self.url = url
        self.status_code = status_code
        self.headers = headers if headers is not None else {}
        self.encoding = encoding
        self._chunks = chunks if isinstance(chunks, list) else [chunks]
        self.iter_calls = 0
        self.closed = False

    def iter_content(self, chunk_size):
        self.iter_calls += 1
        yield from self._chunks

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()
        return False


def _patch_default_network(monkeypatch, *, ips=None, resp=None, dns_error=None):
    """Подменяет socket.getaddrinfo и requests.get (сети нет); возвращает вызовы.

    ips — host -> ip (хоста нет в словаре — публичный 93.184.216.34);
    dns_error — исключение из резолвера; resp — ответ requests.get.
    """
    import socket

    import bot.sources.article as article_mod

    calls = {"get": [], "addr": []}

    def _fake_getaddrinfo(host, *args, **kwargs):
        calls["addr"].append(host)
        if dns_error is not None:
            raise dns_error
        ip = (ips or {}).get(host, "93.184.216.34")
        if ":" in ip:
            return [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", (ip, 0, 0, 0))]
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0))]

    def _fake_get(url, *args, **kwargs):
        calls["get"].append(url)
        if resp is None:
            raise AssertionError("тест не заготовил ответ requests.get")
        return resp

    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo)
    monkeypatch.setattr(article_mod.requests, "get", _fake_get)
    return calls


class TestDefaultFetch:
    def test_default_fetch_is_requests_with_timeout_and_browser_ua(self, monkeypatch):
        # контракт дефолтного fetch: requests (stream=True — тело качается с
        # лимитом), timeout=15, браузерный User-Agent. Сети нет: getaddrinfo и
        # requests.get подменены; SSRF-гвард пропускает публичный example.com
        import socket

        import bot.sources.article as article_mod

        def _fake_getaddrinfo(host, *args, **kwargs):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]

        monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo)

        captured = {}
        body = _fixture_html("article_min.html").encode("utf-8")

        class _Resp:
            status_code = 200
            url = ARTICLE_URL
            encoding = "utf-8"
            headers = {"content-type": "text/html; charset=utf-8"}

            def iter_content(self, chunk_size):
                yield body

            def __enter__(self):
                return self

            def __exit__(self, *exc_info):
                return False

        def _fake_get(url, *args, **kwargs):
            captured["url"] = url
            captured["kwargs"] = kwargs
            return _Resp()

        monkeypatch.setattr(article_mod.requests, "get", _fake_get)

        doc = article_mod.ArticleExtractor().extract(ARTICLE_URL)

        assert doc.kind == "article"
        assert captured["url"] == ARTICLE_URL
        assert captured["kwargs"].get("timeout") == 15, "дефолтный таймаут — 15 секунд"
        assert captured["kwargs"].get("stream") is True, "тело качается потоком (лимит размера)"
        headers = {str(k).lower(): str(v) for k, v in (captured["kwargs"].get("headers") or {}).items()}
        assert "mozilla" in headers.get("user-agent", "").lower(), (
            "браузерный User-Agent в дефолтном fetch"
        )


class TestDefaultFetchSsrfGuard:
    """SSRF-гвард дефолтного fetch (фикс P2-2): URL из чата, внутренняя сеть закрыта.

    До GET отклоняются localhost/.local/.internal (по имени) и хосты, чьи IP
    loopback/приватные/link-local/reserved/multicast/unspecified; после GET —
    финальный resp.url (редирект на приватный хост не проходит). DNS-rebinding
    осознанно принят (см. _assert_public_url в bot/sources/article.py).
    """

    _PRIVATE_IPS = {
        "127.0.0.1": "127.0.0.1",
        "10.1.2.3": "10.1.2.3",
        "169.254.169.254": "169.254.169.254",
        "::1": "::1",
    }

    @pytest.mark.parametrize(
        "url",
        [
            "https://localhost/x",
            "http://mybox.local/x",
            "https://svc.internal/x",
            "https://127.0.0.1/x",
            "https://10.1.2.3/x",
            "https://169.254.169.254/latest/meta-data/",
            "https://[::1]/x",
        ],
    )
    def test_internal_addresses_rejected_before_get(self, monkeypatch, url):
        import bot.sources.article as article_mod

        calls = _patch_default_network(monkeypatch, ips=self._PRIVATE_IPS)

        with pytest.raises(RuntimeError) as excinfo:
            article_mod.fetch(url)

        assert "внутренние адреса" in str(excinfo.value)
        assert calls["get"] == [], "requests.get до проверки гвардом не вызывается"

    def test_public_host_is_fetched(self, monkeypatch):
        import bot.sources.article as article_mod

        resp = _StreamResp(url="https://example.com/x", chunks="страница".encode())
        calls = _patch_default_network(monkeypatch, resp=resp)

        fetched = article_mod.fetch("https://example.com/x")

        assert fetched is not None
        status, headers, text = fetched
        assert status == 200
        assert text == "страница"
        assert calls["get"] == ["https://example.com/x"]
        # гвард резолвит дважды: до GET и после него (финальный resp.url)
        assert calls["addr"] == ["example.com", "example.com"]

    def test_redirect_to_private_host_rejected(self, monkeypatch):
        # allow_redirects у requests дефолтен: финальный resp.url проверяется
        # тем же гвардом — редирект в приватную сеть отклоняется
        import bot.sources.article as article_mod

        resp = _StreamResp(url="http://127.0.0.1/admin", chunks=b"secret")
        calls = _patch_default_network(monkeypatch, ips={"127.0.0.1": "127.0.0.1"}, resp=resp)

        with pytest.raises(RuntimeError) as excinfo:
            article_mod.fetch("https://example.com/redirect")

        assert "внутренние адреса" in str(excinfo.value)
        assert calls["get"], "GET происходит (редирект follow'ит requests)"
        assert resp.closed, "ответ закрыт — соединение не течёт"

    def test_dns_failure_is_runtime_error(self, monkeypatch):
        # сбой резолвера — RuntimeError наружу (гвард не пропускает непроверенное)
        import socket

        import bot.sources.article as article_mod

        calls = _patch_default_network(
            monkeypatch, dns_error=socket.gaierror("name resolve failed")
        )

        with pytest.raises(RuntimeError) as excinfo:
            article_mod.fetch("https://no-such-host.invalid/x")

        assert "не удалось проверить адрес" in str(excinfo.value)
        assert calls["get"] == []


class TestDefaultFetchSizeLimit:
    """Лимит размера тела (10 МБ, фикс P2-3): дампы не качаются и не декодируются."""

    def test_big_content_length_rejected_before_body_read(self, monkeypatch):
        import bot.sources.article as article_mod

        resp = _StreamResp(
            url="https://example.com/dump",
            headers={"content-length": str(20 * 1024 * 1024)},
        )
        _patch_default_network(monkeypatch, resp=resp)

        with pytest.raises(RuntimeError) as excinfo:
            article_mod.fetch("https://example.com/dump")

        assert "страница слишком большая" in str(excinfo.value)
        assert resp.iter_calls == 0, "тело не читается: лимит проверен по Content-Length"
        assert resp.closed

    def test_stream_over_limit_rejected_midway(self, monkeypatch):
        # Content-Length отсутствует/лжёт: лимит дублируется по факту чтения
        import bot.sources.article as article_mod

        chunks = [b"x" * (10 * 1024 * 1024), b"y" * 128]
        resp = _StreamResp(url="https://example.com/dump", chunks=chunks)
        _patch_default_network(monkeypatch, resp=resp)

        with pytest.raises(RuntimeError) as excinfo:
            article_mod.fetch("https://example.com/dump")

        assert "страница слишком большая" in str(excinfo.value)
        assert resp.closed


def test_not_an_article_is_exception_in_base():
    # контракт фазы 5: «по ссылке нет статьи» — отдельный класс в базовом модуле,
    # отличимый от сетевых сбоев (обычных исключений) extract-семантики
    from bot.sources.base import NotAnArticle

    assert issubclass(NotAnArticle, Exception)
