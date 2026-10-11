"""Юнит-тесты BotConfig: дефолты, чтение .env, приоритеты, missing_fields, int-парсинг."""

import os

import pytest

from bot.config import BotConfig

# Все from_env-тесты передают env_file во временную папку: реальный .env
# репозитория никогда не читается и не влияет на тесты.


def test_defaults():
    config = BotConfig()
    assert config.bot_token is None
    assert config.llm_api_key is None
    assert config.llm_base_url == "https://api.openai.com/v1"
    assert config.llm_model == "gpt-4o-mini"
    assert config.llm_max_tokens == 50000
    assert config.llm_max_attempts == 5
    assert config.llm_max_backoff == 120
    assert config.send_max_attempts == 3
    assert config.flush_interval_s == 60
    assert config.transcript_limit == 50000
    assert config.db_path == "bot_data.sqlite3"
    assert config.split_threshold == 1900
    assert config.max_split_messages == 8
    assert config.author_ttl_days == 90  # фаза 3: TTL обзора автора


def test_from_env_reads_dotenv(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "DISCORD_BOT_TOKEN=tok-dotenv\n"
        "LLM_API_KEY=key-dotenv\n"
        "LLM_BASE_URL=http://dotenv/v1\n",
        encoding="utf-8",
    )
    config = BotConfig.from_env(environ={}, env_file=env_file)
    assert config.bot_token == "tok-dotenv"
    assert config.llm_api_key == "key-dotenv"
    assert config.llm_base_url == "http://dotenv/v1"


def test_env_var_priority_over_dotenv(tmp_path):
    # уже выставленная переменная окружения сильнее значения из .env
    env_file = tmp_path / ".env"
    env_file.write_text("DISCORD_BOT_TOKEN=from-dotenv\n", encoding="utf-8")
    config = BotConfig.from_env(
        environ={"DISCORD_BOT_TOKEN": "from-env-var"}, env_file=env_file
    )
    assert config.bot_token == "from-env-var"


def test_environ_none_reads_os_environ(monkeypatch, tmp_path):
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "tok-os")
    monkeypatch.setenv("LLM_API_KEY", "key-os")
    env_file = tmp_path / ".env"  # не существует — os.environ единственный источник
    config = BotConfig.from_env(env_file=env_file)
    assert config.bot_token == "tok-os"
    assert config.llm_api_key == "key-os"


def test_from_env_does_not_mutate_os_environ(tmp_path, monkeypatch):
    # значения из .env не должны протекать в os.environ (без side effects)
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("DISCORD_BOT_TOKEN=tok-dotenv\n", encoding="utf-8")
    BotConfig.from_env(environ={}, env_file=env_file)
    assert "DISCORD_BOT_TOKEN" not in os.environ


def test_missing_fields():
    assert BotConfig().missing_fields() == ["bot_token", "llm_api_key"]
    assert BotConfig(bot_token="t").missing_fields() == ["llm_api_key"]
    assert BotConfig(llm_api_key="k").missing_fields() == ["bot_token"]
    assert BotConfig(bot_token="t", llm_api_key="k").missing_fields() == []


def test_llm_max_tokens_env_override(tmp_path):
    config = BotConfig.from_env(
        environ={"LLM_MAX_TOKENS": "1234"}, env_file=tmp_path / ".env"
    )
    assert config.llm_max_tokens == 1234


@pytest.mark.parametrize("garbage", ["abc", "", "12.5", "  "])
def test_llm_max_tokens_garbage_falls_back_to_default(tmp_path, garbage):
    # мусорное значение не должно ронять конфиг — берётся дефолт
    config = BotConfig.from_env(
        environ={"LLM_MAX_TOKENS": garbage}, env_file=tmp_path / ".env"
    )
    assert config.llm_max_tokens == 50000


@pytest.mark.parametrize("garbage", ["abc", "", "12.5", "  "])
def test_author_ttl_days_garbage_falls_back_to_default(tmp_path, garbage):
    # мусорный AUTHOR_TTL_DAYS не должен ронять конфиг — берётся дефолт (90 дней)
    config = BotConfig.from_env(
        environ={"AUTHOR_TTL_DAYS": garbage}, env_file=tmp_path / ".env"
    )
    assert config.author_ttl_days == 90


@pytest.mark.parametrize(
    ("env_name", "field", "value"),
    [
        ("LLM_MAX_ATTEMPTS", "llm_max_attempts", "3"),
        ("LLM_MAX_BACKOFF", "llm_max_backoff", "15"),
        ("SEND_MAX_ATTEMPTS", "send_max_attempts", "7"),
        ("TRANSCRIPT_LIMIT", "transcript_limit", "999"),
        ("SPLIT_THRESHOLD", "split_threshold", "1500"),
        ("MAX_SPLIT_MESSAGES", "max_split_messages", "4"),
        ("AUTHOR_TTL_DAYS", "author_ttl_days", "30"),
    ],
)
def test_int_fields_env_override(tmp_path, env_name, field, value):
    # имена переменных — как в монолите/AGENTS.md: настройки через env с дефолтом
    config = BotConfig.from_env(environ={env_name: value}, env_file=tmp_path / ".env")
    assert getattr(config, field) == int(value)


def test_llm_base_url_and_model_env_override(tmp_path):
    config = BotConfig.from_env(
        environ={"LLM_BASE_URL": "http://custom/v1", "LLM_MODEL": "custom-model"},
        env_file=tmp_path / ".env",
    )
    assert config.llm_base_url == "http://custom/v1"
    assert config.llm_model == "custom-model"


def test_dotenv_quotes_stripped(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text('DISCORD_BOT_TOKEN="tok"\n', encoding="utf-8")
    config = BotConfig.from_env(environ={}, env_file=env_file)
    assert config.bot_token == "tok"


def test_dotenv_ignores_comments_and_blank_lines(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# комментарий\n"
        "\n"
        "LLM_API_KEY=key\n",
        encoding="utf-8",
    )
    config = BotConfig.from_env(environ={}, env_file=env_file)
    assert config.llm_api_key == "key"
    assert config.bot_token is None


def test_missing_dotenv_file_is_ok(tmp_path):
    config = BotConfig.from_env(environ={}, env_file=tmp_path / "nope.env")
    assert config.bot_token is None
    assert config.llm_max_tokens == 50000
