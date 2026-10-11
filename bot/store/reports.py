"""Кеш отчётов в БД: ключ (canonical_id, prompts_ver, schema_ver, content_hash).

Смена промптов, схемы отчёта или контента источника меняет ключ — старые записи
инвалидируются детерминированно, ручной сброс кеша не нужен (ARCHITECTURE.md §6).
PK остаётся тройкой (шаг миграции 2 — только ADD COLUMN): content_hash — обычная
колонка сравнения в WHERE, запись с новым хэшем замещает прежнюю.

get_latest — фолбэк «субтитры недоступны»: последний payload по тройке без
учёта content_hash (свежий хэш посчитать не из чего).
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

    def get(
        self, canonical_id: str, prompts_ver: str, schema_ver: str, content_hash: str | None = None
    ) -> str | None:
        """Отчёт по полному ключу или None (промах кеша).

        content_hash=None — «хэш не указан»: совпадает только с записями без
        хэша (NULL после легаси-миграции и "" от put по умолчанию), но не с
        конкретным чужим хэшем — иначе фазовые записи путались бы с новыми.
        """
        if content_hash is None:
            rows = self.db._query(
                "SELECT payload FROM reports "
                "WHERE canonical_id = ? AND prompts_ver = ? AND schema_ver = ? "
                "AND (content_hash IS NULL OR content_hash = '')",
                (canonical_id, prompts_ver, schema_ver),
            )
        else:
            rows = self.db._query(
                "SELECT payload FROM reports "
                "WHERE canonical_id = ? AND prompts_ver = ? AND schema_ver = ? "
                "AND content_hash = ?",
                (canonical_id, prompts_ver, schema_ver, content_hash),
            )
        return rows[0][0] if rows else None

    def get_latest(self, canonical_id: str, prompts_ver: str, schema_ver: str) -> str | None:
        """Payload последней записи по created_at в тройке ключей (любой content_hash).

        Нужен фолбэку пайплайна: субтитры могли стать недоступны — свежий
        content_hash посчитать не из чего, поэтому берём последний разбор этого
        видео по тройке, каким бы ни был хэш, под которым он записан.
        """
        rows = self.db._query(
            "SELECT payload FROM reports "
            "WHERE canonical_id = ? AND prompts_ver = ? AND schema_ver = ? "
            "ORDER BY created_at DESC LIMIT 1",
            (canonical_id, prompts_ver, schema_ver),
        )
        return rows[0][0] if rows else None

    def put(
        self, canonical_id: str, prompts_ver: str, schema_ver: str, payload: str, content_hash: str = ""
    ) -> None:
        """Кладёт отчёт; повторная запись по тому же ключу — перезапись.

        INSERT OR REPLACE: PK — тройка ключей, так что отчёт с новым content_hash
        замещает прежний (старый хэш становится промахом, дублей нет); created_at
        фиксирует момент последнего разбора. Дефолт хэша "" — совместимость с
        вызовами фазы 1 без content_hash.
        """
        self.db._write(
            "INSERT OR REPLACE INTO reports "
            "(canonical_id, prompts_ver, schema_ver, payload, created_at, content_hash) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (canonical_id, prompts_ver, schema_ver, payload, int(time.time()), content_hash),
        )
