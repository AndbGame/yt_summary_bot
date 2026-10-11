"""Юнит-тесты персистентного буфера недоставленных сообщений (outbox)."""

from types import SimpleNamespace

import aiohttp
import discord

import yt_summary_bot as bot


def test_roundtrip_and_unicode(tmp_path, monkeypatch):
    monkeypatch.setattr(bot, "PENDING_FILE", tmp_path / "pending.json")
    bot._pending_append(111, "привет **мир** 🎬")
    bot._pending_append(222, "второе сообщение")
    items = bot._pending_load()
    assert [i["thread_id"] for i in items] == [111, 222]
    assert items[0]["content"] == "привет **мир** 🎬"


def test_missing_file_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(bot, "PENDING_FILE", tmp_path / "nope.json")
    assert bot._pending_load() == []


def test_corrupted_file_returns_empty(tmp_path, monkeypatch):
    p = tmp_path / "pending.json"
    p.write_text("{broken json", encoding="utf-8")
    monkeypatch.setattr(bot, "PENDING_FILE", p)
    assert bot._pending_load() == []


def test_save_writes_atomically_no_tmp_left(tmp_path, monkeypatch):
    monkeypatch.setattr(bot, "PENDING_FILE", tmp_path / "pending.json")
    bot._pending_save([{"thread_id": 1, "content": "x"}])
    assert not (tmp_path / "pending.tmp").exists()
    assert (tmp_path / "pending.json").exists()


def test_retryable_send_error_classification():
    assert bot._is_retryable_send_error(aiohttp.ClientError()) is True
    # asyncio.TimeoutError с Python 3.11 — это встроенный TimeoutError
    assert bot._is_retryable_send_error(TimeoutError()) is True
    # 5xx ретраим, 4xx — нет
    fake_500 = discord.HTTPException(SimpleNamespace(status=500, reason="x"), "boom")
    fake_403 = discord.HTTPException(SimpleNamespace(status=403, reason="x"), "no rights")
    assert bot._is_retryable_send_error(fake_500) is True
    assert bot._is_retryable_send_error(fake_403) is False
