"""Распознавание YouTube-ссылок и скачивание субтитров."""

import logging
import re

import youtube_transcript_api

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


def thread_name_from_message(text: str, video_id: str) -> str:
    """Имя треда: текст сообщения без ссылки, либо 'Видео <id>' (монолитный build_thread_name)."""
    text = YT_RE.sub("", text).strip() or f"Видео {video_id}"
    return text[:90]


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
