"""Надёжная отправка в Discord: ретраи на сетевых сбоях + досылка буфера.

Стратегия из монолита: короткий ретрай-цикл send(), затем персистентный буфер
(PendingOutbox) и фоновая досылка flush_loop(), пока бот снова не встанет онлайн.
"""

import asyncio
import logging
from typing import Any

import aiohttp
import discord

from bot.config import BotConfig
from bot.delivery.outbox import PendingOutbox

log = logging.getLogger("yt-summary")


def is_retryable_send_error(e: Exception) -> bool:
    """Сетевые сбои и 5xx — ретраебельны; 4xx (права/не найдено) — нет."""
    if isinstance(e, (aiohttp.ClientError, asyncio.TimeoutError)):
        return True
    if isinstance(e, discord.HTTPException):
        return getattr(e, "status", 0) >= 500
    return False


async def resolve_channel(client: Any, thread_id: int):
    """Канал по id: сначала кэш клиента, затем fetch (для тредов вне кэша).

    None — тред не найден (например, удалён); вызывающий код решает:
    отбросить задачу или буферизовать сообщение.
    """
    ch = client.get_channel(thread_id)
    if ch is None:
        try:
            ch = await client.fetch_channel(thread_id)
        except (discord.HTTPException, discord.NotFound, discord.InvalidData):
            return None
    return ch


class SafeSender:
    """Отправка с ретраями и досылка персистентного буфера; клиент — из Deps."""

    def __init__(self, client: Any, outbox: PendingOutbox, config: BotConfig):
        self.client = client
        self.outbox = outbox
        self.config = config

    async def send(self, channel, content: str) -> bool:
        """Отправка с ретраями на сетевых сбоях. False — надо буферизовать."""
        last_exc = None
        for attempt in range(1, self.config.send_max_attempts + 1):
            try:
                await channel.send(content)
                return True
            except discord.HTTPException as e:
                if not is_retryable_send_error(e):
                    log.error(
                        "постоянная ошибка отправки в %s: %s", getattr(channel, "id", "?"), e
                    )
                    return False
                last_exc = e
            except Exception as e:  # ConnectionError и прочее
                last_exc = e
            wait = min(30, 3 * 2 ** (attempt - 1))
            log.warning(
                "отправка не прошла (попытка %d/%d), пауза %ss: %s",
                attempt, self.config.send_max_attempts, wait, last_exc,
            )
            await asyncio.sleep(wait)
        return False

    async def flush_pending(self) -> None:
        """Досылает буферизованные ответы. Если сеть опять лежит — сохраняет остаток
        и выходит (порядок сообщений сохраняется)."""
        items = self.outbox.load()
        if not items:
            return
        if self.client.is_closed() or not self.client.is_ready():
            return
        log.info("досылаю буфер: %d сообщ.", len(items))
        remaining: list = []
        for idx, item in enumerate(items):
            ch = await resolve_channel(self.client, int(item["thread_id"]))
            if ch is None:
                log.error("буфер: тред %s не найден, сообщение отброшено", item["thread_id"])
                continue
            ok = await self.send(ch, item["content"])
            if not ok:
                # сеть лежит: сохраняем недоставленное и всё, что после него
                remaining = items[idx:]
                break
        self.outbox.save(remaining)
        if not remaining:
            log.info("буфер пуст — всё доставлено")

    async def flush_loop(self) -> None:
        """Фоновый цикл: раз в config.flush_interval_s секунд пытается опустошить буфер."""
        while not self.client.is_closed():
            await asyncio.sleep(self.config.flush_interval_s)
            try:
                await self.flush_pending()
            except Exception:
                log.exception("ошибка фоновой досылки буфера")
