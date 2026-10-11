"""Композиция: build_deps собирает реальные зависимости; main() валидирует конфиг.

Реальные discord/openai-клиенты здесь только конструируются — сети нет.
"""

import asyncio

import discord
import pytest

from bot.config import BotConfig
from bot.delivery.outbox import PendingOutbox
from bot.delivery.sender import SafeSender
from bot.main import build_deps, main


class _StubConfigEmpty:
    """Подмена BotConfig в main(): from_env даёт пустой конфиг (реальный .env не читается)."""

    @classmethod
    def from_env(cls, environ=None, **kwargs):
        return BotConfig()


class _StubConfigFull:
    """Подмена BotConfig в main(): конфиг со всеми обязательными полями."""

    @classmethod
    def from_env(cls, environ=None, **kwargs):
        return BotConfig(bot_token="token-test", llm_api_key="key-test")


def test_build_deps_wires_real_components(tmp_path):
    config = BotConfig(bot_token="t", llm_api_key="k", db_path=str(tmp_path / "db.sqlite3"))

    deps = build_deps(config)

    assert deps.config is config
    assert hasattr(deps.llm, "summarize")  # реальный LLMClient за портом .summarize
    assert isinstance(deps.outbox, PendingOutbox)
    assert isinstance(deps.sender, SafeSender)
    assert isinstance(deps.client, discord.Client)
    assert isinstance(deps.queue, asyncio.Queue)


def test_main_exits_without_required_config(monkeypatch):
    # подменяем BotConfig в обоих модулях: независимо от того, как main его импортирует
    monkeypatch.setattr("bot.main.BotConfig", _StubConfigEmpty, raising=False)
    monkeypatch.setattr("bot.config.BotConfig", _StubConfigEmpty, raising=False)

    with pytest.raises(SystemExit) as excinfo:
        main()

    message = str(excinfo.value.code)
    assert "DISCORD_BOT_TOKEN" in message
    assert "LLM_API_KEY" in message


def test_main_runs_client_with_token(monkeypatch):
    ran = []
    monkeypatch.setattr("bot.main.BotConfig", _StubConfigFull, raising=False)
    monkeypatch.setattr("bot.config.BotConfig", _StubConfigFull, raising=False)
    monkeypatch.setattr(discord.Client, "run", lambda self, token, **kwargs: ran.append(token))

    main()

    assert ran == ["token-test"]
