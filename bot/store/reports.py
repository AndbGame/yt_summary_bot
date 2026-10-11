"""Кеш отчётов в БД: ключ (canonical_id, prompts_ver, schema_ver).

Смена промптов или схемы отчёта меняет ключ — старые записи инвалидируются
детерминированно, ручной сброс кеша не нужен (ARCHITECTURE.md §6).
"""

import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # только для аннотаций: Store импортирует репозитории, обратный импорт — цикл
    from bot.store.db import Store


class ReportStore:
    """Фасад над таблицей reports; .db — открытая ссылка на Store-владелец соединения.

    Публичный .db нужен ровно одному месту вне пакета — migrate_legacy_outbox
    в bot/main.py достаёт через него Store для миграции легаси-буфера.
    """

    def __init__(self, db: "Store"):
        self.db = db

    def get(self, canonical_id: str, prompts_ver: str, schema_ver: str) -> str | None:
        """Отчёт по полному ключу или None (промах кеша)."""
        rows = self.db._query(
            "SELECT payload FROM reports "
            "WHERE canonical_id = ? AND prompts_ver = ? AND schema_ver = ?",
            (canonical_id, prompts_ver, schema_ver),
        )
        return rows[0][0] if rows else None

    def put(self, canonical_id: str, prompts_ver: str, schema_ver: str, payload: str) -> None:
        """Кладёт отчёт; повторная запись по тому же ключу — перезапись.

        INSERT OR REPLACE: ключ и так однозначно идентифицирует отчёт, дублей
        быть не должно; created_at фиксирует момент последнего разбора.
        """
        self.db._write(
            "INSERT OR REPLACE INTO reports "
            "(canonical_id, prompts_ver, schema_ver, payload, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (canonical_id, prompts_ver, schema_ver, payload, int(time.time())),
        )
