"""Юнит-тесты провайдера комментариев: парсинг ответов YouTube, сортировка
по лайкам, отказная стойкость. Сеть — за инжектируемым fetch (фейк)."""

import json

import pytest

from bot.analysis.comments import (
    YouTubeCommentsProvider,
    _comment_token,
    _parse_comments,
    parse_like_count,
)

VIDEO_ID = "abcdefghijk"
URL = f"https://youtu.be/{VIDEO_ID}"

# Синтетическая watch-страница: ключ innertube + ytInitialData с секцией комментариев
_PAGE = (
    '<html><script>var ytInitialData = '
    + json.dumps(
        {
            "contents": {
                "itemSectionRenderer": [
                    {"sectionIdentifier": "comment-item-section"},
                    {
                        "sectionIdentifier": "comment-item-section",
                        "contents": [
                            {
                                "continuationItemRenderer": {
                                    "continuationEndpoint": {
                                        "continuationCommand": {"token": "TOKEN123", "request": "x"}
                                    }
                                }
                            }
                        ],
                    },
                ]
            }
        }
    )
    + ";</script>"
    + '"INNERTUBE_API_KEY":"test-key-123"'
)

# Ответ next: новый формат commentEntityPayload
_NEXT_NEW = json.dumps(
    {
        "frameworkUpdates": {
            "entityBatchUpdate": {
                "mutations": [
                    {
                        "payload": {
                            "commentEntityPayload": {
                                "properties": {"content": {"content": "топовый комментарий"}},
                                "toolbar": {"likeCountNotliked": "1.2K"},
                            }
                        }
                    },
                    {
                        "payload": {
                            "commentEntityPayload": {
                                "properties": {"content": {"content": "мало лайков"}},
                                "toolbar": {"likeCountNotliked": "5"},
                            }
                        }
                    },
                ]
            }
        }
    }
)

# Ответ next: старый формат commentRenderer (с дубликатом текста)
_NEXT_OLD = json.dumps(
    {
        "onResponseReceivedEndpoints": [
            {
                "appendContinuationItemsAction": {
                    "continuationItems": [
                        {
                            "commentRenderer": {
                                "contentText": {"runs": [{"text": "старый "}, {"text": "формат"}]},
                                "voteCount": {"simpleText": "2,5 тыс."},
                            }
                        },
                        {
                            "commentRenderer": {
                                "contentText": {"runs": [{"text": "старый "}, {"text": "формат"}]},
                                "voteCount": {"simpleText": "999"},
                            }
                        },
                    ]
                }
            }
        ]
    }
)


class _FakeFetch:
    """fetch(url, data) -> (status, text) | None: watch-страница и POST-ответы."""

    def __init__(self, page_status=200, page_text=_PAGE, post_status=200, post_text=_NEXT_NEW,
                 page_exc=None, post_exc=None):
        self.page_status, self.page_text = page_status, page_text
        self.post_status, self.post_text = post_status, post_text
        self.page_exc, self.post_exc = page_exc, post_exc
        self.calls = []

    def __call__(self, url, data=None):
        self.calls.append((url, data))
        try:
            if data is None:
                if self.page_exc is not None:
                    raise self.page_exc
                return self.page_status, self.page_text
            if self.post_exc is not None:
                raise self.post_exc
            return self.post_status, self.post_text
        except Exception:
            return None


class TestParseLikeCount:
    def test_numbers_and_multipliers(self):
        assert parse_like_count("42") == 42
        assert parse_like_count("1.2K") == 1200
        assert parse_like_count("2M") == 2_000_000
        assert parse_like_count("1,2 тыс.") == 1200
        assert parse_like_count("1,53 млн") == 1_530_000

    def test_garbage_is_zero(self):
        assert parse_like_count("") == 0
        assert parse_like_count("—") == 0
        assert parse_like_count(None) == 0


class TestParseComments:
    def test_new_format_sorted_by_likes(self):
        comments = _parse_comments(_NEXT_NEW)
        assert comments == [("топовый комментарий", 1200), ("мало лайков", 5)]

    def test_old_format_deduplicated(self):
        comments = _parse_comments(_NEXT_OLD)
        # дубликат текста схлопнут, остался максимум лайков из виденных
        assert len(comments) == 1
        text, likes = comments[0]
        assert text == "старый формат"
        assert likes in (2500, 999)


class TestCommentToken:
    def test_token_found_in_comment_section(self):
        assert _comment_token(json.loads(_PAGE.split("var ytInitialData = ")[1].rsplit(";</script>", 1)[0])) == "TOKEN123"

    def test_token_none_without_section(self):
        assert _comment_token({"contents": []}) is None


class TestProviderGet:
    def test_happy_path_top_comments_formatted(self):
        fake = _FakeFetch()
        provider = YouTubeCommentsProvider(fetch=fake)
        comments = provider.get(URL)
        assert comments == [
            "топовый комментарий (👍 1200)",
            "мало лайков (👍 5)",
        ]
        # два запроса: watch-страница (GET) и next (POST с continuation)
        page_url, page_data = fake.calls[0]
        assert "watch?v=abcdefghijk" in page_url and page_data is None
        post_url, post_data = fake.calls[1]
        assert "youtubei/v1/next" in post_url and "test-key-123" in post_url
        body = json.loads(post_data)
        assert body["continuation"] == "TOKEN123"
        assert provider.calls == [URL]

    def test_not_youtube_url_none(self):
        fake = _FakeFetch()
        provider = YouTubeCommentsProvider(fetch=fake)
        assert provider.get("https://example.com/news") is None
        assert fake.calls == [], "без сети для не-YouTube ссылок"

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"page_status": 404},
            {"page_exc": RuntimeError("сеть")},
            {"post_status": 500},
            {"post_exc": RuntimeError("сеть")},
            {"post_text": "не json"},
            {"post_text": json.dumps({"empty": True})},
        ],
    )
    def test_any_failure_is_none(self, kwargs):
        provider = YouTubeCommentsProvider(fetch=_FakeFetch(**kwargs))
        assert provider.get(URL) is None

    def test_no_comments_section_none(self):
        page = '"INNERTUBE_API_KEY":"k"' + "<html>без комментариев</html>"
        provider = YouTubeCommentsProvider(fetch=_FakeFetch(page_text=page))
        assert provider.get(URL) is None
