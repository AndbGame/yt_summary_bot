"""Юнит-тесты AuthorStore (фаза 3, bot/store/authors.py) и миграции шага 3.

Контракт (docs/ARCHITECTURE.md §7, фаза 3): Store открывает репозиторий
store.authors; таблица authors с составным PK (platform, handle); put(...) пишет
факты/обзор и штампует analyzed_at = refreshed_at = now; get возвращает dict со
всеми восемью колонками или None; is_fresh(row, ttl_seconds, now=None) — свежесть
по analyzed_at.

Форма is_fresh («метод или статикметод») оставлена решению реализатора: тесты
зовут её через инстанс (store.authors.is_fresh(...)) — это работает в обоих
вариантах. Граница на равенстве (возраст ровно ttl) сознательно не фиксируется:
проверяются ttl-1 (свежий) и ttl+1 (устаревший), семантика «<» или «<=» — на
выбор реализации. Схема проверяется прямым SQL из отдельного соединения — как
внешний наблюдатель (тот же подход, что в tests/unit/test_store.py). Прямой
импорт AuthorStore — только там, где проверяется сам класс (лениво: TDD, как в
test_pipeline_cache.py): остальное работает через store.authors без импорта.
"""

import sqlite3

from bot.store.db import Store


def _sql(db_path, statement):
    """Выполняет SQL через отдельное соединение — читает файл БД как внешний наблюдатель."""
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute(statement).fetchall()
    finally:
        conn.close()


def _make_store(tmp_path):
    return Store(str(tmp_path / "db.sqlite3"))


class TestSchema:
    """Таблица authors — шаг миграции 3 (свежая БД сразу с таблицей)."""

    def test_fresh_db_creates_authors_table(self, tmp_path):
        path = str(tmp_path / "db.sqlite3")
        store = Store(path)
        store.close()

        rows = _sql(path, "PRAGMA table_info(authors)")
        assert [r[1] for r in rows] == [
            "platform",
            "handle",
            "display_name",
            "facts_json",
            "review_json",
            "prompts_ver",
            "analyzed_at",
            "refreshed_at",
        ]
        for idx in range(2, 6):
            assert "TEXT" in rows[idx][2].upper()
        assert "INT" in rows[6][2].upper() and "INT" in rows[7][2].upper()
        # platform/handle — NOT NULL: составной ключ не бывает пустым
        assert rows[0][3] == 1 and rows[1][3] == 1
        # составной первичный ключ ровно из двух колонок, в объявленном порядке
        pk = [r[1] for r in sorted((r for r in rows if r[5] > 0), key=lambda r: r[5])]
        assert pk == ["platform", "handle"]
        # шаг миграции 3 применён: версия схемы БД поднята
        assert _sql(path, "PRAGMA user_version")[0][0] >= 3

    def test_v2_database_migrates_to_v3_on_open(self, tmp_path):
        """БД фазы 2 (user_version=2) мигрирует при открытии новым Store: появляется
        таблица authors, прежние данные (reports/outbox) остаются читаемыми."""
        path = str(tmp_path / "legacy.sqlite3")
        conn = sqlite3.connect(path)
        try:
            conn.executescript(
                """
                CREATE TABLE reports (
                    canonical_id TEXT NOT NULL,
                    prompts_ver  TEXT NOT NULL,
                    schema_ver   TEXT NOT NULL,
                    payload      TEXT NOT NULL,
                    created_at   INTEGER NOT NULL,
                    content_hash TEXT,
                    PRIMARY KEY (canonical_id, prompts_ver, schema_ver)
                );
                CREATE TABLE outbox (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    thread_id  INTEGER NOT NULL,
                    content    TEXT NOT NULL,
                    created_at INTEGER NOT NULL
                );
                """
            )
            conn.execute(
                "INSERT INTO reports VALUES "
                "('youtube:video:old', '2', 'blocks-1', 'легаси-отчёт', 1700000000, 'hash1')"
            )
            conn.execute("PRAGMA user_version = 2")
            conn.commit()
        finally:
            conn.close()

        store = Store(path)
        try:
            assert _sql(path, "PRAGMA user_version")[0][0] >= 3, "применён шаг миграции 3"
            assert _sql(path, "SELECT COUNT(*) FROM authors")[0][0] == 0
            assert store.reports.get("youtube:video:old", "2", "blocks-1", "hash1") == "легаси-отчёт"
            # репозиторий сразу работоспособен на мигрированной БД
            store.authors.put("youtube", "UC1", "Имя", "{}", "обзор", "2")
            assert store.authors.get("youtube", "UC1") is not None
        finally:
            store.close()


class TestAuthorStoreRepository:
    def test_store_exposes_authors_repository(self, tmp_path):
        from bot.store.authors import AuthorStore  # лениво: TDD

        store = _make_store(tmp_path)
        try:
            assert isinstance(store.authors, AuthorStore)
        finally:
            store.close()

    def test_get_miss_returns_none(self, tmp_path):
        store = _make_store(tmp_path)
        try:
            assert store.authors.get("youtube", "UC404") is None
        finally:
            store.close()

    def test_put_get_roundtrip(self, tmp_path):
        store = _make_store(tmp_path)
        try:
            store.authors.put("youtube", "UC123", "Имя Канала", '{"name": "Имя Канала"}', "Текст обзора", "2")

            row = store.authors.get("youtube", "UC123")

            assert row is not None
            assert set(row) == {
                "platform",
                "handle",
                "display_name",
                "facts_json",
                "review_json",
                "prompts_ver",
                "analyzed_at",
                "refreshed_at",
            }
            assert row["platform"] == "youtube"
            assert row["handle"] == "UC123"
            assert row["display_name"] == "Имя Канала"
            assert row["facts_json"] == '{"name": "Имя Канала"}'
            assert row["review_json"] == "Текст обзора"
            assert row["prompts_ver"] == "2"
            # put штампует оба времени моментом записи
            assert isinstance(row["analyzed_at"], int) and row["analyzed_at"] > 0
            assert isinstance(row["refreshed_at"], int) and row["refreshed_at"] > 0
        finally:
            store.close()

    def test_put_overwrites_same_primary_key(self, tmp_path):
        path = str(tmp_path / "db.sqlite3")
        store = Store(path)
        try:
            store.authors.put("youtube", "UC123", "Имя", "{}", "старый обзор", "2")
            store.authors.put("youtube", "UC123", "Имя", "{}", "новый обзор", "2")
            assert store.authors.get("youtube", "UC123")["review_json"] == "новый обзор"
        finally:
            store.close()
        # перезапись, а не вторая строка: по PK ровно одна запись
        assert _sql(path, "SELECT COUNT(*) FROM authors")[0][0] == 1

    def test_authors_are_isolated_by_platform_and_handle(self, tmp_path):
        store = _make_store(tmp_path)
        try:
            store.authors.put("youtube", "UC1", "А", "{}", "обзор А", "2")
            store.authors.put("youtube", "UC2", "Б", "{}", "обзор Б", "2")
            store.authors.put("vk", "UC1", "В", "{}", "обзор В", "2")

            assert store.authors.get("youtube", "UC1")["review_json"] == "обзор А"
            assert store.authors.get("youtube", "UC2")["review_json"] == "обзор Б"
            assert store.authors.get("vk", "UC1")["review_json"] == "обзор В"
        finally:
            store.close()

    def test_close_then_reopen_keeps_authors(self, tmp_path):
        # зафиксированное поведение close(): повторное открытие файла видит всё записанное
        path = str(tmp_path / "db.sqlite3")
        store = Store(path)
        store.authors.put("youtube", "UC123", "Имя", "{}", "обзор 🎬", "2")
        store.close()

        reopened = Store(path)
        try:
            assert reopened.authors.get("youtube", "UC123")["review_json"] == "обзор 🎬"
        finally:
            reopened.close()


class TestIsFresh:
    """Свежесть по analyzed_at против ttl_seconds; now подаётся явно — без ожиданий."""

    TTL = 1000

    def _row(self, store):
        store.authors.put("youtube", "UC1", "Имя", "{}", "обзор", "2")
        return store.authors.get("youtube", "UC1")

    def test_just_analyzed_row_is_fresh(self, tmp_path):
        store = _make_store(tmp_path)
        try:
            row = self._row(store)
            assert store.authors.is_fresh(row, 90 * 86400), "только что записанная строка свежая"
        finally:
            store.close()

    def test_exactly_now_is_fresh(self, tmp_path):
        store = _make_store(tmp_path)
        try:
            row = self._row(store)
            assert store.authors.is_fresh(row, self.TTL, now=row["analyzed_at"])
        finally:
            store.close()

    def test_within_ttl_is_fresh(self, tmp_path):
        store = _make_store(tmp_path)
        try:
            row = self._row(store)
            assert store.authors.is_fresh(row, self.TTL, now=row["analyzed_at"] + self.TTL - 1)
        finally:
            store.close()

    def test_beyond_ttl_is_stale(self, tmp_path):
        store = _make_store(tmp_path)
        try:
            row = self._row(store)
            assert not store.authors.is_fresh(row, self.TTL, now=row["analyzed_at"] + self.TTL + 1)
        finally:
            store.close()
