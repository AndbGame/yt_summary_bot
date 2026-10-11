"""Очередь задач и единственный LLM-воркер.

Один воркер — не более одного LLM-запроса одновременно (провайдер лимитирует,
см. AGENTS.md); исключение в задаче воркер не убивает.
"""

import asyncio
import logging

from bot.pipeline import Deps, process_job

log = logging.getLogger("yt-summary")


def create_queue() -> asyncio.Queue:
    """Очередь задач; создаётся в build_deps и передаётся через Deps."""
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
