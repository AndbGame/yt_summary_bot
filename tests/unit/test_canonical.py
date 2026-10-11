"""Канонический идентификатор источника — первая часть ключа кеша отчётов (фаза 1)."""

from bot.sources.youtube import canonical_video_id, extract_video_id


def test_canonical_video_id_format():
    assert canonical_video_id("dQw4w9WgXcQ") == "youtube:video:dQw4w9WgXcQ"


def test_canonical_video_id_deterministic():
    # чистая функция: одинаковый вход — одинаковый ключ кеша
    assert canonical_video_id("abc123def45") == canonical_video_id("abc123def45")


def test_canonical_id_from_parsed_link():
    # связка с парсером: сообщение -> video_id -> канонический ключ кеша
    text = "глянь https://youtu.be/dQw4w9WgXcQ"
    video_id = extract_video_id(text)[0]
    assert canonical_video_id(video_id) == "youtube:video:dQw4w9WgXcQ"
