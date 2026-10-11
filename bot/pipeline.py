"""Пайплайн задачи: кеш -> субтитры -> LLM -> форматирование -> отправка в тред.

Ядро без глобалов: всё внешнее (Discord, LLM, буфер, кеш отчётов) приходит через
Deps — явные порты вместо монолитных глобалов; единственная точка подмены в
тестах — модульная функция fetch_transcript (monkeypatch bot.pipeline.fetch_transcript).
"""

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

import discord

from bot.analysis.prompts import load_prompts
from bot.config import BotConfig
from bot.delivery.outbox import OutboxPort
from bot.delivery.render import format_for_discord, truncate_message
from bot.delivery.sender import SafeSender, resolve_channel
from bot.sources.youtube import canonical_video_id, fetch_transcript

log = logging.getLogger("yt-summary")

# Версия схемы отчёта — часть ключа кеша (ARCHITECTURE.md §6). Сейчас формат
# один (свободный текст саммари); фаза 2 переведёт отчёт на блоки — константу
# поднимут, и старый кеш инвалидируется сам, без ручного сброса.
REPORT_SCHEMA = "summary-1"


@dataclass
class Deps:
    """Явные зависимости пайплайна (вместо глобалов монолита); в тестах — фейки."""

    config: BotConfig
    llm: Any  # LLMClient | фейк из conftest
    outbox: OutboxPort  # PendingOutbox | DbOutbox — оба структурно соответствуют порту
    prompts: dict | None  # prompts.yaml в памяти; None — лениво прочитать с диска
    sender: SafeSender
    client: Any  # discord.Client | фейк из conftest
    queue: asyncio.Queue
    reports: Any  # ReportStore | фейк из conftest — кеш отчётов по ключу фазы 1


async def process_job(deps: Deps, job: dict) -> None:
    """Полный цикл: кеш -> субтитры -> LLM -> форматирование -> отправка в тред."""
    video_id = job["video_id"]
    thread = await resolve_channel(deps.client, job["thread_id"])
    if thread is None:
        log.error("тред %s недоступен, задача %s отброшена", job["thread_id"], video_id)
        return

    status = job.get("status")

    try:
        # промпты могут быть не заданы (обратная совместимость) — лениво читаем
        # с диска; версия промптов входит в ключ кеша
        prompts = deps.prompts
        if prompts is None:
            prompts = load_prompts()
        prompts_ver = str(prompts.get("version", "0"))

        # ключ кеша: канонический id источника + версия промптов + схема отчёта
        canonical = canonical_video_id(video_id)
        summary = deps.reports.get(canonical, prompts_ver, REPORT_SCHEMA)
        if summary is not None:
            log.info("саммари из кеша: %s", video_id)
        else:
            transcript = await asyncio.to_thread(fetch_transcript, video_id)
            summary = await asyncio.to_thread(deps.llm.summarize, transcript)
            # в кеш кладём только успешный разбор: ошибки транскрипта/LLM
            # повторно разберутся на следующей попытке
            deps.reports.put(canonical, prompts_ver, REPORT_SCHEMA, summary)
    except Exception as e:
        log.warning("саммари не вышло (%s): %s", video_id, e)
        prompts = deps.prompts
        if prompts is None:
            # промпты не переданы в Deps: ленивое чтение с диска — один раз,
            # выше, для ключа кеша; при заданном deps.prompts error-ветка
            # диск не читает (шаблон берётся из памяти)
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
