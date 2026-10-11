"""Дедуп сообщений в БД (фаза 4): in-memory set заменён таблицей (ARCHITECTURE.md §7, §10).

Ключ — message_id (PRIMARY KEY processed_messages): повторная обработка того же
сообщения (редакты, повторная доставка события после рестарта) отбрасывается,
причём независимо от canonical_id — дедуп по сообщению, а не по ссылке. Та же
ссылка в ДРУГОМ сообщении разбирается штатно. mark даёт True только при первой
вставке (INSERT OR IGNORE + rowcount) — персистентно переживает перезапуск.
"""

import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # только для аннотаций: Store импортирует репозитории, обратный импорт — цикл
    from bot.store.db import Store


class DedupStore:
    """Фасад над таблицей processed_messages; соединением владеет Store."""

    def __init__(self, db: "Store"):
        self.db = db

    def mark(self, message_id, canonical_id: str) -> bool:
        """Отмечает сообщение обработанным; True — только при первой вставке."""
        with self.db._txn() as conn:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO processed_messages (message_id, canonical_id, created_at) "
                "VALUES (?, ?, ?)",
                (str(message_id), canonical_id, int(time.time())),
            )
            return cursor.rowcount == 1
