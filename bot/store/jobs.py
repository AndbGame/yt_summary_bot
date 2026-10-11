"""Джобы в БД (фаза 4): задачи переживают рестарт (ARCHITECTURE.md §10).

JobStore — фасад над таблицей jobs (как ReportStore/DbOutbox): put пишет
payload_json со state='new', take_new отдаёт недоделанные джобы в FIFO по id
(payload + job_id; state/error наружу не протекают), mark_done/mark_failed
фиксируют исход. take_new чтением состояние НЕ меняет — метку исхода ставит
только llm_worker по результату process_job (в т.ч. после recovery: джоба,
поставленная в очередь recover_jobs'ом, остаётся new до реальной обработки).
"""

import json
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # только для аннотаций: Store импортирует репозитории, обратный импорт — цикл
    from bot.store.db import Store


class JobStore:
    """Фасад над таблицей jobs; соединением владеет Store."""

    def __init__(self, db: "Store"):
        self.db = db

    def put(self, payload: dict) -> int:
        """Пишет джобу (state='new') и возвращает её id (монотонный, AUTOINCREMENT)."""
        now = int(time.time())
        with self.db._txn() as conn:
            cursor = conn.execute(
                "INSERT INTO jobs (payload_json, state, created_at, updated_at) "
                "VALUES (?, 'new', ?, ?)",
                (json.dumps(payload, ensure_ascii=False), now, now),
            )
            return cursor.lastrowid

    def take_new(self) -> list[dict]:
        """Недоделанные джобы (state='new') в порядке FIFO по id: payload + job_id."""
        rows = self.db._query(
            "SELECT id, payload_json FROM jobs WHERE state = 'new' ORDER BY id"
        )
        return [{**json.loads(raw), "job_id": job_id} for job_id, raw in rows]

    def mark_done(self, job_id: int) -> None:
        """Джоба обработана — после рестарта не восстанавливается."""
        self.db._write(
            "UPDATE jobs SET state = 'done', updated_at = ? WHERE id = ?",
            (int(time.time()), job_id),
        )

    def mark_failed(self, job_id: int, error: str) -> None:
        """Джоба упала: state='failed', текст ошибки сохранён (для диагностики)."""
        self.db._write(
            "UPDATE jobs SET state = 'failed', error = ?, updated_at = ? WHERE id = ?",
            (error, int(time.time()), job_id),
        )

    def count_new(self) -> int:
        """Сколько джоб ждёт обработки (восстановление после рестарта)."""
        rows = self.db._query("SELECT COUNT(*) FROM jobs WHERE state = 'new'")
        return rows[0][0]
