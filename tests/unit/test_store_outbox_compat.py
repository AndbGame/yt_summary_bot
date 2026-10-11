"""Интерфейсная совместимость буферов: PendingOutbox (JSON) и DbOutbox (SQLite).

Outbox переезжает с JSON-файла в БД (фаза 1) с тем же интерфейсом — SafeSender
и пайплайн не должны заметить подмены. Общий набор поведений гоняется
параметризованно на обеих реализациях.

Сценарии, специфичные для формата хранения, в общий набор не входят: битый
JSON-файл и атомарная запись через .tmp осмысленны только для PendingOutbox
(покрыты в tests/unit/test_outbox.py), к БД они неприменимы.
"""

import pytest


@pytest.fixture(params=["json", "sqlite"])
def make_outbox(request, tmp_path):
    """Фабрика буфера: make_outbox() -> новый экземпляр над тем же хранилищем.

    «json» — PendingOutbox во временном файле; «sqlite» — DbOutbox поверх Store
    во временном файле (несколько экземпляров = несколько соединений, как
    перезапуск процесса). Открытые Store закрываются после теста.
    """
    stores = []

    if request.param == "json":
        from bot.delivery.outbox import PendingOutbox

        def _make():
            return PendingOutbox(tmp_path / "pending.json")

    else:
        from bot.store.db import Store

        def _make():
            store = Store(str(tmp_path / "db.sqlite3"))
            stores.append(store)
            return store.outbox

    yield _make
    for store in stores:
        store.close()


def test_fresh_buffer_is_empty(make_outbox):
    assert make_outbox().load() == []


def test_append_load_roundtrip(make_outbox):
    outbox = make_outbox()
    outbox.append(111, "первое **сообщение**")
    outbox.append(222, "второе")

    items = outbox.load()
    assert [(i["thread_id"], i["content"]) for i in items] == [
        (111, "первое **сообщение**"),
        (222, "второе"),
    ]
    # контракт интерфейса: ключи thread_id:int и content:str, ровно два
    assert all(type(i["thread_id"]) is int for i in items)
    assert all(type(i["content"]) is str for i in items)
    assert all(set(i) == {"thread_id", "content"} for i in items)


def test_unicode_and_markdown_survive(make_outbox):
    payload = 'привет 🎬 "кавычки" **жирный** \n перенос'
    outbox = make_outbox()
    outbox.append(1, payload)
    assert outbox.load()[0]["content"] == payload


def test_second_instance_sees_same_storage(make_outbox):
    # «перезапуск»: новый экземпляр поверх того же хранилища видит содержимое
    make_outbox().append(1, "переживает рестарт")
    assert [i["content"] for i in make_outbox().load()] == ["переживает рестарт"]


def test_save_replaces_whole_content(make_outbox):
    outbox = make_outbox()
    outbox.append(1, "старое")
    outbox.save([{"thread_id": 2, "content": "новое"}])
    assert outbox.load() == [{"thread_id": 2, "content": "новое"}]
    outbox.save([])
    assert outbox.load() == []


def test_append_after_save_continues_fifo(make_outbox):
    outbox = make_outbox()
    outbox.append(1, "буферизованное")
    # семантика flush_pending: save(остаток), затем новые сообщения — в конец
    outbox.save([{"thread_id": 9, "content": "остаток после сбоя"}])
    outbox.append(2, "добавленное позже")
    assert [i["content"] for i in outbox.load()] == ["остаток после сбоя", "добавленное позже"]
