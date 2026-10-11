"""Пайплайн задачи: субтитры -> LLM -> форматирование -> отправка в тред.

Ядро без глобалов: всё внешнее (Discord, LLM, буфер) приходит через Deps —
явные порты вместо монолитных глобалов; единственная точка подмены в тестах —
модульная функция fetch_transcript (monkeypatch bot.pipeline.fetch_transcript).
"""

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

import discord

from bot.analysis.prompts import load_prompts
from bot.config import BotConfig
from bot.delivery.outbox import PendingOutbox
from bot.delivery.render import format_for_discord, truncate_message
from bot.delivery.sender import SafeSender, resolve_channel
from bot.sources.youtube import fetch_transcript

log = logging.getLogger("yt-summary")


@dataclass
class Deps:
    """Явные зависимости пайплайна (вместо глобалов монолита); в тестах — фейки."""

    config: BotConfig
    llm: Any  # LLMClient | фейк из conftest
    outbox: PendingOutbox
    prompts: dict | None  # prompts.yaml в памяти; None — лениво прочитать с диска
    sender: SafeSender
    client: Any  # discord.Client | фейк из conftest
    queue: asyncio.Queue


async def process_job(deps: Deps, job: dict) -> None:
    """Полный цикл: субтитры -> LLM -> форматирование -> отправка в тред."""
    video_id = job["video_id"]
    thread = await resolve_channel(deps.client, job["thread_id"])
    if thread is None:
        log.error("тред %s недоступен, задача %s отброшена", job["thread_id"], video_id)
        return

    status = job.get("status")

    try:
        transcript = await asyncio.to_thread(fetch_transcript, video_id)
        summary = await asyncio.to_thread(deps.llm.summarize, transcript)
    except Exception as e:
        log.warning("саммари не вышло (%s): %s", video_id, e)
        prompts = deps.prompts
        if prompts is None:
            # обратная совместимость: Deps без prompts — лениво читаем с диска
            prompts = load_prompts()
        summary = prompts.get("error", "Не смог сделать саммари: {reason}").format(reason=e)

    link = f"https://youtu.be/{video_id}"
    full = format_for_discord(f"**Тезисы: {link}**\n\n{summary}")
    chunks = truncate_message(full, deps.config.split_threshold)[: deps.config.max_split_messages]

    # отправка: при сетевом сбое недоставленные чанки уходят в персистентный буфер
    for idx, chunk in enumerate(chunks):
        if await deps.sender.send(thread, chunk):
            continue
        for rest in chunks[idx:]:
            deps.outbox.append(thread.id, rest)
        log.warning(
            "%s: буферизовано %d/%d чанков (Discord недоступен)",
            video_id, len(chunks) - idx, len(chunks),
        )
        return

    if status:
        try:
            await status.delete()
        except discord.HTTPException:
            pass
    log.info("готово: %s (%d чанк(ов))", video_id, len(chunks))
