"""Миграция легаси-буфера pending_replies.json в БД (фаза 1).

Зафиксированный контракт: build_deps(config) миграцией НЕ занимается — за неё
отвечает функция migrate_legacy_outbox(store, legacy_path=None) в bot/main.py,
вызываемая из main() после build_deps. legacy_path=None означает продакшен-путь
(pending_replies.json в корне репозитория, константа PENDING_FILE); тесты
передают tmp-пути явно через legacy_path и db_path.

Поведение (порядок важен: парсинг и валидация формы файла -> проверка outbox ->
вливание):
- легаси-записи вливаются в outbox-таблицу ОДНИМ save (атомарная транзакция),
  файл -> pending_replies.json.migrated;
- если в БД уже есть записи — корректный файл переименовывается БЕЗ вливания
  (без дублей);
- битый JSON или не та форма (не list, элементы не dict с thread_id/content) —
  файл -> pending_replies.json.corrupted, в том числе при непустом outbox;
- миграция не падает; файла нет — ничего не делает.
"""

import asyncio
import json

import pytest

from bot.config import BotConfig
from bot.main import migrate_legacy_outbox
from bot.store.db import Store


def _write_legacy(path, items):
    path.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")


def _make_config(tmp_path):
    return BotConfig(bot_token="t", llm_api_key="k", db_path=str(tmp_path / "bot_data.sqlite3"))


def test_legacy_records_imported_and_file_renamed(tmp_path, make_deps, make_client, make_thread):
    legacy = tmp_path / "pending_replies.json"
    _write_legacy(
        legacy,
        [
            {"thread_id": 111, "content": "недоставленное первое"},
            {"thread_id": 222, "content": "недоставленное второе"},
        ],
    )
    config = _make_config(tmp_path)
    store = Store(str(config.db_path))
    try:
        migrate_legacy_outbox(store, legacy_path=legacy)

        assert not legacy.exists()
        assert (tmp_path / "pending_replies.json.migrated").exists()
        items = store.outbox.load()
        assert [i["thread_id"] for i in items] == [111, 222]
        assert [i["content"] for i in items] == ["недоставленное первое", "недоставленное второе"]

        # записи живые: flush_pending доставляет их в треды и опустошает буфер
        t1, t2 = make_thread(111), make_thread(222)
        deps = make_deps(
            client=make_client({111: t1, 222: t2}),
            outbox=store.outbox,
            config=config,
        )
        asyncio.run(deps.sender.flush_pending())
        assert t1.sent == ["недоставленное первое"]
        assert t2.sent == ["недоставленное второе"]
        assert store.outbox.load() == []
    finally:
        store.close()


def test_existing_db_records_not_duplicated(tmp_path):
    # в outbox-таблице уже есть записи (буфер уже писался в БД): корректный
    # легаси-файл не вливается вторично — только переименовывается, дублей нет
    legacy = tmp_path / "pending_replies.json"
    _write_legacy(legacy, [{"thread_id": 999, "content": "старьё"}])
    config = _make_config(tmp_path)
    store = Store(str(config.db_path))
    try:
        store.outbox.append(555, "уже в буфере")

        migrate_legacy_outbox(store, legacy_path=legacy)

        assert not legacy.exists()
        assert (tmp_path / "pending_replies.json.migrated").exists()
        items = store.outbox.load()
        assert [(i["thread_id"], i["content"]) for i in items] == [(555, "уже в буфере")]
    finally:
        store.close()


def test_corrupted_json_renamed_and_bot_starts(tmp_path):
    legacy = tmp_path / "pending_replies.json"
    legacy.write_text("{битый json", encoding="utf-8")
    config = _make_config(tmp_path)
    store = Store(str(config.db_path))
    try:
        # миграция не должна ронять старт бота на битом файле
        migrate_legacy_outbox(store, legacy_path=legacy)

        assert not legacy.exists()
        assert (tmp_path / "pending_replies.json.corrupted").exists()
        assert store.outbox.load() == []
        # бот после этого полноценно работает: буфер в БД можно использовать
        store.outbox.append(1, "новое сообщение")
        assert [i["content"] for i in store.outbox.load()] == ["новое сообщение"]
    finally:
        store.close()


def test_corrupted_json_with_nonempty_outbox_goes_to_corrupted(tmp_path):
    # порядок «сначала парсинг, потом проверка outbox»: битый файл при непустом
    # outbox уходит в .corrupted, а НЕ в .migrated (rename-only ветка — только
    # для корректных файлов); содержимое буфера при этом не трогается
    legacy = tmp_path / "pending_replies.json"
    legacy.write_text("{битый json", encoding="utf-8")
    config = _make_config(tmp_path)
    store = Store(str(config.db_path))
    try:
        store.outbox.append(555, "уже в буфере")

        migrate_legacy_outbox(store, legacy_path=legacy)

        assert not legacy.exists()
        assert (tmp_path / "pending_replies.json.corrupted").exists()
        assert not (tmp_path / "pending_replies.json.migrated").exists()
        assert [(i["thread_id"], i["content"]) for i in store.outbox.load()] == [
            (555, "уже в буфере"),
        ]
    finally:
        store.close()


@pytest.mark.parametrize(
    "payload",
    [
        {"a": 1},  # dict вместо list
        [{"thread_id": "abc"}],  # thread_id не приводится к int
    ],
    ids=["dict-instead-of-list", "thread-id-not-int"],
)
def test_invalid_shape_corrupted_and_bot_starts(tmp_path, payload):
    # валидация формы: файл не той формы уходит в .corrupted ЦЕЛИКОМ (частичного
    # вливания нет), старт-логика (миграция) не падает и ничего не вливает
    legacy = tmp_path / "pending_replies.json"
    _write_legacy(legacy, payload)
    config = _make_config(tmp_path)
    store = Store(str(config.db_path))
    try:
        migrate_legacy_outbox(store, legacy_path=legacy)

        assert not legacy.exists()
        assert (tmp_path / "pending_replies.json.corrupted").exists()
        assert not (tmp_path / "pending_replies.json.migrated").exists()
        assert store.outbox.load() == []
        # бот после этого полноценно работает: буфер в БД можно использовать
        store.outbox.append(1, "новое сообщение")
        assert [i["content"] for i in store.outbox.load()] == ["новое сообщение"]
    finally:
        store.close()


def test_merge_is_single_atomic_save(tmp_path, monkeypatch):
    # контракт атомарности: вливание идёт ОДНИМ save (полный список за раз),
    # а не серией append — append оставлял бы окно, в котором смерть процесса
    # между append'ом и rename теряла бы остаток файла
    legacy = tmp_path / "pending_replies.json"
    _write_legacy(
        legacy,
        [
            {"thread_id": 111, "content": "недоставленное первое"},
            {"thread_id": 222, "content": "недоставленное второе"},
        ],
    )
    config = _make_config(tmp_path)
    store = Store(str(config.db_path))
    try:
        save_calls = []
        original_save = store.outbox.save

        def _spy_save(items):
            save_calls.append(list(items))
            original_save(items)

        monkeypatch.setattr(store.outbox, "save", _spy_save)
        migrate_legacy_outbox(store, legacy_path=legacy)

        assert len(save_calls) == 1
        assert save_calls[0] == [
            {"thread_id": 111, "content": "недоставленное первое"},
            {"thread_id": 222, "content": "недоставленное второе"},
        ]
        assert [i["content"] for i in store.outbox.load()] == [
            "недоставленное первое",
            "недоставленное второе",
        ]
        assert (tmp_path / "pending_replies.json.migrated").exists()
    finally:
        store.close()


def test_no_legacy_file_is_noop(tmp_path):
    config = _make_config(tmp_path)
    store = Store(str(config.db_path))
    try:
        migrate_legacy_outbox(store, legacy_path=tmp_path / "pending_replies.json")

        assert store.outbox.load() == []
        assert list(tmp_path.glob("pending_replies.json*")) == []
    finally:
        store.close()
