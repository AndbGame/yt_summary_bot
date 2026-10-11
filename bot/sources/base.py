"""Порт источников (фаза 4): ExtractorPort, SourceDocument, SourceKind.

Каноническая точка доменных типов источников (ARCHITECTURE.md §5, §11):
AuthorRef определён в bot.analysis.author и отсюда только ре-экспортируется
(единственность определения закреплена тестом на identity), SourceDocument —
нормализованный результат экстракции, ExtractorPort — контракт экстрактора.

Контракт порта: find(text) -> (start, end) | None — ОСНОВНОЙ метод (Router
строит по нему границы совпадения), matches производен: matches(url) ==
(find(url) is not None). Protocol runtime_checkable — чтобы фабрики/фейки
тестов проверялись isinstance'ом без наследования.
"""

from dataclasses import dataclass, field
from typing import Literal, Protocol, runtime_checkable

from bot.analysis.author import AuthorRef

SourceKind = Literal["video", "article", "site"]


@dataclass
class SourceDocument:
    """Нормализованный результат экстракции (ARCHITECTURE.md §5).

    (canonical_id, kind, url) — обязательное ядро; content — текст с разметкой
    [MM:SS] для видео; data — необязательные метаданные конкретного kind.
    """

    canonical_id: str
    kind: SourceKind
    url: str
    title: str = ""
    author_ref: AuthorRef | None = None
    content: str = ""
    lang: str = ""
    data: dict = field(default_factory=dict)


@runtime_checkable
class ExtractorPort(Protocol):
    """Экстрактор источника: поиск ссылок в тексте + добыча контента.

    Добавление источника = класс с этим контрактом + строка в реестре Router
    (bot/main.build_deps); ядро пайплайна про конкретные платформы не знает.

    Инвариант (дедуп и кеш-ключ строятся по canonical_id): если find(text)
    вернул совпадение, canonical_id(url) по найденному url обязан быть не None
    — иначе handle() не сможет записать дедуп и джобу, а пайплайн не получит
    ключ кеша отчётов.
    """

    name: str

    def find(self, text: str) -> tuple[int, int] | None:
        """Границы (start, end) первой ссылки в тексте или None."""
        ...

    def matches(self, url: str) -> bool:
        """Производный предикат: url вообще опознаваем этим экстрактором."""
        ...

    def canonical_id(self, url: str) -> str | None:
        """Стабильный ключ кеша вида <platform>:<kind>:<id> или None."""
        ...

    def extract(self, url: str) -> SourceDocument:
        """Добывает контент; сбой добычи — исключение наружу."""
        ...
