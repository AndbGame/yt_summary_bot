"""Пайплайн задачи: субтитры -> кеш/LLM (JSON-отчёт) -> прогрессивный рендер -> тред.

Ядро без глобалов: всё внешнее (Discord, LLM, буфер, кеш отчётов) приходит через
Deps — явные порты вместо монолитных глобалов; единственная точка подмены в
тестах — модульная функция fetch_transcript (monkeypatch bot.pipeline.fetch_transcript).

Отчёт доставляется по сообщениям (кратко -> тезисы -> разделы), каждое отдельно
проходит format_for_discord (LLM мог нарушить запрет таблиц) и чанк-сплит; при
сбое отправки в персистентный буфер уходит ПОЛНЫЙ хвост отчёта — остаток чанков
текущего сообщения плюс все последующие сообщения, — порядок доставки сохраняется.

Субтитры могли исчезнуть с YouTube: при ошибке скачивания отдаём последний
разбор этого видео из кеша (любой content_hash), без кеша — error-сообщение.
"""

import asyncio
import hashlib
import logging
import re
from dataclasses import dataclass
from typing import Any

import discord

from bot.analysis.prompts import load_prompts
from bot.analysis.report import Report, render_report_messages, validate_budgets
from bot.config import BotConfig
from bot.delivery.outbox import OutboxPort
from bot.delivery.render import format_for_discord, truncate_message
from bot.delivery.sender import SafeSender, resolve_channel
from bot.sources.youtube import canonical_video_id, fetch_transcript

log = logging.getLogger("yt-summary")

# Версия схемы отчёта — часть ключа кеша (ARCHITECTURE.md §6): фаза 2 перевела
# отчёт на блоки (JSON-ответ LLM), старые записи summary-1 инвалидируются сами.
REPORT_SCHEMA = "blocks-1"

_ERROR_TEMPLATE = "Не смог сделать разбор этого видео: {reason}"


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
    reports: Any  # ReportStore | фейк из conftest — кеш отчётов по ключу фазы 2


def _content_hash(transcript: str) -> str:
    """Хэш субтитров — четвёртая часть ключа кеша (короткий, для читаемости ключа)."""
    return hashlib.sha256(transcript.encode("utf-8")).hexdigest()[:16]


async def process_job(deps: Deps, job: dict) -> None:
    """Полный цикл: субтитры -> кеш/LLM -> рендер -> отправка в тред."""
    video_id = job["video_id"]
    thread = await resolve_channel(deps.client, job["thread_id"])
    if thread is None:
        log.error("тред %s недоступен, задача %s отброшена", job["thread_id"], video_id)
        return

    status = job.get("status")
    link = f"https://youtu.be/{video_id}"

    try:
        # промпты могут быть не заданы (обратная совместимость) — лениво читаем
        # с диска; версия промптов и бюджеты берутся из одного словаря
        prompts = deps.prompts
        if prompts is None:
            prompts = load_prompts()
        prompts_ver = str(prompts.get("version", "0"))
        budgets = prompts.get("budgets")

        # субтитры скачиваются ВСЕГДА: content_hash считается из них даже при
        # попадании в кеш (изменившиеся субтитры должны дать новый отчёт)
        canonical = canonical_video_id(video_id)
        try:
            transcript = await asyncio.to_thread(fetch_transcript, video_id)
        except Exception as fetch_err:
            # Трейд-офф «свежесть vs доступность» — осознанно отдаём возможное
            # устаревшее: субтитры могли исчезнуть с YouTube навсегда, и тогда без
            # фолбэка пользователь не получит ничего, хотя разбор этого видео уже
            # лежит в кеше под прежним content_hash (свежий хэш посчитать не из чего).
            cached = deps.reports.get_latest(canonical, prompts_ver, REPORT_SCHEMA)
            if cached is None:
                raise
            log.info("субтитры недоступны, отдаю прошлый разбор из кеша: %s", fetch_err)
            report = Report.from_json(cached)
        else:
            content_hash = _content_hash(transcript)
            payload = deps.reports.get(canonical, prompts_ver, REPORT_SCHEMA, content_hash)
            if payload is not None:
                log.info("разбор из кеша: %s", video_id)
                report = Report.from_json(payload)
            else:
                report = await asyncio.to_thread(deps.llm.analyze_content, transcript)
                if not report.blocks:
                    # пустой отчёт нельзя ни кешировать, ни отправлять: пользователь
                    # останется без сообщений с висящим статусом
                    raise RuntimeError("LLM вернул пустой отчёт")
                # превышение бюджета не фатально: рендер укоротит блок, но в логе
                # остаётся след для настройки промптов
                for warning in validate_budgets(report, budgets):
                    log.warning("%s: %s", video_id, warning)
                # в кеш кладём только успешный разбор: ошибки транскрипта/LLM
                # повторно разберутся на следующей попытке
                deps.reports.put(canonical, prompts_ver, REPORT_SCHEMA, report.to_json(), content_hash)

        messages = render_report_messages(report, link, budgets=budgets)
        if messages:
            # заголовок разбора — только в первом сообщении (рендер про ссылку-источник не знает)
            messages[0] = f"**Разбор: {link}**\n\n{messages[0]}"
    except Exception as e:
        log.warning("разбор не вышел (%s): %s", video_id, e)
        prompts = deps.prompts
        if prompts is None:
            # промпты не переданы в Deps: ленивое чтение с диска — один раз,
            # выше, для ключа кеша; при заданном deps.prompts error-ветка
            # диск не читает (шаблон берётся из памяти)
            prompts = load_prompts()
        # LLM/провайдер могут вернуть текст с mention — не позволяем боту тегать
        # людей: @everyone/@here и начало <@-пинга вырезаются из причины ошибки
        reason = re.sub(r"@everyone|@here|<@", "", str(e))
        error_text = prompts.get("error", _ERROR_TEMPLATE).format(reason=reason)
        messages = [f"**Разбор: {link}**\n\n{error_text}"]

    # отправка: каждое сообщение — свои чанки; чанки всех сообщений предвычислены
    # заранее, чтобы при сбое буферизовать ПОЛНЫЙ хвост отчёта одним проходом
    # (остаток текущего сообщения + все последующие сообщения): иначе сбой в
    # середине навсегда терял бы конец разбора; порядок доставки сохраняется
    chunks_per_message = [
        truncate_message(format_for_discord(message), deps.config.split_threshold)[
            : deps.config.max_split_messages
        ]
        for message in messages
    ]

    delivered = 0
    for i, chunks in enumerate(chunks_per_message):
        for idx, chunk in enumerate(chunks):
            if await deps.sender.send(thread, chunk):
                delivered += 1
                continue
            # хвост с упавшего чанка до конца отчёта — в порядке рендера
            tail = chunks[idx:] + [c for later in chunks_per_message[i + 1 :] for c in later]
            for buffered in tail:
                deps.outbox.append(thread.id, buffered)
            log.warning("%s: буферизовано %d чанков (Discord недоступен)", video_id, len(tail))
            return

    if status:
        try:
            await status.delete()
        except discord.HTTPException:
            pass
    log.info("готово: %s (%d сообщений, %d чанков)", video_id, len(messages), delivered)
