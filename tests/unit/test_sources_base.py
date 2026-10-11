"""Юнит-тесты порта источников (фаза 4, bot/sources/base.py).

Контракт (docs/ARCHITECTURE.md §5, §11):
- SourceKind = video | article | site — строковые значения (тип — Literal или
  константы — реализация выбирает, тесты пинят ЗНАЧЕНИЯ, не тип);
- SourceDocument — нормализованный результат экстракции: (canonical_id, kind,
  url) обязательны, title/author_ref/content/lang/data с дефолтами; mutable
  default (data) не общий между экземплярами;
- AuthorRef — ОДНА каноническая точка: bot.sources.base ре-экспортирует его из
  bot.analysis.author (identity), документы ссылаются на тот же класс;
- ExtractorPort — runtime_checkable Protocol: name + find (основной метод,
  границы совпадения для Router) + matches (производный) + canonical_id +
  extract; объект без find порту не соответствует.
"""

import typing


def test_source_document_positional_order_and_defaults():
    from bot.sources.base import SourceDocument

    doc = SourceDocument("youtube:video:abc", "video", "https://youtu.be/abc")
    assert doc.canonical_id == "youtube:video:abc"
    assert doc.kind == "video"
    assert doc.url == "https://youtu.be/abc"
    assert doc.title == ""
    assert doc.author_ref is None
    assert doc.content == ""
    assert doc.lang == ""
    assert doc.data == {}


def test_source_document_data_default_not_shared():
    from bot.sources.base import SourceDocument

    doc = SourceDocument("c", "video", "u")
    doc.data["page"] = 2
    assert SourceDocument("c", "video", "u").data == {}, "mutable default не общий объект"


def test_author_ref_is_single_canonical_point():
    from bot.analysis.author import AuthorRef as AuthorRefFromAnalysis
    from bot.sources.base import AuthorRef

    assert AuthorRef is AuthorRefFromAnalysis, "AuthorRef переэкспортируется, а не дублируется"


def test_author_ref_accepted_in_document():
    from bot.sources.base import AuthorRef, SourceDocument

    ref = AuthorRef(platform="youtube", handle="UC123")
    doc = SourceDocument("c", "video", "u", author_ref=ref)
    assert doc.author_ref is ref


def test_source_kind_declares_three_kinds():
    # SourceKind — Literal или строковые константы: пиним значения, не форму
    import bot.sources.base as base

    kind = base.SourceKind
    args = typing.get_args(kind)
    if args:
        assert tuple(args) == ("video", "article", "site")
    else:
        declared = {
            v for v in vars(base).values() if isinstance(v, str) and v in {"video", "article", "site"}
        }
        assert declared == {"video", "article", "site"}


def _duck_with_find():
    class _Duck:
        name = "x"

        def find(self, text):
            return None

        def matches(self, url):
            return False

        def canonical_id(self, url):
            return None

        def extract(self, url):
            raise AssertionError("не вызывается")

        def describe(self, url):
            return None

    return _Duck()


def test_extractor_port_runtime_checkable_and_real_extractor_conforms():
    from bot.sources.base import ExtractorPort
    from bot.sources.youtube import YoutubeExtractor

    assert isinstance(YoutubeExtractor(), ExtractorPort)
    assert isinstance(_duck_with_find(), ExtractorPort)


def test_extractor_port_requires_find():
    # find — основной метод порта (решение контракта фазы 4): matches производен
    # от find, Router строит границы совпадения только по нему
    from bot.sources.base import ExtractorPort

    class _NoFind:
        name = "x"

        def matches(self, url):
            return True

        def canonical_id(self, url):
            return None

        def extract(self, url):
            raise AssertionError("не вызывается")

    assert not isinstance(_NoFind(), ExtractorPort), "объект без find не соответствует порту"


def test_find_match_implies_canonical_id_not_none(make_extractor):
    # инвариант порта (P3-7, дедуп и кеш-ключ строятся по canonical_id):
    # find(url) нашёл ссылку -> canonical_id(url) не None — иначе handle()
    # не сможет записать дедуп и джобу, а пайплайн не получит ключ кеша
    from bot.sources.youtube import YoutubeExtractor

    url = "https://youtu.be/abcdefghijk"
    for extractor in (make_extractor(), YoutubeExtractor()):
        assert extractor.find(url) is not None, f"{extractor.name}: find должен найти ссылку"
        assert extractor.canonical_id(url) is not None, (
            f"{extractor.name}: совпадение find обязано давать canonical_id"
        )
