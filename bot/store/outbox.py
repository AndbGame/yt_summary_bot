"""Outbox в БД — буфер недоставленных сообщений (замена pending_replies.json).

Тот же интерфейс load/save/append, что у PendingOutbox: SafeSender и пайплайн
не замечают подмены (общие поведения — tests/unit/test_store_outbox_compat.py).
Порядок — FIFO по монотонному id (AUTOINCREMENT).
"""

import logging
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # только для аннотаций: Store импортирует репозитории, обратный импорт — цикл
    from bot.store.db import Store

log = logging.getLogger("yt-summary")


class DbOutbox:
    """Фасад над таблицей outbox; .db — открытая ссылка на Store-владелец соединения."""

    def __init__(self, db: "Store"):
        self.db = db

    def load(self) -> list:
        """Все недоставленные сообщения в порядке отправки (FIFO по id)."""
        rows = self.db._query("SELECT thread_id, content FROM outbox ORDER BY id")
        return [{"thread_id": int(t), "content": c} for t, c in rows]

    def save(self, items: list) -> None:
        """Заменяет содержимое буфера целиком (семантика flush_pending: «остаток»).

        Один DELETE + все INSERT в одной транзакции: между ними буфер не может
        быть замечен пустым или наполовину заполненным.
        """
        with self.db._txn() as conn:
            conn.execute("DELETE FROM outbox")
            conn.executemany(
                "INSERT INTO outbox (thread_id, content, created_at) VALUES (?, ?, ?)",
                [(int(i["thread_id"]), i["content"], int(time.time())) for i in items],
            )

    def append(self, thread_id: int, content: str) -> None:
        """Добавляет сообщение в конец очереди (запись сразу закоммичена)."""
        self.db._write(
            "INSERT INTO outbox (thread_id, content, created_at) VALUES (?, ?, ?)",
            (int(thread_id), content, int(time.time())),
        )
        total = self.db._query("SELECT COUNT(*) FROM outbox")[0][0]
        log.warning("ответ буферизован (%d всего): тред %s", total, thread_id)
