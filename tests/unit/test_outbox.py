"""Юнит-тесты персистентного буфера недоставленных сообщений (PendingOutbox)."""

from bot.delivery.outbox import PendingOutbox


def test_roundtrip_and_unicode(tmp_path):
    outbox = PendingOutbox(tmp_path / "pending.json")
    outbox.append(111, "привет **мир** 🎬")
    outbox.append(222, "второе сообщение")
    items = outbox.load()
    assert [i["thread_id"] for i in items] == [111, 222]
    assert items[0]["content"] == "привет **мир** 🎬"


def test_missing_file_returns_empty(tmp_path):
    outbox = PendingOutbox(tmp_path / "nope.json")
    assert outbox.load() == []


def test_corrupted_file_returns_empty(tmp_path):
    p = tmp_path / "pending.json"
    p.write_text("{broken json", encoding="utf-8")
    outbox = PendingOutbox(p)
    assert outbox.load() == []


def test_save_writes_atomically_no_tmp_left(tmp_path):
    outbox = PendingOutbox(tmp_path / "pending.json")
    outbox.save([{"thread_id": 1, "content": "x"}])
    # атомарная запись через .tmp: временный файл не остаётся после записи
    assert not list(tmp_path.glob("*.tmp"))
    assert (tmp_path / "pending.json").exists()


def test_save_load_direct_roundtrip(tmp_path):
    outbox = PendingOutbox(tmp_path / "pending.json")
    items = [
        {"thread_id": 1, "content": "юникод 🎬 и **разметка**"},
        {"thread_id": 2, "content": "второе"},
    ]
    outbox.save(items)
    assert outbox.load() == items
