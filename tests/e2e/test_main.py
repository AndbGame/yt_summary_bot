"""Композиция: build_deps собирает реальные зависимости; main() валидирует конфиг.

Реальные discord/openai-клиенты здесь только конструируются — сети нет.
"""

import asyncio

import discord
import pytest

from bot.config import BotConfig
from bot.delivery.sender import SafeSender
from bot.main import build_deps, main
from bot.store.outbox import DbOutbox
from bot.store.reports import ReportStore


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
    assert hasattr(deps.llm, "analyze_content")  # реальный LLMClient за портом .analyze_content
    assert isinstance(deps.outbox, DbOutbox)
    assert isinstance(deps.reports, ReportStore)
    assert isinstance(deps.sender, SafeSender)
    assert isinstance(deps.client, discord.Client)
    assert isinstance(deps.queue, asyncio.Queue)

    # фаза 4: роутер с экстракторами за портом, задачи и дедуп — репозитории БД
    # фаза 5: реестр [YoutubeExtractor, ArticleExtractor] — article последним
    from bot.router import Router
    from bot.sources.article import ArticleExtractor
    from bot.sources.youtube import YoutubeExtractor
    from bot.store.dedup import DedupStore
    from bot.store.jobs import JobStore

    assert isinstance(deps.router, Router)
    assert isinstance(deps.router.by_name("video"), YoutubeExtractor), (
        "YouTube — первый экстрактор в реестре роутера"
    )
    assert isinstance(deps.router.by_name("article"), ArticleExtractor), (
        "ArticleExtractor — экстрактор статей в реестре роутера"
    )
    assert len(deps.router.extractors) == 2
    assert deps.router.extractors[0] is deps.router.by_name("video")
    assert deps.router.extractors[-1] is deps.router.by_name("article"), (
        "article — ПОСЛЕДНИЙ (fallback) экстрактор: YouTube забирает своё первым"
    )
    assert isinstance(deps.jobs, JobStore)
    assert isinstance(deps.dedup, DedupStore)


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
