"""Юнит-тесты DedupStore (фаза 4, bot/store/dedup.py): дедуп сообщений в БД.

Контракт: DedupStore(db).mark(message_id, canonical_id) -> bool — True только
при первой вставке (INSERT OR IGNORE), False если сообщение уже обработано.
Ключ — message_id (PRIMARY KEY таблицы processed_messages): повторная отметка
того же сообщения с ДРУГИМ canonical_id всё равно False. Персистентность:
дедуп переживает переоткрытие файла (перезапуск процесса) — in-memory set
фазы 3 заменён таблицей (ARCHITECTURE.md §7, §10).
"""

import sqlite3

from bot.store.db import Store


def _sql(db_path, statement, params=()):
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute(statement, params).fetchall()
    finally:
        conn.close()


def _dedup_store(tmp_path):
    from bot.store.dedup import DedupStore

    path = str(tmp_path / "dedup.sqlite3")
    store = Store(path)
    return store, DedupStore(store), path


class TestMark:
    def test_first_mark_true_second_false(self, tmp_path):
        store, dedup, _ = _dedup_store(tmp_path)
        try:
            assert dedup.mark("2002", "youtube:video:abc") is True
            assert dedup.mark("2002", "youtube:video:abc") is False
        finally:
            store.close()

    def test_same_message_other_canonical_still_false(self, tmp_path):
        # ключ — сообщение: повторная обработка того же сообщения отбрасывается,
        # каким бы ни был canonical
        store, dedup, _ = _dedup_store(tmp_path)
        try:
            assert dedup.mark("2002", "youtube:video:abc") is True
            assert dedup.mark("2002", "article:other") is False
        finally:
            store.close()

    def test_other_message_marked_true(self, tmp_path):
        # та же ссылка в другом сообщении — разбирается (дедуп по message, не по canonical)
        store, dedup, _ = _dedup_store(tmp_path)
        try:
            assert dedup.mark("2001", "youtube:video:abc") is True
            assert dedup.mark("2002", "youtube:video:abc") is True
        finally:
            store.close()

    def test_marks_independent_per_message(self, tmp_path):
        store, dedup, _ = _dedup_store(tmp_path)
        try:
            dedup.mark("1", "c1")
            dedup.mark("2", "c2")
            assert dedup.mark("1", "c1") is False
            assert dedup.mark("2", "c2") is False
            assert dedup.mark("3", "c1") is True
        finally:
            store.close()


class TestRows:
    def test_canonical_id_recorded(self, tmp_path):
        store, dedup, path = _dedup_store(tmp_path)
        try:
            dedup.mark("2002", "youtube:video:abc")
            message_id, canonical_id = _sql(path, "SELECT message_id, canonical_id FROM processed_messages")[0]
            assert message_id == "2002"
            assert canonical_id == "youtube:video:abc"
        finally:
            store.close()

    def test_created_at_recorded(self, tmp_path):
        store, dedup, path = _dedup_store(tmp_path)
        try:
            dedup.mark("2002", "c")
            created_at = _sql(path, "SELECT created_at FROM processed_messages")[0][0]
            assert created_at is not None
        finally:
            store.close()


class TestPersistence:
    def test_dedup_survives_reopen(self, tmp_path):
        from bot.store.dedup import DedupStore

        path = str(tmp_path / "dedup.sqlite3")
        store = Store(path)
        DedupStore(store).mark("2002", "youtube:video:abc")
        store.close()

        reopened = Store(path)
        try:
            # «перезапуск» процесса: сообщение уже обработано — вставки нет
            assert DedupStore(reopened).mark("2002", "youtube:video:abc") is False
        finally:
            reopened.close()
