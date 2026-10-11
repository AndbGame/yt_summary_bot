"""Юнит-тесты LLM-слоя (фаза 2): парсинг Retry-After, цикл ретраев
LLMClient.analyze_content(), JSON-режим (response_format + откат при 4xx),
сборка Report из ответа, попадание параметров конфига в вызов API.

openai-фейк подменяется на уровне модуля bot.analysis.llm — так же, как
исключения и OpenAI импортированы в самом модуле. Сети нет: реальные классы
openai используются только для КОНСТРУИРОВАНИЯ исключений (BadRequestError),
как это делает провайдер при отказе JSON-режима.
"""

from types import SimpleNamespace

import openai
import pytest

from bot.analysis import llm as llm_mod
from bot.analysis.report import Report, ReportParseError
from bot.config import BotConfig

# Промпты фазы 2: секция content_analysis с system/user ({transcript} в user)
PROMPTS = {"content_analysis": {"system": "СИСТЕМА", "user": "Отчёт: {transcript}"}}

_OK_JSON = '{"brief": "итог"}'


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


def _bad_request(text, status=400):
    """BadRequest провайдера, как его отдаёт openai-клиент: текст + 4xx-статус."""
    response = SimpleNamespace(status_code=status, headers={}, request=SimpleNamespace())
    return openai.BadRequestError(text, response=response, body=None)


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


class TestAnalyzeRetries:
    """Ретраи 429/сетевых сбоев — та же семантика, что у summarize() фазы 1."""

    def test_success_after_two_429(self, monkeypatch):
        class Fake429(Exception):
            pass

        comp, _ = _patch_llm(monkeypatch, [Fake429(), Fake429(), _resp(_OK_JSON)])
        # except-ветка берёт имя из глобалов модуля — подменяем на тестовый класс
        monkeypatch.setattr(llm_mod, "RateLimitError", Fake429)
        waits = []
        monkeypatch.setattr(llm_mod.time, "sleep", lambda s: waits.append(s))

        report = _client().analyze_content("транскрипт")
        assert isinstance(report, Report)
        assert comp.calls == 3
        assert waits == [pytest.approx(5), pytest.approx(10)]  # экспоненциальная пауза

    def test_attempts_respect_config(self, monkeypatch):
        class Fake429(Exception):
            pass

        comp, _ = _patch_llm(monkeypatch, [Fake429(), _resp(_OK_JSON)])
        monkeypatch.setattr(llm_mod, "RateLimitError", Fake429)

        assert isinstance(_client(_config(llm_max_attempts=2)).analyze_content("т"), Report)
        assert comp.calls == 2

    def test_backoff_capped_by_config(self, monkeypatch):
        class Fake429(Exception):
            pass

        comp, _ = _patch_llm(monkeypatch, [Fake429(), Fake429(), Fake429(), _resp(_OK_JSON)])
        monkeypatch.setattr(llm_mod, "RateLimitError", Fake429)
        waits = []
        monkeypatch.setattr(llm_mod.time, "sleep", lambda s: waits.append(s))

        # llm_max_backoff=7: паузы 5 -> 10 -> 20 обрезаются кэпом до 5, 7, 7
        _client(_config(llm_max_backoff=7)).analyze_content("т")
        assert waits == [pytest.approx(5), pytest.approx(7), pytest.approx(7)]

    def test_retry_after_capped_by_config(self, monkeypatch):
        class Fake429(Exception):
            pass

        exc = Fake429()
        exc.response = SimpleNamespace(headers={"retry-after": "9999"})
        comp, _ = _patch_llm(monkeypatch, [exc, _resp(_OK_JSON)])
        monkeypatch.setattr(llm_mod, "RateLimitError", Fake429)
        waits = []
        monkeypatch.setattr(llm_mod.time, "sleep", lambda s: waits.append(s))

        # retry-after провайдера (9999с) обрезается кэпом ИЗ КОНФИГА (7), а не дефолтным 120
        _client(_config(llm_max_backoff=7)).analyze_content("т")
        assert comp.calls == 2
        assert waits == [pytest.approx(7)]


class TestAnalyzeResponseParsing:
    """Ответ провайдера — JSON, который превращается в Report; мусор = «пустой ответ»."""

    def test_valid_json_becomes_report_with_blocks(self, monkeypatch):
        comp, _ = _patch_llm(
            monkeypatch,
            [
                _resp(
                    '{"brief": "Суть", "complexity": "specialized", "context": "Простыми", '
                    '"theses": ["т1"], "sections": [{"title": "S", "start_sec": 75, "body": "б"}]}'
                )
            ],
        )

        report = _client().analyze_content("т")
        assert [b.kind for b in report.blocks] == ["brief", "context", "theses", "detailed"]
        assert report.blocks[0].data == {"complexity": "specialized"}
        assert report.blocks[3].data == {"start_sec": 75}

    def test_unparseable_response_retried_then_raises(self, monkeypatch):
        # ReportParseError трактуется как «пустой ответ»: ретраи, потом исключение наружу
        comp, _ = _patch_llm(monkeypatch, [_resp("совсем не json")] * 5)  # дефолт llm_max_attempts

        with pytest.raises(ReportParseError):
            _client().analyze_content("транскрипт")
        assert comp.calls == 5

    def test_parse_failure_uses_short_pause(self, monkeypatch):
        # пауза как у прежнего «пустого ответа»: 2 * attempt, без бэк-оффа
        comp, _ = _patch_llm(monkeypatch, [_resp("мусор"), _resp("мусор"), _resp(_OK_JSON)])
        waits = []
        monkeypatch.setattr(llm_mod.time, "sleep", lambda s: waits.append(s))

        _client().analyze_content("т")
        assert comp.calls == 3
        assert waits == [pytest.approx(2), pytest.approx(4)]

    def test_bad_request_without_json_hint_not_retried(self, monkeypatch):
        # обычный 400 (не про JSON-режим) — не ретраебельный, исключение сразу наружу
        comp, _ = _patch_llm(monkeypatch, [_bad_request("max_tokens too large")])

        with pytest.raises(openai.BadRequestError):
            _client().analyze_content("транскрипт")
        assert comp.calls == 1


class TestJsonMode:
    """response_format={"type": "json_object"} и откат при отказе провайдера."""

    def test_response_format_sent_by_default(self, monkeypatch):
        comp, _ = _patch_llm(monkeypatch, [_resp(_OK_JSON)])

        _client().analyze_content("т")
        assert comp.last_kwargs["response_format"] == {"type": "json_object"}

    def test_bad_request_about_json_retried_without_response_format(self, monkeypatch):
        # BadRequest с "response_format" в тексте и 4xx -> повтор БЕЗ response_format
        comp, _ = _patch_llm(monkeypatch, [_bad_request("response_format is not supported"), _resp(_OK_JSON)])

        report = _client().analyze_content("т")
        assert isinstance(report, Report)
        assert comp.calls == 2
        assert "response_format" not in comp.last_kwargs

    def test_json_hint_variant_text_also_falls_back(self, monkeypatch):
        # условие по тексту: "response_format" ИЛИ "json"
        comp, _ = _patch_llm(monkeypatch, [_bad_request("'json' mode is unsupported"), _resp(_OK_JSON)])

        assert isinstance(_client().analyze_content("т"), Report)
        assert comp.calls == 2
        assert "response_format" not in comp.last_kwargs

    def test_json_mode_disabled_persists_for_next_call(self, monkeypatch):
        # после первого фейла следующий вызов сразу без response_format
        comp, _ = _patch_llm(monkeypatch, [_bad_request("response_format? no"), _resp(_OK_JSON)])
        client = _client()

        client.analyze_content("т1")
        comp.outcomes.append(_resp(_OK_JSON))
        client.analyze_content("т2")

        assert comp.calls == 3
        assert "response_format" not in comp.last_kwargs
        assert client._json_mode_disabled is True

    def test_json_hint_with_5xx_does_not_fall_back(self, monkeypatch):
        # откат только при 4xx: 5xx с «response_format» в тексте — наружу без повтора
        comp, _ = _patch_llm(monkeypatch, [_bad_request("response_format boom", status=502)])

        with pytest.raises(openai.BadRequestError):
            _client().analyze_content("т")
        assert comp.calls == 1

    def test_invalid_json_body_error_does_not_disable_json_mode(self, monkeypatch):
        # 4xx «invalid json in request body» — про тело запроса, а не про отказ
        # JSON-режима: «json» в тексте без слов режима/формата — откат не включается
        comp, _ = _patch_llm(monkeypatch, [_bad_request("invalid json in request body")])
        client = _client()

        with pytest.raises(openai.BadRequestError):
            client.analyze_content("т")
        assert comp.calls == 1
        assert client._json_mode_disabled is False

        # после фейка вызов продолжается с response_format (JSON-режим не отключён)
        comp.outcomes.append(_resp(_OK_JSON))
        client.analyze_content("т2")
        assert comp.last_kwargs["response_format"] == {"type": "json_object"}


class TestAnalyzeApiContract:
    """Сообщения из content_analysis, обрезка транскрипта, параметры конфига в API."""

    def test_prompts_system_and_user_used(self, monkeypatch):
        comp, _ = _patch_llm(monkeypatch, [_resp(_OK_JSON)])

        _client().analyze_content("транскрипт-текст")
        messages = comp.last_kwargs["messages"]
        assert messages[0] == {"role": "system", "content": "СИСТЕМА"}
        assert messages[1]["content"] == "Отчёт: транскрипт-текст"

    def test_transcript_cut_to_limit(self, monkeypatch):
        comp, _ = _patch_llm(monkeypatch, [_resp(_OK_JSON)])

        _client().analyze_content("x" * (BotConfig().transcript_limit + 100))
        user_msg = comp.last_kwargs["messages"][1]["content"]
        limit = BotConfig().transcript_limit
        assert "x" * limit in user_msg           # вошёл целиком лимит
        assert "x" * (limit + 1) not in user_msg  # и ни символом больше

    def test_transcript_limit_from_config(self, monkeypatch):
        comp, _ = _patch_llm(monkeypatch, [_resp(_OK_JSON)])

        _client(_config(transcript_limit=100)).analyze_content("x" * 300)
        user_msg = comp.last_kwargs["messages"][1]["content"]
        assert "x" * 100 in user_msg
        assert "x" * 101 not in user_msg

    def test_llm_max_tokens_and_model_in_api_kwargs(self, monkeypatch):
        comp, _ = _patch_llm(monkeypatch, [_resp(_OK_JSON)])

        _client(_config(llm_max_tokens=4321)).analyze_content("транскрипт")
        assert comp.last_kwargs["max_tokens"] == 4321
        assert comp.last_kwargs["model"] == "test-model"

    def test_openai_client_receives_api_key_and_base_url(self, monkeypatch):
        _, openai_kwargs = _patch_llm(monkeypatch, [_resp(_OK_JSON)])

        _client().analyze_content("транскрипт")
        assert openai_kwargs["api_key"] == "test-key"
        assert openai_kwargs["base_url"] == "https://api.test/v1"

    def test_garbage_env_llm_max_tokens_falls_back_to_default(self, monkeypatch, tmp_path):
        # мусорный env-лимит -> дефолт конфига -> дефолт в вызове API (не падение)
        config = BotConfig.from_env(
            environ={"LLM_API_KEY": "k", "LLM_MAX_TOKENS": "мусор"},
            env_file=tmp_path / ".env",
        )
        comp, _ = _patch_llm(monkeypatch, [_resp(_OK_JSON)])

        _client(config).analyze_content("транскрипт")
        assert comp.last_kwargs["max_tokens"] == 50000
