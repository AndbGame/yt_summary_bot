"""Кеш обзоров авторов в БД: ключ (platform, handle), свежесть по analyzed_at.

Один канал = много видео, поэтому обзор автора считается один раз и живёт с TTL
(ARCHITECTURE.md §7): свежая строка переиспользуется без LLM, устаревшая
обновляется фоном (см. bot.jobs.author_refresh_worker). facts_json хранит факты
отдельно от обзора — повторные LLM-разборы не требуют повторного похода в сеть.
"""

import time

_COLUMNS = (
    "platform",
    "handle",
    "display_name",
    "facts_json",
    "review_json",
    "prompts_ver",
    "analyzed_at",
    "refreshed_at",
)


class AuthorStore:
    """Фасад над таблицей authors; соединением владеет Store."""

    def __init__(self, db):
        self.db = db

    def get(self, platform: str, handle: str) -> dict | None:
        """Строка автора всеми восемью колонками или None (промах кеша)."""
        rows = self.db._query(
            f"SELECT {', '.join(_COLUMNS)} FROM authors WHERE platform = ? AND handle = ?",
            (platform, handle),
        )
        return dict(zip(_COLUMNS, rows[0], strict=False)) if rows else None

    def put(
        self,
        platform: str,
        handle: str,
        display_name: str,
        facts_json: str,
        review_json: str,
        prompts_ver: str,
    ) -> None:
        """Пишет факты и обзор, штампуя оба времени моментом записи.

        INSERT OR REPLACE по составному PK: повторный разбор того же автора
        замещает прежнюю запись, дублей нет.
        """
        now = int(time.time())
        self.db._write(
            f"INSERT OR REPLACE INTO authors ({', '.join(_COLUMNS)}) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (platform, handle, display_name, facts_json, review_json, prompts_ver, now, now),
        )

    def is_fresh(self, row: dict | None, ttl_seconds: int, now: int | None = None) -> bool:
        """Свежесть по analyzed_at: возраст строго меньше TTL.

        Граница «ровно TTL» трактуется как устаревшая — консервативнее для
        обновления и неважно для пользователей.
        """
        if row is None:
            return False
        current = int(time.time()) if now is None else now
        return current - row["analyzed_at"] < ttl_seconds
