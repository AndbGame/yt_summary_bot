"""Юнит-тесты очереди задач: create_queue и единственный llm_worker."""

import asyncio

import pytest

from bot import jobs


def test_create_queue_returns_asyncio_queue():
    assert isinstance(jobs.create_queue(), asyncio.Queue)


def test_llm_worker_processes_all_jobs_and_survives_errors(make_deps, monkeypatch):
    # воркер не должен умирать от исключения в задаче (поведение монолита)
    processed = []

    async def _fake_process_job(deps, job):
        processed.append(job["n"])
        if job["n"] == 1:
            raise RuntimeError("упс в задаче")

    # подменяем в обоих модулях: независимо от того, как jobs импортирует process_job
    monkeypatch.setattr("bot.jobs.process_job", _fake_process_job, raising=False)
    monkeypatch.setattr("bot.pipeline.process_job", _fake_process_job, raising=False)

    async def _scenario():
        queue = jobs.create_queue()
        await queue.put({"n": 1})
        await queue.put({"n": 2})
        worker = asyncio.create_task(jobs.llm_worker(queue, make_deps()))
        # join завершится только если worker вызвал task_done() для обеих задач
        await asyncio.wait_for(queue.join(), timeout=5)
        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker

    asyncio.run(_scenario())
    assert processed == [1, 2]
