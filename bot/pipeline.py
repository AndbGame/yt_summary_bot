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

Обзор автора (фаза 3) строится только на промахе кеша: провайдер фактов ->
свежая строка кеша авторов либо review_author; блок добавляется в отчёт до
кеширования. На попадании автор-блок берётся из payload как есть, а устаревшая
или отсутствующая строка автора даёт фоновую задачу обновления — пользователь
никогда не ждёт фона. Любой сбой автор-пути не роняет джобу.
"""

import asyncio
import hashlib
import json
import logging
import re
from dataclasses import dataclass
from typing import Any

import discord

from bot.analysis.author import facts_to_dict
from bot.analysis.prompts import load_prompts
from bot.analysis.report import Report, ReportBlock, render_report_messages, validate_budgets
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
    """Явные зависимости пайплайна (вместо глобалов монолита); в тестах — фейки.

    Поля фазы 3 могут быть None (None-безопасность): без провайдера фактов или
    хранилища авторов автор-путь просто пропускается, отчёт доставляется без
    обзора автора.
    """

    config: BotConfig
    llm: Any  # LLMClient | фейк из conftest
    outbox: OutboxPort  # PendingOutbox | DbOutbox — оба структурно соответствуют порту
    prompts: dict | None  # prompts.yaml в памяти; None — лениво прочитать с диска
    sender: SafeSender
    client: Any  # discord.Client | фейк из conftest
    queue: asyncio.Queue
    reports: Any  # ReportStore | фейк из conftest — кеш отчётов по ключу фазы 2
    # фаза 3: провайдер фактов автора, кеш обзоров, фоновая очередь обновления
    # и общий лок LLM-вызовов (контент-разбор и обзор автора не пересекаются)
    author_facts: Any = None  # YouTubeAuthorFactsProvider | фейк из conftest
    authors: Any = None  # AuthorStore | фейк из conftest
    background_queue: Any = None  # asyncio.Queue задач {"platform", "handle"}
    llm_lock: Any = None  # asyncio.Lock | None — сериализация LLM-вызовов


def _content_hash(transcript: str) -> str:
    """Хэш субтитров — четвёртая часть ключа кеша (короткий, для читаемости ключа)."""
    return hashlib.sha256(transcript.encode("utf-8")).hexdigest()[:16]


async def llm_call(deps: "Deps", method: str, arg) -> Any:
    """Единственная дверь к LLM: исполнение в потоке + deps.llm_lock (если задан).

    Методы LLM-клиента синхронные, поэтому asyncio.to_thread; лок — барьер между
    llm_worker и author_refresh_worker, чтобы их вызовы не пересекались
    (провайдер лимитирует, см. AGENTS.md «один LLM-воркер»).
    """
    lock = getattr(deps, "llm_lock", None)
    call = getattr(deps.llm, method)
    if lock is None:
        return await asyncio.to_thread(call, arg)
    async with lock:
        return await asyncio.to_thread(call, arg)


def _author_block(ref, facts, review: str) -> ReportBlock:
    """Автор-блок отчёта; data связывает блок с ключом кеша авторов (HIT-путь)."""
    return ReportBlock(
        "author",
        title="Обзор автора",
        body_md=review,
        data={
            "platform": getattr(ref, "platform", "") or "",
            "handle": getattr(ref, "handle", "") or "",
            "name": getattr(facts, "name", "") or "",
            "url": getattr(facts, "url", "") or "",
        },
    )


def _author_ttl_seconds(deps: "Deps") -> int:
    return deps.config.author_ttl_days * 86400


async def _attach_author_block(
    deps: "Deps", report: Report, video_id: str, prompts_ver: str, budgets: dict | None
) -> None:
    """MISS-путь автора: факты -> свежая строка кеша или review_author.

    Блок добавляется в отчёт ДО кеширования — в кеш попадает отчёт уже с
    автором. Любой сбой пути (фактов нет, обзор не вышел, кеш автора упал)
    оставляет отчёт без автора: доставка разбора не должна зависеть от обзора
    автора.
    """
    provider = getattr(deps, "author_facts", None)
    authors = getattr(deps, "authors", None)
    if provider is None:
        return
    try:
        fetched = await asyncio.to_thread(provider.get, video_id)
    except Exception as e:
        log.warning("факты автора не получены (%s): %s", video_id, e)
        return
    if not fetched:
        return
    ref, facts = fetched
    platform = getattr(ref, "platform", "") or ""
    handle = getattr(ref, "handle", "") or ""
    if not handle:
        return

    try:
        row = authors.get(platform, handle) if authors is not None else None
        fresh = bool(row) and bool(row.get("review_json")) and authors.is_fresh(
            row, _author_ttl_seconds(deps)
        )
    except Exception as e:
        # сбой кеша автора — деградация до без-автор-блока, а не падение джобы:
        # строка считается отсутствующей, обзор строится в LLM как на промахе
        log.warning("кеш автора недоступен (%s/%s): %s", platform, handle, e)
        row, fresh = None, False
    if fresh:
        # свежая строка кеша: LLM не зовётся, обзор берётся как записан
        review = row["review_json"]
    else:
        try:
            review = await llm_call(deps, "review_author", facts)
        except Exception as e:
            log.warning("обзор автора не вышел (%s): %s", video_id, e)
            return
    report.blocks.append(_author_block(ref, facts, review))
    # бюджет author-блока проверяется отдельно: блок появляется уже после общей
    # валидации отчёта в process_job (превышение не фатально, рендер укоротит)
    for warning in validate_budgets(Report([report.blocks[-1]]), budgets):
        log.warning("%s: %s", video_id, warning)
    if fresh:
        return

    if authors is not None:
        try:
            authors.put(
                platform,
                handle,
                getattr(facts, "name", "") or "",
                json.dumps(facts_to_dict(facts), ensure_ascii=False),
                review,
                prompts_ver,
            )
        except Exception:
            log.exception("обзор автора %s/%s не закеширован", platform, handle)


def _queue_stale_author_refresh(deps: "Deps", report: Report) -> None:
    """HIT-путь автора: пользователь сразу получает старый блок из payload.

    Строка автора отсутствует или устарела — фоновая задача обновления;
    пользователь никогда не ждёт фона. Автор-блока нет — тишина.
    """
    queue = getattr(deps, "background_queue", None)
    if queue is None:
        return
    block = next((b for b in report.blocks if b.kind == "author"), None)
    if block is None:
        return
    data = block.data or {}
    platform, handle = data.get("platform"), data.get("handle")
    if not platform or not handle:
        return
    authors = getattr(deps, "authors", None)
    if authors is None:
        return
    try:
        row = authors.get(platform, handle)
        fresh = row is not None and authors.is_fresh(row, _author_ttl_seconds(deps))
    except Exception as e:
        # сбой кеша автора — деградация до без-автор-блока, а не падение джобы:
        # не зная свежести строки, фон не ставим (отчёт уже доставлен из payload)
        log.warning("кеш автора недоступен (%s/%s): %s", platform, handle, e)
        return
    if fresh:
        return
    queue.put_nowait({"platform": platform, "handle": handle})


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
                # HIT: автор-блок из payload, провайдер и LLM не трогаются;
                # строка автора устарела или отсутствует — обновление в фоне
                _queue_stale_author_refresh(deps, report)
            else:
                report = await llm_call(deps, "analyze_content", transcript)
                if not report.blocks:
                    # пустой отчёт нельзя ни кешировать, ни отправлять: пользователь
                    # останется без сообщений с висящим статусом
                    raise RuntimeError("LLM вернул пустой отчёт")
                # превышение бюджета не фатально: рендер укоротит блок, но в логе
                # остаётся след для настройки промптов
                for warning in validate_budgets(report, budgets):
                    log.warning("%s: %s", video_id, warning)
                # MISS: автор-блок строится до кеширования — в кеш попадает
                # отчёт уже с автором
                await _attach_author_block(deps, report, video_id, prompts_ver, budgets)
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
