"""Конфигурация бота: все env-настройки в одном dataclass.

Конвенция AGENTS.md «константы-настройки — env-переменные с дефолтом,
читаются только в одном месте» — здесь. from_env() без побочных эффектов:
os.environ не изменяется, .env парсится встроенным парсером монолита
(кавычки снимаются, комментарии и пустые строки пропускаются).
Приоритет: уже выставленная переменная окружения сильнее значения из .env
(та же setdefault-семантика, что была в монолитном load_env()).
"""

import os
from dataclasses import dataclass
from pathlib import Path

# Дефолты — единая точка правды и для dataclass, и для парсинга env.
# MAX_MESSAGE_LENGTH (2000) — лимит Discord, живёт в bot.delivery.render:
# это константа платформы, а не настройка запуска.
DEFAULT_LLM_BASE_URL = "https://api.openai.com/v1"
DEFAULT_LLM_MODEL = "gpt-4o-mini"
DEFAULT_LLM_MAX_TOKENS = 50000  # рассчитан на GLM; у OpenAI-моделей лимит ниже (см. AGENTS.md)
DEFAULT_LLM_MAX_ATTEMPTS = 5
DEFAULT_LLM_MAX_BACKOFF = 120
DEFAULT_SEND_MAX_ATTEMPTS = 3
DEFAULT_FLUSH_INTERVAL_S = 60
DEFAULT_TRANSCRIPT_LIMIT = 50000
DEFAULT_DB_PATH = "bot_data.sqlite3"
DEFAULT_SPLIT_THRESHOLD = 1900
DEFAULT_MAX_SPLIT_MESSAGES = 8
DEFAULT_AUTHOR_TTL_DAYS = 90  # TTL обзора автора (ARCHITECTURE.md §7)

# Имя env-переменной для каждого поля; нужно main() для текста об ошибке запуска
ENV_NAMES = {
    "bot_token": "DISCORD_BOT_TOKEN",
    "llm_api_key": "LLM_API_KEY",
    "llm_base_url": "LLM_BASE_URL",
    "llm_model": "LLM_MODEL",
    "llm_max_tokens": "LLM_MAX_TOKENS",
    "llm_max_attempts": "LLM_MAX_ATTEMPTS",
    "llm_max_backoff": "LLM_MAX_BACKOFF",
    "send_max_attempts": "SEND_MAX_ATTEMPTS",
    "flush_interval_s": "FLUSH_INTERVAL_S",
    "transcript_limit": "TRANSCRIPT_LIMIT",
    "db_path": "DB_PATH",
    "split_threshold": "SPLIT_THRESHOLD",
    "max_split_messages": "MAX_SPLIT_MESSAGES",
    "author_ttl_days": "AUTHOR_TTL_DAYS",
}

# .env ищется в корне репозитория (рядом с шимом yt_summary_bot.py)
_DEFAULT_ENV_FILE = Path(__file__).resolve().parents[1] / ".env"


def _read_env_file(path: Path) -> dict:
    """Простой парсер .env без внешних зависимостей (перенос load_env монолита)."""
    if not path.exists():
        return {}
    data = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            data[key.strip()] = value.strip().strip("'\"")
    return data


def _merged_env(environ, env_file) -> dict:
    """Слияние окружения и .env: существующие переменные сильнее (setdefault-семантика)."""
    if environ is None:
        environ = os.environ
    if env_file is None:
        env_file = _DEFAULT_ENV_FILE
    merged = dict(environ)
    for key, value in _read_env_file(Path(env_file)).items():
        merged.setdefault(key, value)
    return merged


def _env_int(env: dict, name: str, default: int) -> int:
    """int из env; мусорное значение («abc», «12.5», пустое) — не падение, а дефолт."""
    try:
        return int(env.get(name, default))
    except (TypeError, ValueError):
        return default


@dataclass
class BotConfig:
    """Все настройки бота; обязательные поля — bot_token и llm_api_key."""

    bot_token: str | None = None
    llm_api_key: str | None = None
    llm_base_url: str = DEFAULT_LLM_BASE_URL
    llm_model: str = DEFAULT_LLM_MODEL
    llm_max_tokens: int = DEFAULT_LLM_MAX_TOKENS
    llm_max_attempts: int = DEFAULT_LLM_MAX_ATTEMPTS
    llm_max_backoff: int = DEFAULT_LLM_MAX_BACKOFF
    send_max_attempts: int = DEFAULT_SEND_MAX_ATTEMPTS
    flush_interval_s: int = DEFAULT_FLUSH_INTERVAL_S
    transcript_limit: int = DEFAULT_TRANSCRIPT_LIMIT
    db_path: str = DEFAULT_DB_PATH  # задел под SQLite-кеш (фаза 1)
    split_threshold: int = DEFAULT_SPLIT_THRESHOLD
    max_split_messages: int = DEFAULT_MAX_SPLIT_MESSAGES
    author_ttl_days: int = DEFAULT_AUTHOR_TTL_DAYS

    @classmethod
    def from_env(cls, environ=None, env_file=None) -> "BotConfig":
        """Читает настройки из окружения и .env; ничего не мутирует и не создаёт клиентов.

        Значения из .env видны только BotConfig и не инжектятся в os.environ —
        поведение осознанное.
        """
        env = _merged_env(environ, env_file)
        return cls(
            bot_token=env.get(ENV_NAMES["bot_token"]),
            llm_api_key=env.get(ENV_NAMES["llm_api_key"]),
            llm_base_url=env.get(ENV_NAMES["llm_base_url"], DEFAULT_LLM_BASE_URL),
            llm_model=env.get(ENV_NAMES["llm_model"], DEFAULT_LLM_MODEL),
            llm_max_tokens=_env_int(env, ENV_NAMES["llm_max_tokens"], DEFAULT_LLM_MAX_TOKENS),
            llm_max_attempts=_env_int(env, ENV_NAMES["llm_max_attempts"], DEFAULT_LLM_MAX_ATTEMPTS),
            llm_max_backoff=_env_int(env, ENV_NAMES["llm_max_backoff"], DEFAULT_LLM_MAX_BACKOFF),
            send_max_attempts=_env_int(env, ENV_NAMES["send_max_attempts"], DEFAULT_SEND_MAX_ATTEMPTS),
            flush_interval_s=_env_int(env, ENV_NAMES["flush_interval_s"], DEFAULT_FLUSH_INTERVAL_S),
            transcript_limit=_env_int(env, ENV_NAMES["transcript_limit"], DEFAULT_TRANSCRIPT_LIMIT),
            db_path=env.get(ENV_NAMES["db_path"], DEFAULT_DB_PATH),
            split_threshold=_env_int(env, ENV_NAMES["split_threshold"], DEFAULT_SPLIT_THRESHOLD),
            max_split_messages=_env_int(
                env, ENV_NAMES["max_split_messages"], DEFAULT_MAX_SPLIT_MESSAGES
            ),
            author_ttl_days=_env_int(env, ENV_NAMES["author_ttl_days"], DEFAULT_AUTHOR_TTL_DAYS),
        )

    def missing_fields(self) -> list:
        """Обязательные поля, оставшиеся пустыми (порядок — для сообщения об ошибке)."""
        return [name for name in ("bot_token", "llm_api_key") if not getattr(self, name)]
