"""Юнит-тесты LLM-слоя: парсинг Retry-After, цикл ретраев summarize(), обрезка субтитров."""

from types import SimpleNamespace

import pytest

import yt_summary_bot as bot


def _exc_with_headers(headers):
    return SimpleNamespace(response=SimpleNamespace(headers=headers))


class TestRetryAfterSeconds:
    def test_retry_after_header(self):
        assert bot._retry_after_seconds(_exc_with_headers({"retry-after": "7"})) == 7.0

    def test_header_case_variants(self):
        assert bot._retry_after_seconds(_exc_with_headers({"Retry-After": "9"})) == 9.0

    def test_unparseable_value_returns_none(self):
        # "0.9s" не парсится float() — должен вернуть None (не упасть)
        assert bot._retry_after_seconds(_exc_with_headers({"x-ratelimit-reset-requests": "0.9s"})) is None

    def test_capped_by_max_backoff(self):
        assert bot._retry_after_seconds(_exc_with_headers({"retry-after": "9999"})) == bot.LLM_MAX_BACKOFF

    def test_body_error_payload(self):
        e = SimpleNamespace(response=None, body={"error": {"retry_after": 12}})
        assert bot._retry_after_seconds(e) == 12.0

    def test_no_retry_info_returns_none(self):
        assert bot._retry_after_seconds(SimpleNamespace()) is None


class _FakeCompletions:
    """Записывает аргументы create() и раздаёт заготовленные исходы по очереди."""

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0
        self.last_kwargs = None

    def create(self, **kwargs):
        self.calls += 1
        self.last_kwargs = kwargs
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _resp(text):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text))])


def _patch_llm(monkeypatch, outcomes):
    comp = _FakeCompletions(outcomes)
    fake_client = SimpleNamespace(chat=SimpleNamespace(completions=comp))
    monkeypatch.setattr(bot, "OpenAI", lambda **kw: fake_client)
    monkeypatch.setattr(bot.time, "sleep", lambda s: None)
    return comp


class TestSummarizeRetries:
    def test_success_after_two_429(self, monkeypatch):
        class Fake429(Exception):
            pass

        comp = _patch_llm(monkeypatch, [Fake429(), Fake429(), _resp("итог")])
        # except-ветка берёт имя из глобалов модуля — подменяем на тестовый класс
        monkeypatch.setattr(bot, "RateLimitError", Fake429)
        waits = []
        monkeypatch.setattr(bot.time, "sleep", lambda s: waits.append(s))
        monkeypatch.setattr(bot.random, "uniform", lambda a, b: 0)  # убрать джиттер

        assert bot.summarize("транскрипт") == "итог"
        assert comp.calls == 3
        assert waits == [pytest.approx(5), pytest.approx(10)]  # экспоненциальная пауза

    def test_empty_content_retried_then_raises(self, monkeypatch):
        comp = _patch_llm(monkeypatch, [_resp("  ")] * bot.LLM_MAX_ATTEMPTS)

        with pytest.raises(RuntimeError, match="пустой ответ"):
            bot.summarize("транскрипт")
        assert comp.calls == bot.LLM_MAX_ATTEMPTS

    def test_permanent_error_not_retried(self, monkeypatch):
        class BadRequest(Exception):
            pass

        comp = _patch_llm(monkeypatch, [BadRequest("max_tokens too large")])

        # 400-е ошибки не входят в ретраебельные ветки — сразу наружу
        with pytest.raises(BadRequest):
            bot.summarize("транскрипт")
        assert comp.calls == 1

    def test_transcript_cut_to_limit(self, monkeypatch):
        comp = _patch_llm(monkeypatch, [_resp("ок")])

        bot.summarize("x" * (bot.TRANSCRIPT_LIMIT + 100))
        user_msg = comp.last_kwargs["messages"][1]["content"]
        assert "x" * bot.TRANSCRIPT_LIMIT in user_msg          # вошёл целиком лимит
        assert "x" * (bot.TRANSCRIPT_LIMIT + 1) not in user_msg  # и ни символом больше
