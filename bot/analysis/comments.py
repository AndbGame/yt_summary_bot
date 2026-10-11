"""Ознакомительный обзор комментариев: топ популярных комментариев видео.

YouTube отдаёт комментарии только через внутренний API: качаем watch-страницу,
извлекаем ytInitialData и INNERTUBE_API_KEY, одним POST youtubei/v1/next по
continuation-токену секции комментариев получаем первую выдачу «Топ
комментариев» (она уже отсортирована по популярности — сортируем ещё раз
у себя по числу лайков на случай смены порядка). Парсинг защитный: известны
два формата ответа (commentEntityPayload и commentRenderer) — берём оба.
Ознакомительный обзор = небольшая выборка (MAX_COMMENTS), не весь пул.

Отказная стойкость как у автора: ЛЮБОЙ сбой (нет токена, комментарии
отключены, сеть, смена формата YouTube) — None, метод не бросает; разбор
видео не должен зависеть от обзора комментариев. Сеть за fetch(url, data=None)
-> (status, text) | None (data — тело POST); в тестах фейк, в проде requests.
"""

import json
import logging
import re
from urllib.parse import urlencode

import requests

from bot.sources.youtube import extract_video_id

log = logging.getLogger("yt-summary")

_TIMEOUT = 10  # watch-страница тяжёлая, POST лёгкий; общий запас

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ru,en;q=0.8",
    "Content-Type": "application/json",
}

_INNERTUBE_KEY_RE = re.compile(r'"INNERTUBE_API_KEY":"([^"]+)"')
_INITIAL_DATA_RE = re.compile(r"var ytInitialData = (\{.+?\});\s*</script>", re.DOTALL)

# Признаки лайков: "1.2K", "42", "1,2 тыс.", "1,53 млн" — число + опциональный множитель
_LIKE_RE = re.compile(r"([\d.,]+)\s*(тыс|млн|K|M|B|млрд)?", re.IGNORECASE)
_MULTIPLIERS = {"k": 1e3, "m": 1e6, "b": 1e9, "тыс": 1e3, "млн": 1e6, "млрд": 1e9}


def _default_fetch(url: str, data=None):
    """(status, text) или None при сетевой ошибке; data — тело POST."""
    try:
        if data is not None:
            resp = requests.post(url, data=data, timeout=_TIMEOUT, headers=_HEADERS)
        else:
            resp = requests.get(url, timeout=_TIMEOUT, headers=_HEADERS)
    except requests.RequestException as e:
        log.warning("запрос комментариев %s упал: %s", url, e)
        return None
    return resp.status_code, resp.text


def parse_like_count(label: str) -> int:
    """«1.2K»/«1,2 тыс.»/«42» -> число лайков; неразбираемое — 0."""
    match = _LIKE_RE.search(label or "")
    if not match:
        return 0
    try:
        value = float(match.group(1).replace(" ", "").replace(",", "."))
    except ValueError:
        return 0
    return int(value * _MULTIPLIERS.get((match.group(2) or "").lower(), 1))


def _walk_dicts(node):
    """Все словари вглубь структуры (JSON YouTube — глубокие и неустойчивые)."""
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk_dicts(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk_dicts(value)


def _comment_token(initial_data: dict) -> str | None:
    """Continuation-токен секции комментариев (itemSectionRenderer с
    sectionIdentifier «comment-item-section» -> continuationItemRenderer)."""
    for node in _walk_dicts(initial_data):
        if node.get("sectionIdentifier") != "comment-item-section":
            continue
        for inner in _walk_dicts(node):
            continuation = inner.get("continuationItemRenderer")
            if not isinstance(continuation, dict):
                continue
            token = (continuation.get("continuationEndpoint") or {}).get("continuationCommand", {}).get("token")
            if token:
                return token
    return None


def _runs_text(runs) -> str:
    return "".join(r.get("text", "") for r in runs or [] if isinstance(r, dict))


def _parse_comments(payload_text: str) -> list[tuple[str, int]]:
    """(текст, лайки) из ответа youtubei/v1/next; известные форматы — оба."""
    try:
        data = json.loads(payload_text)
    except (TypeError, json.JSONDecodeError):
        return []
    comments: list[tuple[str, int]] = []
    seen: set[str] = set()
    for node in _walk_dicts(data):
        entity = node.get("commentEntityPayload")
        if isinstance(entity, dict):
            text = ((entity.get("properties") or {}).get("content") or {}).get("content") or ""
            likes = ((entity.get("toolbar") or {}).get("likeCountNotliked")) or "0"
            _append(comments, seen, text, likes)
            continue
        renderer = node.get("commentRenderer")
        if isinstance(renderer, dict):
            content = renderer.get("contentText") or {}
            text = _runs_text(content.get("runs")) or (content.get("simpleText") or "")
            votes = renderer.get("voteCount") or {}
            likes = votes.get("simpleText") or _runs_text(votes.get("accessibility", {}).get("accessibilityData", {}).get("label", ""))
            _append(comments, seen, text, likes)
    comments.sort(key=lambda pair: pair[1], reverse=True)
    return comments


def _append(comments: list, seen: set, text, likes) -> None:
    text = (text or "").strip()
    if not text or text in seen:
        return
    seen.add(text)
    comments.append((text, parse_like_count(str(likes))))


class YouTubeCommentsProvider:
    """Топ популярных комментариев видео (ознакомительная выборка)."""

    MAX_COMMENTS = 20

    def __init__(self, fetch=None):
        self._fetch = fetch or _default_fetch
        self.calls: list[str] = []

    def get(self, url: str) -> list[str] | None:
        """Список строк «текст (👍 N)» по популярности; сбой — None, не бросает."""
        self.calls.append(url)
        try:
            return self._get(url)
        except Exception:
            log.warning("комментарии %s не получены", url, exc_info=True)
            return None

    def _get(self, url: str) -> list[str] | None:
        found = extract_video_id(url or "")
        if not found:
            return None
        video_id = found[0]

        page = self._fetch(f"https://www.youtube.com/watch?v={video_id}", None)
        if page is None:
            return None
        status, html = page
        if not 200 <= status < 300:
            return None
        key_match = _INNERTUBE_KEY_RE.search(html)
        initial_match = _INITIAL_DATA_RE.search(html)
        if not key_match or not initial_match:
            return None
        try:
            initial_data = json.loads(initial_match.group(1))
        except json.JSONDecodeError:
            return None
        token = _comment_token(initial_data)
        if not token:
            return None

        body = json.dumps(
            {
                "context": {"client": {"clientName": "WEB", "clientVersion": "2.20240304.00.00", "hl": "ru"}},
                "continuation": token,
            }
        )
        response = self._fetch(
            f"https://www.youtube.com/youtubei/v1/next?{urlencode({'key': key_match.group(1)})}", body
        )
        if response is None:
            return None
        status, text = response
        if not 200 <= status < 300:
            return None
        comments = _parse_comments(text)
        if not comments:
            return None
        return [f"{text_} (👍 {likes or '—'})" for text_, likes in comments[: self.MAX_COMMENTS]]
