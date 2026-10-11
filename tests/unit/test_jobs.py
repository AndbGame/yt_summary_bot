"""Юнит-тесты очереди задач: create_queue и единственный llm_worker.

Фаза 4: джоба в очереди несёт job_id — исход фиксируется в deps.jobs
(mark_done после успешной process_job, mark_failed(id, ошибка) при исключении);
исключение в задаче воркер не убивает (поведение монолита сохранено).
"""

import asyncio

import pytest

from bot import jobs


def test_create_queue_returns_asyncio_queue():
    assert isinstance(jobs.create_queue(), asyncio.Queue)


def test_llm_worker_processes_all_jobs_and_survives_errors(make_deps, make_job_store, monkeypatch):
    processed = []

    async def _fake_process_job(deps, job):
        processed.append(job["n"])
        if job["n"] == 1:
            raise RuntimeError("упс в задаче")

    # подменяем в обоих модулях: независимо от того, как jobs импортирует process_job
    monkeypatch.setattr("bot.jobs.process_job", _fake_process_job, raising=False)
    monkeypatch.setattr("bot.pipeline.process_job", _fake_process_job, raising=False)

    deps = make_deps(jobs=make_job_store())

    async def _scenario():
        queue = jobs.create_queue()
        await queue.put({"n": 1, "job_id": 101})
        await queue.put({"n": 2, "job_id": 102})
        worker = asyncio.create_task(jobs.llm_worker(queue, deps))
        # join завершится только если worker вызвал task_done() для обеих задач
        await asyncio.wait_for(queue.join(), timeout=5)
        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker

    asyncio.run(_scenario())
    assert processed == [1, 2]
    # фаза 4: исход каждой джобы отмечен в deps.jobs по job_id
    assert deps.jobs.done_calls == [102]
    assert [job_id for job_id, _ in deps.jobs.failed_calls] == [101]
    assert any("упс" in error for _, error in deps.jobs.failed_calls), (
        "текст ошибки записан в джобу"
    )
