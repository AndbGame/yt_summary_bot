"""SQLite-хранилище: единственное соединение + идемпотентные миграции.

Store — владелец соединения; репозитории (reports, outbox) — фасады над ним
и доступны как свойства. Журнал WAL: читатели не блокируют писателя, а режим
персистентен в файле БД (виден любому внешнему соединению).
"""

import sqlite3
import threading
from contextlib import contextmanager

from bot.store.authors import AuthorStore
from bot.store.outbox import DbOutbox
from bot.store.reports import ReportStore


def _add_reports_content_hash(conn) -> None:
    """Шаг 2 (фаза 2): колонка content_hash в reports — часть ключа кеша.

    ALTER TABLE в sqlite не умеет IF NOT EXISTS, а свежая база уже получает
    колонку от шага 1 — поэтому проверяем PRAGMA table_info и добавляем только
    при отсутствии (легаси-база фазы 1). Колонка nullable: ALTER с NOT NULL
    в sqlite требует непустой DEFAULT, а легаси-строки должны остаться читаемыми
    (пустой хэш трактуется как «запись без content_hash», см. ReportStore.get).
    """
    columns = [row[1] for row in conn.execute("PRAGMA table_info(reports)")]
    if "content_hash" not in columns:
        conn.execute("ALTER TABLE reports ADD COLUMN content_hash TEXT")


# Миграции схемы: ключ — целевая PRAGMA user_version, значение — DDL шага
# (строка) или функция от соединения (когда одного SQL недостаточно).
# Новая версия схемы = новый шаг с большим номером; старые не переписывать.
_MIGRATIONS = {
    # шаг 1 обновлён фазой 2: свежая БД создаётся сразу с content_hash;
    # для легаси — шаг 2 добавляет колонку ALTER'ом
    1: """
        CREATE TABLE IF NOT EXISTS reports (
            canonical_id TEXT NOT NULL,
            prompts_ver  TEXT NOT NULL,
            schema_ver   TEXT NOT NULL,
            payload      TEXT NOT NULL,
            created_at   INTEGER NOT NULL,
            content_hash TEXT,
            PRIMARY KEY (canonical_id, prompts_ver, schema_ver)
        );

        -- AUTOINCREMENT: строгая FIFO по id — id монотонны и не переиспользуются
        -- после DELETE (save() очищает таблицу, новые записи встают в конец)
        CREATE TABLE IF NOT EXISTS outbox (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            thread_id  INTEGER NOT NULL,
            content    TEXT NOT NULL,
            created_at INTEGER NOT NULL
        );
    """,
    2: _add_reports_content_hash,
    # шаг 3 (фаза 3): факты + LLM-обзор автора с TTL, ключ (platform, handle);
    # analyzed_at/refreshed_at штампует AuthorStore.put при записи
    3: """
        CREATE TABLE IF NOT EXISTS authors (
            platform     TEXT NOT NULL,
            handle       TEXT NOT NULL,
            display_name TEXT,
            facts_json   TEXT,
            review_json  TEXT,
            prompts_ver  TEXT,
            analyzed_at  INTEGER,
            refreshed_at INTEGER,
            PRIMARY KEY (platform, handle)
        );
    """,
}


class Store:
    """SQLite-хранилище: владеет одним соединением, выдаёт репозитории."""

    def __init__(self, path):
        # check_same_thread=False: соединение шарится между потоками (discord-воркеры,
        # flush-цикл); конкурентная запись сериализуется внутренним Lock, а не
        # соединением на поток — один файл, один процесс, проще и без гонок на файле.
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        # Lock на запись: python-sqlite3 не гарантирует сериализацию параллельных
        # DML по одному соединению из разных потоков — оборачиваем каждую
        # транзакцию в мьютекс (commit — внутри блокировки).
        self._lock = threading.Lock()
        self._migrate()
        self._reports = ReportStore(self)
        self._outbox = DbOutbox(self)
        self._authors = AuthorStore(self)

    def _migrate(self) -> None:
        """Применяет недостающие шаги миграции.

        Почему user_version: номер версии хранится в самом файле БД — повторный
        старт применяет только недостающие шаги, без дублей таблиц и без потери
        данных (идемпотентность «бесплатно», без таблицы schema_migrations).
        """
        version = self._query("PRAGMA user_version")[0][0]
        if version >= max(_MIGRATIONS):
            return
        with self._lock:
            for target, step in sorted(_MIGRATIONS.items()):
                if version < target:
                    if callable(step):
                        step(self._conn)
                    else:
                        self._conn.executescript(step)
                    self._conn.execute(f"PRAGMA user_version = {target}")
            self._conn.commit()

    @contextmanager
    def _txn(self):
        """Единица записи: захват блокировки + commit (или rollback при ошибке).

        commit обязателен до выхода из блокировки: тесты и «перезапуск» бота
        читают данные из ОТДЕЛЬНОГО соединения — без commit они бы их не увидели.
        """
        with self._lock:
            try:
                yield self._conn
            except Exception:
                self._conn.rollback()
                raise
            self._conn.commit()

    def _write(self, sql: str, params=()) -> None:
        """Одиночный записывающий оператор (выполняется в своей транзакции)."""
        with self._txn() as conn:
            conn.execute(sql, params)

    def _query(self, sql: str, params=()) -> list:
        # чтение тоже под Lock: одно соединение шарится между потоками,
        # и интерливинг SELECT с чужим DML на нём недопустим
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    @property
    def reports(self) -> ReportStore:
        """Репозиторий кеша отчётов — единый на Store."""
        return self._reports

    @property
    def outbox(self) -> DbOutbox:
        """Репозиторий outbox — буфер недоставленных сообщений в БД."""
        return self._outbox

    @property
    def authors(self) -> AuthorStore:
        """Репозиторий кеша обзоров авторов — единый на Store."""
        return self._authors

    def close(self) -> None:
        """Закрывает соединение; данные уже закоммичены каждой транзакцией."""
        self._conn.close()
