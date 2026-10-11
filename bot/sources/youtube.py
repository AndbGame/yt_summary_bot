"""YouTube-источник (фаза 4): экстрактор за портом + скачивание субтитров.

YoutubeExtractor — первая реализация ExtractorPort: find/matches/canonical_id
чистые (сеть не трогают), extract добывает субтитры через инжектируемый
fetch_transcript (в тестах — фейк). Метаданных (oEmbed/title) extract не
добывает — ref автора ушел провайдеру фактов (bot/analysis/author.py), чтобы
сети в пайплайне был ровно один поход — за транскриптом.
"""

import logging
import re

import youtube_transcript_api

from bot.sources.base import SourceDocument

log = logging.getLogger("yt-summary")

YT_RE = re.compile(
    r"(?:https?://)?(?:www\.|m\.)?"
    r"(?:youtube\.com/(?:watch\?.*?v=|shorts/|live/|embed/)|youtu\.be/)"
    r"([\w-]{11})"
)


def extract_video_id(text: str):
    """Возвращает (video_id, start, end) или None. start/end — границы совпадения."""
    m = YT_RE.search(text or "")
    if not m:
        return None
    return m.group(1), m.start(), m.end()


def canonical_video_id(video_id: str) -> str:
    """Канонический идентификатор источника — первая часть ключа кеша отчётов.

    Формат youtube:video:<id> (ARCHITECTURE.md §7): позже рядом появятся
    article:<norm-url> и site:<domain>, ключи не пересекутся.
    """
    return f"youtube:video:{video_id}"


def fetch_transcript(video_id: str) -> str:
    """Скачивает субтитры (ru -> en -> любые), возвращает текст с таймкодами."""
    api = youtube_transcript_api.YouTubeTranscriptApi()
    try:
        tr = api.fetch(video_id, languages=["ru", "en"])
    except Exception:
        tr_list = api.list(video_id)
        codes = [t.language_code for t in tr_list]
        if not codes:
            raise RuntimeError("нет доступных субтитров") from None
        tr = api.fetch(video_id, languages=codes)
    lines = []
    for s in tr:
        m, sec = divmod(int(s.start), 60)
        h, mm = divmod(m, 60)
        stamp = f"{h}:{mm:02d}:{sec:02d}" if h else f"{mm:02d}:{sec:02d}"
        lines.append(f"[{stamp}] {s.text}")
    return "\n".join(lines)


# Алиас нужен, потому что параметр __init__ экстрактора называется fetch_transcript
# и затеняет функцию внутри своего scope (см. bot/analysis/author.py с fetch)
_DEFAULT_FETCH_TRANSCRIPT = fetch_transcript


class YoutubeExtractor:
    """Экстрактор YouTube-видео за портом ExtractorPort (ARCHITECTURE.md §5-§6).

    find — основной метод порта (границы совпадения для Router), matches
    производен от него; canonical_id — «youtube:video:<id>», ключ кеша отчётов.
    Сеть трогает только extract через fetch_transcript (инжектируется в тестах).
    """

    name = "video"

    def __init__(self, fetch_transcript=None):
        self._fetch_transcript = fetch_transcript or _DEFAULT_FETCH_TRANSCRIPT

    def find(self, text: str) -> tuple[int, int] | None:
        """Границы первой YouTube-ссылки в тексте или None."""
        m = YT_RE.search(text or "")
        return (m.start(), m.end()) if m else None

    def matches(self, url: str) -> bool:
        """Производный предикат порта: find(url) нашёл ссылку."""
        return self.find(url) is not None

    def canonical_id(self, url: str) -> str | None:
        """«youtube:video:<id>» по ссылке или None, если ссылка не YouTube."""
        found = extract_video_id(url)
        return canonical_video_id(found[0]) if found else None

    def extract(self, url: str) -> SourceDocument:
        """Субтитры -> SourceDocument; сбой добычи контента — исключение наружу."""
        found = extract_video_id(url)
        if not found:
            raise ValueError(f"не похоже на ссылку YouTube: {url}")
        video_id = found[0]
        transcript = self._fetch_transcript(video_id)
        return SourceDocument(
            canonical_id=canonical_video_id(video_id),
            kind="video",
            url=f"https://youtu.be/{video_id}",
            content=transcript,
        )
