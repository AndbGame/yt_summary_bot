"""build_deps фазы 1: Store с SQLite-кешем отчётов и outbox в БД.

Контракт: build_deps создаёт Store(config.db_path), deps.reports = store.reports,
deps.outbox = store.outbox (вместо PendingOutbox). store.close() в build_deps не
вызывается — соединение живёт до конца процесса (поэтому в тестах соединение
освобождается вместе с deps, а файлы БД удаляются «по возможности»).

Отдельно зафиксировано: относительный db_path разрешается от КОРНЯ РЕПОЗИТОРИЯ,
а не от cwd (бот запускают из любой директории). Миграцией легаси-файла
build_deps не занимается — см. tests/e2e/test_legacy_migration.py.
"""

import gc
from contextlib import suppress
from pathlib import Path

from bot.config import BotConfig
from bot.main import PENDING_FILE, build_deps
from bot.store.db import Store
from bot.store.outbox import DbOutbox
from bot.store.reports import ReportStore


def _remove_db_files(db_path):
    """Уборка по возможности: соединение Store живёт, пока жив deps (контракт)."""
    for suffix in ("", "-wal", "-shm"):
        with suppress(OSError):
            Path(str(db_path) + suffix).unlink()


def test_build_deps_wires_sqlite_store(tmp_path):
    db_path = tmp_path / "bot_data.sqlite3"
    config = BotConfig(bot_token="t", llm_api_key="k", db_path=str(db_path))

    deps = build_deps(config)

    assert isinstance(deps.reports, ReportStore)
    assert isinstance(deps.outbox, DbOutbox)
    # кеш действительно в файле: записанное через deps.reports читает новый Store
    deps.reports.put("youtube:video:abc", "1", "summary-1", "саммари")
    check = Store(str(db_path))
    try:
        assert check.reports.get("youtube:video:abc", "1", "summary-1") == "саммари"
    finally:
        check.close()

    del deps
    gc.collect()  # соединение Store освобождается вместе с deps (циклы discord-клиента)
    _remove_db_files(db_path)


def test_build_deps_resolves_relative_db_path_from_repo_root(tmp_path, monkeypatch):
    # db_path задан относительно корня репозитория, а не cwd
    repo_root = PENDING_FILE.parent
    db_name = "cache_build_deps_test.sqlite3"
    monkeypatch.chdir(tmp_path)
    config = BotConfig(bot_token="t", llm_api_key="k", db_path=db_name)

    deps = build_deps(config)

    deps.reports.put("youtube:video:abc", "1", "summary-1", "саммари")
    db_path = repo_root / db_name
    assert db_path.exists(), "БД должна создаваться в корне репозитория, не в cwd"

    del deps
    gc.collect()
    _remove_db_files(db_path)
