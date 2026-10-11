"""Юнит-тесты JobStore (фаза 4, bot/store/jobs.py): джобы в БД переживают рестарт.

Контракт: JobStore(db) — фасад над таблицей jobs (как ReportStore/DbOutbox):
- put(payload) -> int: пишет payload_json, state='new', возвращает id;
- take_new() -> list[dict]: строки state='new' в порядке FIFO по id, форма —
  payload + job_id (state/error наружу не протекают); чтение состояние не меняет;
- mark_done(id) / mark_failed(id, error): исход джобы фиксируется в БД
  (failed-строки хранят текст ошибки); take_new/count_new их больше не считают;
- данные переживают переоткрытие файла (перезапуск процесса).
Схема и миграция проверяются SQL из отдельного соединения (см. tests/unit/test_store.py).
"""

import json
import sqlite3

from bot.store.db import Store


def _sql(db_path, statement, params=()):
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute(statement, params).fetchall()
    finally:
        conn.close()


def _job_store(tmp_path):
    from bot.store.jobs import JobStore

    path = str(tmp_path / "jobs.sqlite3")
    store = Store(path)
    return store, JobStore(store), path


class TestPut:
    def test_put_returns_monotonic_ids(self, tmp_path):
        store, jobs, path = _job_store(tmp_path)
        try:
            first = jobs.put({"kind": "video", "url": "u1"})
            second = jobs.put({"kind": "video", "url": "u2"})
            assert isinstance(first, int) and isinstance(second, int)
            assert second == first + 1
        finally:
            store.close()

    def test_put_stores_state_new_and_created_at(self, tmp_path):
        store, jobs, path = _job_store(tmp_path)
        try:
            job_id = jobs.put({"kind": "video"})
            state, created_at = _sql(path, "SELECT state, created_at FROM jobs WHERE id = ?", (job_id,))[0]
            assert state == "new"
            assert created_at is not None
        finally:
            store.close()

    def test_payload_json_is_json_of_payload(self, tmp_path):
        store, jobs, path = _job_store(tmp_path)
        try:
            payload = {"kind": "video", "url": "u", "thread_id": 5}
            job_id = jobs.put(payload)
            raw = _sql(path, "SELECT payload_json FROM jobs WHERE id = ?", (job_id,))[0][0]
            assert json.loads(raw) == payload
        finally:
            store.close()


class TestTakeNew:
    def test_take_new_unpacks_payload_with_job_id(self, tmp_path):
        store, jobs, _ = _job_store(tmp_path)
        try:
            payload = {"kind": "video", "url": "https://x", "thread_id": 5, "message_id": 7}
            job_id = jobs.put(payload)

            rows = jobs.take_new()

            assert rows == [{**payload, "job_id": job_id}], "payload распакован, id добавлен"
        finally:
            store.close()

    def test_take_new_fifo_by_id(self, tmp_path):
        store, jobs, _ = _job_store(tmp_path)
        try:
            jobs.put({"kind": "video", "url": "первая"})
            jobs.put({"kind": "video", "url": "вторая"})
            jobs.put({"kind": "video", "url": "третья"})

            assert [r["url"] for r in jobs.take_new()] == ["первая", "вторая", "третья"]
        finally:
            store.close()

    def test_take_new_does_not_mutate_state(self, tmp_path):
        # чтение: повторный вызов видит те же строки — метки исхода ставят
        # только mark_done/mark_failed
        store, jobs, _ = _job_store(tmp_path)
        try:
            jobs.put({"kind": "video", "url": "u"})

            first = jobs.take_new()
            second = jobs.take_new()

            assert first == second
            assert jobs.count_new() == 1
        finally:
            store.close()

    def test_take_new_skips_done_and_failed(self, tmp_path):
        store, jobs, _ = _job_store(tmp_path)
        try:
            done_id = jobs.put({"kind": "video", "url": "готова"})
            failed_id = jobs.put({"kind": "video", "url": "упала"})
            new_id = jobs.put({"kind": "video", "url": "новая"})
            jobs.mark_done(done_id)
            jobs.mark_failed(failed_id, "нет субтитров")

            rows = jobs.take_new()

            assert [r["job_id"] for r in rows] == [new_id]
        finally:
            store.close()


class TestMarkDone:
    def test_mark_done_removes_from_new(self, tmp_path):
        store, jobs, path = _job_store(tmp_path)
        try:
            job_id = jobs.put({"kind": "video", "url": "u"})

            jobs.mark_done(job_id)

            assert jobs.take_new() == []
            assert jobs.count_new() == 0
            state = _sql(path, "SELECT state FROM jobs WHERE id = ?", (job_id,))[0][0]
            assert state == "done"
        finally:
            store.close()


class TestMarkFailed:
    def test_mark_failed_sets_state_and_error(self, tmp_path):
        store, jobs, path = _job_store(tmp_path)
        try:
            job_id = jobs.put({"kind": "video", "url": "u"})

            jobs.mark_failed(job_id, "нет доступных субтитров")

            assert jobs.take_new() == []
            state, error = _sql(path, "SELECT state, error FROM jobs WHERE id = ?", (job_id,))[0]
            assert state == "failed"
            assert error == "нет доступных субтитров"
        finally:
            store.close()


class TestCountNew:
    def test_count_new_counts_only_new(self, tmp_path):
        store, jobs, _ = _job_store(tmp_path)
        try:
            assert jobs.count_new() == 0
            done_id = jobs.put({"kind": "video"})
            jobs.put({"kind": "video"})
            jobs.mark_done(done_id)

            assert jobs.count_new() == 1
        finally:
            store.close()


class TestPersistence:
    def test_unicode_payload_roundtrip(self, tmp_path):
        store, jobs, _ = _job_store(tmp_path)
        try:
            payload = {
                "kind": "video",
                "url": "https://youtu.be/abc",
                "status_text": "привет 🎬 \"кавычки\" \n перенос",
            }
            job_id = jobs.put(payload)
            assert jobs.take_new() == [{**payload, "job_id": job_id}]
        finally:
            store.close()

    def test_jobs_survive_reopen(self, tmp_path):
        from bot.store.jobs import JobStore

        path = str(tmp_path / "jobs.sqlite3")
        store = Store(path)
        payload = {"kind": "video", "url": "u", "thread_id": 1}
        job_id = JobStore(store).put(payload)
        store.close()

        reopened = Store(path)
        try:
            rows = JobStore(reopened).take_new()
            assert rows == [{**payload, "job_id": job_id}], "джоба пережила «перезапуск»"
        finally:
            reopened.close()
