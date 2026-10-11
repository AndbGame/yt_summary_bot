"""Юнит-тесты LLM-слоя: парсинг Retry-After, цикл ретраев LLMClient.summarize(),
попадание параметров конфига в вызов API.

openai-фейк подменяется на уровне модуля bot.analysis.llm — так же, как
исключения и OpenAI импортированы в самом модуле.
"""

from types import SimpleNamespace

import pytest

from bot.analysis import llm as llm_mod
from bot.config import BotConfig

PROMPTS = {"system": "СИСТЕМА", "user": "Тезисы: {transcript}"}


def _config(**overrides):
    params = {
        "bot_token": "test-token",
        "llm_api_key": "test-key",
        "llm_base_url": "https://api.test/v1",
        "llm_model": "test-model",
    }
    params.update(overrides)
    return BotConfig(**params)


def _client(config=None):
    return llm_mod.LLMClient(config or _config(), PROMPTS)


def _exc_with_headers(headers):
    return SimpleNamespace(response=SimpleNamespace(headers=headers))


class TestRetryAfterSeconds:
    def test_retry_after_header(self):
        assert llm_mod._retry_after_seconds(_exc_with_headers({"retry-after": "7"})) == 7.0

    def test_header_case_variants(self):
        assert llm_mod._retry_after_seconds(_exc_with_headers({"Retry-After": "9"})) == 9.0

    def test_unparseable_value_returns_none(self):
        # "0.9s" не парсится float() — должен вернуть None (не упасть)
        assert llm_mod._retry_after_seconds(_exc_with_headers({"x-ratelimit-reset-requests": "0.9s"})) is None

    def test_capped_by_explicit_cap(self):
        assert llm_mod._retry_after_seconds(_exc_with_headers({"retry-after": "9999"}), cap=50) == 50.0

    def test_capped_by_default_equal_config_backoff(self):
        # дефолтный кэп функции — дефолтный llm_max_backoff из конфига
        assert llm_mod._retry_after_seconds(_exc_with_headers({"retry-after": "9999"})) == BotConfig().llm_max_backoff

    def test_body_error_payload(self):
        e = SimpleNamespace(response=None, body={"error": {"retry_after": 12}})
        assert llm_mod._retry_after_seconds(e) == 12.0

    def test_no_retry_info_returns_none(self):
        assert llm_mod._retry_after_seconds(SimpleNamespace()) is None


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
    """Подменяет OpenAI в модуле; возвращает (фейк-completions, kwargs конструктора)."""
    comp = _FakeCompletions(outcomes)
    fake_client = SimpleNamespace(chat=SimpleNamespace(completions=comp))
    openai_kwargs = {}

    def _fake_openai(**kwargs):
        openai_kwargs.update(kwargs)
        return fake_client

    monkeypatch.setattr(llm_mod, "OpenAI", _fake_openai)
    monkeypatch.setattr(llm_mod.time, "sleep", lambda s: None)
    monkeypatch.setattr(llm_mod.random, "uniform", lambda a, b: 0)  # убрать джиттер
    return comp, openai_kwargs


class TestSummarizeRetries:
    def test_success_after_two_429(self, monkeypatch):
        class Fake429(Exception):
            pass

        comp, _ = _patch_llm(monkeypatch, [Fake429(), Fake429(), _resp("итог")])
        # except-ветка берёт имя из глобалов модуля — подменяем на тестовый класс
        monkeypatch.setattr(llm_mod, "RateLimitError", Fake429)
        waits = []
        monkeypatch.setattr(llm_mod.time, "sleep", lambda s: waits.append(s))

        assert _client().summarize("транскрипт") == "итог"
        assert comp.calls == 3
        assert waits == [pytest.approx(5), pytest.approx(10)]  # экспоненциальная пауза

    def test_empty_content_retried_then_raises(self, monkeypatch):
        comp, _ = _patch_llm(monkeypatch, [_resp("  ")] * 5)  # дефолт llm_max_attempts

        with pytest.raises(RuntimeError, match="пустой ответ"):
            _client().summarize("транскрипт")
        assert comp.calls == 5

    def test_permanent_error_not_retried(self, monkeypatch):
        class BadRequest(Exception):
            pass

        comp, _ = _patch_llm(monkeypatch, [BadRequest("max_tokens too large")])

        # 400-е ошибки не входят в ретраебельные ветки — сразу наружу
        with pytest.raises(BadRequest):
            _client().summarize("транскрипт")
        assert comp.calls == 1

    def test_attempts_respect_config(self, monkeypatch):
        class Fake429(Exception):
            pass

        comp, _ = _patch_llm(monkeypatch, [Fake429(), _resp("ок")])
        monkeypatch.setattr(llm_mod, "RateLimitError", Fake429)

        assert _client(_config(llm_max_attempts=2)).summarize("транскрипт") == "ок"
        assert comp.calls == 2

    def test_backoff_capped_by_config(self, monkeypatch):
        class Fake429(Exception):
            pass

        comp, _ = _patch_llm(monkeypatch, [Fake429(), Fake429(), Fake429(), _resp("ок")])
        monkeypatch.setattr(llm_mod, "RateLimitError", Fake429)
        waits = []
        monkeypatch.setattr(llm_mod.time, "sleep", lambda s: waits.append(s))

        # llm_max_backoff=7: паузы 5 -> 10 -> 20 обрезаются кэпом до 5, 7, 7
        assert _client(_config(llm_max_backoff=7)).summarize("транскрипт") == "ок"
        assert waits == [pytest.approx(5), pytest.approx(7), pytest.approx(7)]

    def test_retry_after_capped_by_config(self, monkeypatch):
        class Fake429(Exception):
            pass

        exc = Fake429()
        exc.response = SimpleNamespace(headers={"retry-after": "9999"})
        comp, _ = _patch_llm(monkeypatch, [exc, _resp("ок")])
        monkeypatch.setattr(llm_mod, "RateLimitError", Fake429)
        waits = []
        monkeypatch.setattr(llm_mod.time, "sleep", lambda s: waits.append(s))

        # retry-after провайдера (9999с) обрезается кэпом ИЗ КОНФИГА (7), а не дефолтным 120
        assert _client(_config(llm_max_backoff=7)).summarize("транскрипт") == "ок"
        assert comp.calls == 2
        assert waits == [pytest.approx(7)]

    def test_transcript_cut_to_limit(self, monkeypatch):
        comp, _ = _patch_llm(monkeypatch, [_resp("ок")])

        _client().summarize("x" * (BotConfig().transcript_limit + 100))
        user_msg = comp.last_kwargs["messages"][1]["content"]
        limit = BotConfig().transcript_limit
        assert "x" * limit in user_msg           # вошёл целиком лимит
        assert "x" * (limit + 1) not in user_msg  # и ни символом больше

    def test_transcript_limit_from_config(self, monkeypatch):
        comp, _ = _patch_llm(monkeypatch, [_resp("ок")])

        _client(_config(transcript_limit=100)).summarize("x" * 300)
        user_msg = comp.last_kwargs["messages"][1]["content"]
        assert "x" * 100 in user_msg
        assert "x" * 101 not in user_msg

    def test_prompts_system_and_user_used(self, monkeypatch):
        comp, _ = _patch_llm(monkeypatch, [_resp("ок")])

        _client().summarize("транскрипт-текст")
        messages = comp.last_kwargs["messages"]
        assert messages[0] == {"role": "system", "content": "СИСТЕМА"}
        assert messages[1]["content"] == "Тезисы: транскрипт-текст"

    def test_llm_max_tokens_and_model_in_api_kwargs(self, monkeypatch):
        comp, _ = _patch_llm(monkeypatch, [_resp("ок")])

        _client(_config(llm_max_tokens=4321)).summarize("транскрипт")
        assert comp.last_kwargs["max_tokens"] == 4321
        assert comp.last_kwargs["model"] == "test-model"

    def test_openai_client_receives_api_key_and_base_url(self, monkeypatch):
        _, openai_kwargs = _patch_llm(monkeypatch, [_resp("ок")])

        _client().summarize("транскрипт")
        assert openai_kwargs["api_key"] == "test-key"
        assert openai_kwargs["base_url"] == "https://api.test/v1"

    def test_garbage_env_llm_max_tokens_falls_back_to_default(self, monkeypatch, tmp_path):
        # мусорный env-лимит -> дефолт конфига -> дефолт в вызове API (не падение)
        config = BotConfig.from_env(
            environ={"LLM_API_KEY": "k", "LLM_MAX_TOKENS": "мусор"},
            env_file=tmp_path / ".env",
        )
        comp, _ = _patch_llm(monkeypatch, [_resp("ок")])

        _client(config).summarize("транскрипт")
        assert comp.last_kwargs["max_tokens"] == 50000
