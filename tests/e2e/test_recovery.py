"""E2E фазы 4: джобы в БД переживают рестарт (docs/ARCHITECTURE.md §10).

Сценарии «падения процесса»:
- джоба записана в БД (state=new), но не обработана — recover_jobs находит её и
  ставит в очередь, статусное сообщение прикрепляется через fetch_message;
- обработка воркером переводит джобу в state=done;
- упавшая process_job -> state=failed, текст ошибки записан в БД. ВАЖНАЯ
  семантика ошибки: process_job сначала информирует пользователя (error-
  сообщение в треде), затем пробрасывает исключение — llm_worker помечает джобу
  failed. Пользователь не остаётся с висящим статусом, а упавшая джоба не
  повторяется после рестарта (ошибка уже показана);
- восстановленная джоба обрабатывается до конца — отчёт доставлен в тред.

Всё на настоящем Store во временном файле + фейках Discord/LLM/экстрактора;
сети нет. «Рестарт» эмулируется тем, что джоба попадает в БД напрямую
(deps.jobs.put) и не кладётся в живую очередь.
"""

import asyncio
import sqlite3

import pytest

from bot.jobs import llm_worker
from bot.store.db import Store

VIDEO_ID = "abcdefghijk"
URL = f"https://youtu.be/{VIDEO_ID}"
THREAD_ID = 4242
STATUS_MESSAGE_ID = 888
MESSAGE_ID = 777

PAYLOAD = {
    "kind": "video",
    "url": URL,
    "canonical_id": f"youtube:video:{VIDEO_ID}",
    "thread_id": THREAD_ID,
    "message_id": MESSAGE_ID,
    "status_message_id": STATUS_MESSAGE_ID,
}


def _sql(db_path, statement, params=()):
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute(statement, params).fetchall()
    finally:
        conn.close()


def _payload(extractor):
    return {
        "kind": extractor.name,
        "url": URL,
        "canonical_id": extractor.canonical_id(URL),
        "thread_id": THREAD_ID,
        "message_id": MESSAGE_ID,
        "status_message_id": STATUS_MESSAGE_ID,
    }


def _drain(deps):
    """Прогоняет llm_worker до опустошения очереди и гасит его."""

    async def _scenario():
        worker = asyncio.create_task(llm_worker(deps.queue, deps))
        await asyncio.wait_for(deps.queue.join(), timeout=5)
        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker

    asyncio.run(_scenario())


@pytest.fixture
def store(tmp_path):
    s = Store(str(tmp_path / "recovery.sqlite3"))
    yield s
    s.close()


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "recovery.sqlite3")


class TestRecoverJobs:
    def test_new_job_from_db_enqueued_with_status(
        self, make_deps, make_thread, make_status, store
    ):
        # «процесс умер» сразу после постановки джобы: в БД строка state=new,
        # очередь пуста; recovery достаёт джобу и прикрепляет статус
        from bot.discord_app import recover_jobs

        thread = make_thread(THREAD_ID)
        status = make_status(STATUS_MESSAGE_ID)
        thread.fetchable[STATUS_MESSAGE_ID] = status
        deps = make_deps(thread=thread, jobs=store.jobs, dedup=store.dedup)
        job_id = deps.jobs.put(PAYLOAD)

        asyncio.run(recover_jobs(deps))

        job = asyncio.run(deps.queue.get())
        assert job["job_id"] == job_id
        assert job["kind"] == "video"
        assert job["url"] == URL
        assert job["canonical_id"] == PAYLOAD["canonical_id"]
        assert job["thread_id"] == THREAD_ID
        assert job["message_id"] == MESSAGE_ID
        assert job["status"] is status, "статус прикреплён через fetch_message"
        assert deps.queue.empty()

    def test_unfetchable_status_gives_none_but_job_enqueued(
        self, make_deps, make_thread, store
    ):
        # сообщение статуса исчезло (тред очищен): джоба всё равно ставится,
        # status=None — пайплайн допускает джобу без статуса
        from bot.discord_app import recover_jobs

        deps = make_deps(thread=make_thread(THREAD_ID), jobs=store.jobs, dedup=store.dedup)
        deps.jobs.put(PAYLOAD)

        asyncio.run(recover_jobs(deps))

        job = asyncio.run(deps.queue.get())
        assert job.get("status") is None
        assert job["url"] == URL

    def test_missing_channel_gives_none_but_job_enqueued(
        self, make_deps, store
    ):
        # тред недоступен (клиент его не знает): джоба ставится, статус None —
        # решает пайплайн (тред недоступен -> джоба отбрасывается с логом)
        from bot.discord_app import recover_jobs

        deps = make_deps(jobs=store.jobs, dedup=store.dedup)  # клиент без каналов
        deps.jobs.put(PAYLOAD)

        asyncio.run(recover_jobs(deps))

        job = asyncio.run(deps.queue.get())
        assert job.get("status") is None
        assert deps.queue.empty()

    def test_recover_takes_only_new_jobs(self, make_deps, make_thread, store):
        # обработанные и упавшие джобы восстановлению не подлежат
        from bot.discord_app import recover_jobs

        deps = make_deps(thread=make_thread(THREAD_ID), jobs=store.jobs, dedup=store.dedup)
        done_id = deps.jobs.put(PAYLOAD)
        deps.jobs.mark_done(done_id)
        failed_id = deps.jobs.put(PAYLOAD)
        deps.jobs.mark_failed(failed_id, "нет субтитров")

        asyncio.run(recover_jobs(deps))

        assert deps.queue.empty(), "только state=new возвращаются в очередь"


class TestJobLifecycleInDb:
    def test_processed_job_marked_done(
        self, make_deps, make_thread, make_llm, make_extractor, make_router, store, db_path
    ):
        thread = make_thread(THREAD_ID)
        extractor = make_extractor(transcripts={URL: "субтитры"})
        deps = make_deps(
            thread=thread,
            llm=make_llm(),
            reports=store.reports,
            jobs=store.jobs,
            dedup=store.dedup,
            router=make_router([extractor]),
        )
        payload = _payload(extractor)
        job_id = deps.jobs.put(payload)
        deps.queue.put_nowait({"job_id": job_id, **payload})

        _drain(deps)

        assert deps.jobs.count_new() == 0
        assert deps.jobs.take_new() == []
        state = _sql(db_path, "SELECT state FROM jobs WHERE id = ?", (job_id,))[0][0]
        assert state == "done"
        assert thread.sent and "**Кратко**" in thread.sent[0], "отчёт доставлен"

    def test_failed_extraction_marked_failed_with_error(
        self, make_deps, make_thread, make_llm, make_extractor, make_router, store, db_path
    ):
        # экстрактор не добыл контент: пользователь получает error-сообщение,
        # джоба помечается failed, текст ошибки записан в БД
        thread = make_thread(THREAD_ID)
        extractor = make_extractor(extract_error=RuntimeError("нет доступных субтитров"))
        deps = make_deps(
            thread=thread,
            llm=make_llm(),
            reports=store.reports,
            jobs=store.jobs,
            dedup=store.dedup,
            router=make_router([extractor]),
        )
        payload = _payload(extractor)
        job_id = deps.jobs.put(payload)
        deps.queue.put_nowait({"job_id": job_id, **payload})

        _drain(deps)

        state, error = _sql(db_path, "SELECT state, error FROM jobs WHERE id = ?", (job_id,))[0]
        assert state == "failed"
        assert error is not None and "нет доступных субтитров" in error, "ошибка записана"
        assert deps.jobs.count_new() == 0
        assert deps.jobs.take_new() == []
        # пользователь информирован: error-путь сработал до mark_failed
        assert any("нет доступных субтитров" in c for c in thread.sent)

    def test_recovered_job_delivers_report_after_restart(
        self, make_deps, make_thread, make_status, make_llm, make_extractor, make_router, store
    ):
        # полный цикл после «рестарта»: джоба в БД -> recovery -> воркер -> тред
        from bot.discord_app import recover_jobs

        thread = make_thread(THREAD_ID)
        status = make_status(STATUS_MESSAGE_ID)
        thread.fetchable[STATUS_MESSAGE_ID] = status
        extractor = make_extractor(transcripts={URL: "субтитры"})
        deps = make_deps(
            thread=thread,
            llm=make_llm(),
            reports=store.reports,
            jobs=store.jobs,
            dedup=store.dedup,
            router=make_router([extractor]),
        )
        payload = _payload(extractor)
        deps.jobs.put(payload)

        asyncio.run(recover_jobs(deps))
        job = asyncio.run(deps.queue.get())
        assert job["status"] is status
        deps.queue.task_done()  # get() выше обязан быть закрыт task_done, как сделал бы воркер
        deps.queue.put_nowait(job)  # вернули в очередь (как воркер бы взял)

        _drain(deps)

        assert thread.sent, "восстановленная джоба обработана"
        assert thread.sent[0].startswith(f"**Разбор: {URL}**"), "отчёт доставлен в тред"
        assert status.deleted, "статусное сообщение закрыто"
        assert deps.jobs.count_new() == 0, "джоба завершена, повторного восстановления нет"
