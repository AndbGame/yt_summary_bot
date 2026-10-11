"""Композиция приложения: конфиг -> зависимости -> discord-адаптер -> запуск.

Здесь допустим единственный side effect верхнего уровня — logging.basicConfig:
на импорте не читается .env, не создаются клиенты и нет сети.
"""

import logging
import sys
from pathlib import Path

from bot import discord_app
from bot.analysis.llm import LLMClient
from bot.analysis.prompts import load_prompts
from bot.config import ENV_NAMES, BotConfig
from bot.delivery.outbox import PendingOutbox
from bot.delivery.sender import SafeSender
from bot.jobs import create_queue
from bot.pipeline import Deps

# Буфер недоставленных ответов — в корне репозитория (рядом со шимом), как у монолита
PENDING_FILE = Path(__file__).resolve().parents[1] / "pending_replies.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("yt-summary")


def build_deps(config: BotConfig) -> Deps:
    """Собирает реальные зависимости: промпты, LLM, буфер, отправитель, клиент."""
    # prompts.yaml читается с диска ровно один раз: и в LLMClient, и в error-ветку пайплайна
    prompts = load_prompts()
    outbox = PendingOutbox(PENDING_FILE)
    llm = LLMClient(config, prompts)
    queue = create_queue()
    client = discord_app.create_client()
    sender = SafeSender(client, outbox, config)
    deps = Deps(
        config=config,
        llm=llm,
        outbox=outbox,
        prompts=prompts,
        sender=sender,
        client=client,
        queue=queue,
    )
    discord_app.attach_handlers(client, deps)
    return deps


def main() -> None:
    """Точка входа: валидация обязательных настроек -> запуск discord-клиента."""
    config = BotConfig.from_env()
    missing = config.missing_fields()
    if missing:
        # в сообщении — имена env-переменных, как в монолите
        names = ", ".join(ENV_NAMES.get(name, name) for name in missing)
        sys.exit(f"Не заданы переменные: {names}. Создай .env рядом со скриптом.")
    deps = build_deps(config)
    deps.client.run(config.bot_token)
