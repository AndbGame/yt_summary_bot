"""Роутер источников (фаза 4): «текст со ссылкой -> экстрактор» (ARCHITECTURE.md §6).

Реестр экстракторов упорядочен по специфичности: route() отдаёт первый, чей
find(text) нашёл совпадение, — порядок реестра = приоритет. Route несёт
границы совпадения, чтобы handle() вырезал URL из текста (имя треда) и дедуп
работал по canonical_id, не завися от формы ссылки в сообщении.
"""

from dataclasses import dataclass

from bot.sources.base import ExtractorPort


@dataclass
class Route:
    """Совпадение: экстрактор, совпавшая подстрока и её границы в исходном тексте."""

    extractor: ExtractorPort
    url: str
    start: int
    end: int


class Router:
    """Реестр экстракторов: route(text) — кто обработает, by_name(name) — по kind."""

    def __init__(self, extractors):
        self.extractors = list(extractors)

    def route(self, text: str) -> Route | None:
        """Первый экстрактор, чей find сработал; ничего не нашлось — None."""
        for extractor in self.extractors:
            span = extractor.find(text)
            if span is not None:
                start, end = span
                return Route(extractor, text[start:end], start, end)
        return None

    def by_name(self, name: str) -> ExtractorPort | None:
        """Экстрактор по имени (джобы ссылаются на экстракторы по kind == name)."""
        return next((e for e in self.extractors if e.name == name), None)
