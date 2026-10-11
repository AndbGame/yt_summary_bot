"""Факты об авторе YouTube-канала: oEmbed + страница канала, без LLM.

AuthorRef — стабильный ключ автора (platform, handle), AuthorFacts — что о нём
известно (ARCHITECTURE.md §5). Провайдер разделяет два шага (фаза 4):
get_author_ref(url) даёт ref по ОДНОМУ oEmbed-запросу к видео (handle —
последний сегмент пути author_url), факты канала собирает отдельно
get_channel(ref): страница канала качается только когда кеш автора не
спасает — пайплайн сначала смотрит в кеш авторов, и лишь на устаревшей или
отсутствующей строке зовёт get_channel.
Обзор по фактам делает LLM (bot/analysis/llm.py), а не провайдер.

Отказная стойкость: ЛЮБОЙ сбой сети/парса — None, метод не бросает. Неполные
факты (есть имя, нет описания) в get_channel тоже считаются сбоем: обзор по
половине фактов хуже отсутствия блока. Сеть изолирована за fetch(url, timeout)
-> (status, text) | None — в тестах фейк, в проде requests.
"""

import html
import json
import logging
import re
from dataclasses import dataclass, field
from urllib.parse import urlencode, urlparse

import requests

log = logging.getLogger("yt-summary")

_TIMEOUT = 5  # секунды на один HTTP-запрос; в env-настройку не выносится

# UA обычного браузера: YouTube охотнее отдаёт полную страницу без согласия на cookies
_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ru,en;q=0.8",
}

_OEMBED_URL = "https://www.youtube.com/oembed"
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_META_TAG_RE = re.compile(r"<meta\b[^>]*>", re.IGNORECASE)
_OG_PROPERTY_RE = re.compile(r"""property\s*=\s*["']og:description["']""", re.IGNORECASE)
_CONTENT_RE = re.compile(r"""content\s*=\s*["'](.*?)["']""", re.IGNORECASE | re.DOTALL)
_YOUTUBE_SUFFIX = " - YouTube"


def fetch(url: str, timeout: float = _TIMEOUT) -> tuple[int, str] | None:
    """Дефолтный fetch: (status_code, text) или None при любой сетевой ошибке.

    Инжектируется в YouTubeAuthorFactsProvider (в тестах — фейк), поэтому держит
    минимальный контракт и исключения наружу не пробрасывает.
    """
    try:
        resp = requests.get(url, timeout=timeout, headers=_BROWSER_HEADERS)
    except requests.RequestException as e:
        log.warning("fetch %s упал: %s", url, e)
        return None
    return resp.status_code, resp.text


# Алиас нужен, потому что параметр __init__ провайдера называется fetch и
# затеняет функцию внутри своего scope
_DEFAULT_FETCH = fetch


@dataclass
class AuthorRef:
    """Стабильный ключ автора; для youtube handle — UC-id или @-имя канала."""

    platform: str
    handle: str


@dataclass
class AuthorFacts:
    """Факты об авторе; extra — разнородные дополнения (подписчики и т.п.)."""

    name: str = ""
    url: str = ""
    description: str = ""
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return facts_to_dict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "AuthorFacts":
        data = data or {}
        return cls(
            name=str(data.get("name", "") or ""),
            url=str(data.get("url", "") or ""),
            description=str(data.get("description", "") or ""),
            extra=dict(data.get("extra") or {}),
        )


def facts_to_dict(facts) -> dict:
    """Факты (настоящие AuthorFacts или duck-typed фейки тестов) в plain dict.

    Пайплайн и фоновый воркер не знают конкретный класс фактов: провайдер может
    вернуть любую реализацию с атрибутами name/url/description/extra.
    """
    return {
        "name": getattr(facts, "name", "") or "",
        "url": getattr(facts, "url", "") or "",
        "description": getattr(facts, "description", "") or "",
        "extra": dict(getattr(facts, "extra", None) or {}),
    }


class YouTubeAuthorFactsProvider:
    """Собирает факты автора YouTube-канала; сбои сети/парса молча дают None."""

    def __init__(self, fetch=None):
        # подмена на уровне инстанса, глобальное состояние не трогаем
        self._fetch = fetch or _DEFAULT_FETCH

    def get_author_ref(self, url: str) -> AuthorRef | None:
        """AuthorRef по ссылке на контент: один oEmbed-запрос; сбой — None.

        MISS-путь пайплайна (фаза 4): ref добывается БЕЗ страницы канала —
        единственный запрос это oEmbed по url; oEmbed-ответ без author_url,
        как и любой сбой сети/парса, даёт None (метод не бросает). Факты —
        отдельный шаг через get_channel(ref).

        Фаза 5: не-YouTube ссылка (статья и т.п.) даёт None СРАЗУ, без oEmbed —
        у статьи нет канала YouTube, её автор живёт в doc.data["author"] и в
        обзорах авторов не участвует.
        """
        from bot.sources.youtube import YT_RE  # лениво: цикл импортов author->youtube->base

        if not YT_RE.search(url or ""):
            return None
        try:
            return self._get_author_ref(url)
        except Exception:
            log.warning("ref автора для %s не получен", url, exc_info=True)
            return None

    def get_channel(self, ref: AuthorRef) -> AuthorFacts | None:
        """Факты по каналу (MISS-путь пайплайна и фоновое обновление); сбой — None.

        Страница канала качается только когда кеш автора не спасает: пайплайн
        зовёт этот шаг лишь на устаревшей или отсутствующей строке кеша (а
        фоновый воркер — на задачах обновления). Терпимый режим: имя
        обязательно (без него обзор не о ком писать), описание опционально
        (пустая строка) — обновление не должно пропадать из-за временно
        пустого og:description, строка кеша всё равно перезапишется целиком.
        """
        try:
            return self._get_channel(ref)
        except Exception:
            log.warning(
                "факты канала %s/%s не получены",
                getattr(ref, "platform", "?"),
                getattr(ref, "handle", "?"),
                exc_info=True,
            )
            return None

    def _get_author_ref(self, url: str) -> AuthorRef | None:
        fetched = self._fetch(
            f"{_OEMBED_URL}?{urlencode({'url': url, 'format': 'json'})}",
            _TIMEOUT,
        )
        if fetched is None:
            return None
        status, text = fetched
        if not 200 <= status < 300:
            return None
        try:
            data = json.loads(text)
        except (TypeError, json.JSONDecodeError):
            return None
        if not isinstance(data, dict):
            return None
        author_url = data.get("author_url")
        if not author_url or not isinstance(author_url, str):
            return None
        handle = _handle_from_url(author_url)
        if not handle:
            return None
        return AuthorRef(platform="youtube", handle=handle)

    def _get_channel(self, ref: AuthorRef) -> AuthorFacts | None:
        if getattr(ref, "platform", "") != "youtube":
            return None
        handle = getattr(ref, "handle", "")
        if not handle:
            return None
        # UC-идентификатор живёт по /channel/, @-хэндл — прямо в корне домена
        url = (
            f"https://www.youtube.com/channel/{handle}"
            if not handle.startswith("@")
            else f"https://www.youtube.com/{handle}"
        )
        page = self._page(url)
        if page is None:
            return None
        name = _title_name(page)
        if name is None:
            return None
        return AuthorFacts(name=name, url=url, description=_og_description(page) or "")

    def _page(self, url: str) -> str | None:
        """HTML страницы канала: только 2xx считаются успехом, остальное — None."""
        fetched = self._fetch(url, _TIMEOUT)
        if fetched is None:
            return None
        status, text = fetched
        if not 200 <= status < 300:
            return None
        return text


def _handle_from_url(url: str) -> str:
    """Последний сегмент пути: …/channel/UCxxx -> UCxxx, …/@name -> @name."""
    segments = [s for s in urlparse(url).path.split("/") if s]
    return segments[-1] if segments else ""


def _title_name(page: str) -> str | None:
    """Имя канала из <title> до суффикса « - YouTube»; суффикса нет — целиком."""
    match = _TITLE_RE.search(page)
    if not match:
        return None
    title = html.unescape(match.group(1)).strip()
    if not title:
        return None
    if title.endswith(_YOUTUBE_SUFFIX):
        title = title[: -len(_YOUTUBE_SUFFIX)].strip()
    return title or None


def _og_description(page: str) -> str | None:
    """og:description страницы; порядок атрибутов в <meta> не важен."""
    for tag in _META_TAG_RE.findall(page):
        if not _OG_PROPERTY_RE.search(tag):
            continue
        match = _CONTENT_RE.search(tag)
        if match:
            description = html.unescape(match.group(1)).strip()
            if description:
                return description
    return None
