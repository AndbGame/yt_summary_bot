"""Юнит-тесты SQLite-хранилища фазы 1 (bot/store): миграции, кеш отчётов, outbox в БД.

Контракт (docs/ARCHITECTURE.md §7, §12 фаза 1): Store владеет одним соединением
(check_same_thread=False + внутренняя блокировка записи), миграции применяются
идемпотентно через PRAGMA user_version, журнал WAL. Схема проверяется прямыми
SQL-запросами к файлу БД из отдельного соединения — как внешний наблюдатель,
независимо от внутренних деталей реализации.

Поведение close() зафиксировано так: close() завершает работу чисто, а повторное
открытие того же файла новым Store видит все записанные данные (переживает
«перезапуск» процесса). Использовать закрытый Store контракт не требует.
"""

import sqlite3
import threading

from bot.store.db import Store
from bot.store.outbox import DbOutbox
from bot.store.reports import ReportStore


def _sql(db_path, statement):
    """Выполняет SQL через отдельное соединение — читает файл БД как внешний наблюдатель."""
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute(statement).fetchall()
    finally:
        conn.close()


class TestSchema:
    """Схема обеих таблиц зафиксирована контрактом фазы 1."""

    def test_reports_table_shape(self, tmp_path):
        path = tmp_path / "db.sqlite3"
        store = Store(path)
        store.close()

        rows = _sql(path, "PRAGMA table_info(reports)")
        assert [r[1] for r in rows] == [
            "canonical_id",
            "prompts_ver",
            "schema_ver",
            "payload",
            "created_at",
        ]
        # типы: TEXT у текстовых колонок, числовая created_at (INT и INTEGER —
        # одна affinity в SQLite, проверяем смысл, а не синоним)
        for row in rows[:4]:
            assert "TEXT" in row[2].upper()
        assert "INT" in rows[4][2].upper()
        # составной первичный ключ ровно из трёх колонок, в объявленном порядке
        pk = [r[1] for r in sorted((r for r in rows if r[5] > 0), key=lambda r: r[5])]
        assert pk == ["canonical_id", "prompts_ver", "schema_ver"]

    def test_outbox_table_shape(self, tmp_path):
        path = tmp_path / "db.sqlite3"
        store = Store(path)
        store.close()

        rows = _sql(path, "PRAGMA table_info(outbox)")
        assert [r[1] for r in rows] == ["id", "thread_id", "content", "created_at"]
        assert "INT" in rows[0][2].upper()  # id
        assert "INT" in rows[1][2].upper()  # thread_id
        assert "TEXT" in rows[2][2].upper()  # content
        assert "INT" in rows[3][2].upper()  # created_at
        # AUTOINCREMENT даёт строгую FIFO по id (монотонные id без переиспользования)
        ddl = _sql(path, "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'outbox'")
        assert "AUTOINCREMENT" in ddl[0][0].upper()

    def test_wal_journal_mode(self, tmp_path):
        # WAL — персистентное свойство файла БД: видно и после закрытия Store
        path = tmp_path / "db.sqlite3"
        store = Store(path)
        store.outbox.append(1, "запись")
        store.close()

        mode = _sql(path, "PRAGMA journal_mode")[0][0]
        assert str(mode).lower() == "wal"


class TestMigrations:
    def test_two_stores_on_same_file_idempotent(self, tmp_path):
        # повторное создание Store на том же файле — миграции применяются
        # идемпотентно: без ошибок, дублей таблиц и потери данных
        path = str(tmp_path / "db.sqlite3")
        s1 = Store(path)
        s1.reports.put("youtube:video:x", "1", "summary-1", "из первого")
        s1.outbox.append(5, "письмо")

        s2 = Store(path)
        assert s2.reports.get("youtube:video:x", "1", "summary-1") == "из первого"
        s2.outbox.append(6, "ответ")
        assert [i["content"] for i in s1.outbox.load()] == ["письмо", "ответ"]

        s3 = Store(path)
        assert s3.reports.get("youtube:video:x", "1", "summary-1") == "из первого"
        s3.close()
        s2.close()
        s1.close()

        # user_version — механизм идемпотентности миграций: применён и больше нуля
        version = _sql(path, "PRAGMA user_version")[0][0]
        assert version >= 1


class TestStore:
    def test_exposes_report_and_outbox_repositories(self, tmp_path):
        store = Store(str(tmp_path / "db.sqlite3"))
        try:
            assert isinstance(store.reports, ReportStore)
            assert isinstance(store.outbox, DbOutbox)
        finally:
            store.close()

    def test_close_then_reopen_keeps_data(self, tmp_path):
        # зафиксированное поведение close(): чистое завершение, повторное
        # открытие файла видит всё записанное (аналог перезапуска бота)
        path = str(tmp_path / "db.sqlite3")
        store = Store(path)
        store.reports.put("youtube:video:abc", "1", "summary-1", "саммари 🎬")
        store.outbox.append(7, "сообщение")
        store.close()

        reopened = Store(path)
        try:
            assert reopened.reports.get("youtube:video:abc", "1", "summary-1") == "саммари 🎬"
            assert [i["content"] for i in reopened.outbox.load()] == ["сообщение"]
        finally:
            reopened.close()


class TestReportStore:
    def test_get_miss_returns_none(self, tmp_path):
        store = Store(str(tmp_path / "db.sqlite3"))
        try:
            assert store.reports.get("youtube:video:nope", "1", "summary-1") is None
        finally:
            store.close()

    def test_put_get_roundtrip(self, tmp_path):
        store = Store(str(tmp_path / "db.sqlite3"))
        try:
            store.reports.put("youtube:video:abc", "1", "summary-1", "**Тезисы**\n- пункт 🎬")
            assert store.reports.get("youtube:video:abc", "1", "summary-1") == "**Тезисы**\n- пункт 🎬"
        finally:
            store.close()

    def test_put_overwrites_same_key(self, tmp_path):
        path = str(tmp_path / "db.sqlite3")
        store = Store(path)
        try:
            store.reports.put("youtube:video:abc", "1", "summary-1", "старое")
            store.reports.put("youtube:video:abc", "1", "summary-1", "новое")
            assert store.reports.get("youtube:video:abc", "1", "summary-1") == "новое"
        finally:
            store.close()
        # перезапись, а не вторая строка: по ключу ровно одна запись
        count = _sql(path, "SELECT COUNT(*) FROM reports")[0][0]
        assert count == 1

    def test_keys_are_isolated(self, tmp_path):
        store = Store(str(tmp_path / "db.sqlite3"))
        try:
            store.reports.put("youtube:video:abc", "1", "summary-1", "саммари")

            assert store.reports.get("youtube:video:abc", "2", "summary-1") is None  # версия промптов
            assert store.reports.get("youtube:video:abc", "1", "summary-2") is None  # схема отчёта
            assert store.reports.get("youtube:video:xyz", "1", "summary-1") is None  # другое видео

            # запись под другим ключом не затирает исходную
            store.reports.put("youtube:video:xyz", "1", "summary-1", "другое видео")
            assert store.reports.get("youtube:video:abc", "1", "summary-1") == "саммари"
        finally:
            store.close()


class TestDbOutbox:
    def test_fresh_outbox_is_empty(self, tmp_path):
        store = Store(str(tmp_path / "db.sqlite3"))
        try:
            assert store.outbox.load() == []
        finally:
            store.close()

    def test_append_load_fifo_roundtrip(self, tmp_path):
        store = Store(str(tmp_path / "db.sqlite3"))
        try:
            store.outbox.append(111, "первое")
            store.outbox.append(222, "второе")
            store.outbox.append(333, "третье")

            items = store.outbox.load()
            assert [(i["thread_id"], i["content"]) for i in items] == [
                (111, "первое"),
                (222, "второе"),
                (333, "третье"),
            ]
            # контракт интерфейса: ровно ключи thread_id:int и content:str,
            # как у PendingOutbox (доп. колонки БД наружу не протекают)
            assert all(type(i["thread_id"]) is int for i in items)
            assert all(type(i["content"]) is str for i in items)
            assert all(set(i) == {"thread_id", "content"} for i in items)
        finally:
            store.close()

    def test_unicode_content(self, tmp_path):
        store = Store(str(tmp_path / "db.sqlite3"))
        try:
            payload = 'привет 🎬 "кавычки" **жирный** \n перенос'
            store.outbox.append(1, payload)
            assert store.outbox.load()[0]["content"] == payload
        finally:
            store.close()

    def test_save_replaces_whole_content(self, tmp_path):
        store = Store(str(tmp_path / "db.sqlite3"))
        try:
            store.outbox.append(1, "старое")
            store.outbox.save([{"thread_id": 2, "content": "новое"}])
            assert store.outbox.load() == [{"thread_id": 2, "content": "новое"}]
            store.outbox.save([])
            assert store.outbox.load() == []
        finally:
            store.close()

    def test_append_after_save_continues_fifo(self, tmp_path):
        store = Store(str(tmp_path / "db.sqlite3"))
        try:
            store.outbox.append(1, "буферизованное")
            # flush_pending сохраняет «остаток»: новые append встают в конец очереди
            store.outbox.save([{"thread_id": 9, "content": "остаток"}])
            store.outbox.append(2, "добавленное позже")
            assert [i["content"] for i in store.outbox.load()] == ["остаток", "добавленное позже"]
        finally:
            store.close()

    def test_parallel_appends_from_threads(self, tmp_path):
        # 10 потоков пишут через ОДИН store.outbox: внутренняя блокировка записи
        # в Store обязана сериализовать append — без исключений и без потерь
        store = Store(str(tmp_path / "db.sqlite3"))
        errors = []

        def _append(i):
            try:
                store.outbox.append(i, f"сообщение {i}")
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=_append, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        try:
            assert errors == []
            items = store.outbox.load()
            assert sorted(i["thread_id"] for i in items) == list(range(10))
            assert sorted(i["content"] for i in items) == sorted(
                f"сообщение {i}" for i in range(10)
            )
        finally:
            store.close()
