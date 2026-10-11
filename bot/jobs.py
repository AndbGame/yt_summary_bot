"""Очереди и воркеры: единственный LLM-воркер + фоновое обновление авторов.

Один LLM-воркер — не более одного LLM-запроса одновременно (провайдер лимитирует,
см. AGENTS.md); исключение в задаче воркер не убивает. Фоновый воркер обновляет
устаревшие обзоры авторов: Discord не трогает, сбои логирует и живёт дальше.
"""

import asyncio
import json
import logging

from bot.analysis.author import AuthorRef, facts_to_dict
from bot.analysis.prompts import load_prompts
from bot.pipeline import Deps, _author_ttl_seconds, llm_call, process_job

log = logging.getLogger("yt-summary")


def create_queue() -> asyncio.Queue:
    """Очередь пользовательских задач; создаётся в build_deps и передаётся через Deps."""
    return asyncio.Queue()


def create_background_queue() -> asyncio.Queue:
    """Очередь фоновых задач обновления обзоров авторов; создаётся в build_deps."""
    return asyncio.Queue()


async def llm_worker(queue: asyncio.Queue, deps: Deps) -> None:
    """Единственный потребитель очереди: LLM-запросы строго по одному."""
    while True:
        job = await queue.get()
        try:
            await process_job(deps, job)
        except Exception:
            log.exception("задача %s упала", job.get("video_id"))
        finally:
            queue.task_done()


async def author_refresh_worker(queue: asyncio.Queue, deps: Deps) -> None:
    """Потребитель фоновой очереди: {"platform", "handle"} -> факты -> обзор -> кеш.

    Discord не трогается совсем — обновление незаметно для пользователя.
    Исключение на одной задаче воркер не убивает: следующая обработается штатно.
    """
    while True:
        task = await queue.get()
        try:
            await _refresh_author(deps, task)
        except Exception:
            log.exception("фоновое обновление автора %s упало", task)
        finally:
            queue.task_done()


async def _refresh_author(deps: Deps, task: dict) -> None:
    provider = getattr(deps, "author_facts", None)
    authors = getattr(deps, "authors", None)
    if provider is None or authors is None:
        return
    platform = task.get("platform")
    handle = task.get("handle")
    if not platform or not handle:
        return
    # перечитываем строку перед работой: пока задача ждала в очереди, другой
    # джобой/фоновым воркером она могла быть уже обновлена — дублируемый LLM-
    # вызов не нужен, задача просто рассасывается
    row = authors.get(platform, handle)
    if authors.is_fresh(row, _author_ttl_seconds(deps)):
        log.info("фоновое обновление: обзор %s/%s уже свежий", platform, handle)
        return
    ref = AuthorRef(platform=platform, handle=handle)
    facts = await asyncio.to_thread(provider.get_channel, ref)
    if facts is None:
        # канал недоступен или сеть легла: строка остаётся как есть до следующего HIT
        log.info("фоновое обновление: факты %s/%s недоступны", platform, handle)
        return
    review = await llm_call(deps, "review_author", facts)
    prompts = deps.prompts
    if prompts is None:
        prompts = load_prompts()
    authors.put(
        platform,
        handle,
        getattr(facts, "name", "") or "",
        json.dumps(facts_to_dict(facts), ensure_ascii=False),
        review,
        str(prompts.get("version", "0")),
    )
