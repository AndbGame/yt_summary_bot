"""Композиция приложения: конфиг -> зависимости -> discord-адаптер -> запуск.

Здесь допустим единственный side effect верхнего уровня — logging.basicConfig:
на импорте не читается .env, не создаются клиенты и нет сети.
"""

import asyncio
import json
import logging
import sys
from pathlib import Path

from bot import discord_app
from bot.analysis.author import YouTubeAuthorFactsProvider
from bot.analysis.comments import YouTubeCommentsProvider
from bot.analysis.llm import LLMClient
from bot.analysis.prompts import load_prompts
from bot.config import ENV_NAMES, BotConfig
from bot.delivery.sender import SafeSender
from bot.jobs import create_background_queue, create_queue
from bot.pipeline import Deps
from bot.router import Router
from bot.sources.article import ArticleExtractor
from bot.sources.youtube import YoutubeExtractor
from bot.store.db import Store

# Буфер недоставленных ответов — в корне репозитория (рядом с шимом), как у монолита
PENDING_FILE = Path(__file__).resolve().parents[1] / "pending_replies.json"

# Корень репозитория: точка привязки для путей состояния (БД, легаси-буфер).
# Бот запускают из любой директории (Docker, start.bat), поэтому пути не
# резолвятся от cwd.
_REPO_ROOT = Path(__file__).resolve().parents[1]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("yt-summary")


def _resolve_db_path(db_path: str) -> Path:
    """Относительный db_path разрешается от корня репозитория, не от cwd."""
    p = Path(db_path)
    return p if p.is_absolute() else _REPO_ROOT / p


def build_deps(config: BotConfig) -> Deps:
    """Собирает реальные зависимости: промпты, LLM, SQLite-хранилище, роутер
    экстракторов, отправитель, клиент."""
    # prompts.yaml читается с диска ровно один раз: и в LLMClient, и в error-ветку пайплайна
    prompts = load_prompts()
    # Store — владелец единственного соединения; закрывать его здесь нельзя:
    # репозитории в deps живут до конца процесса (переживают все job'ы)
    store = Store(_resolve_db_path(config.db_path))
    llm = LLMClient(config, prompts)
    queue = create_queue()
    client = discord_app.create_client()
    sender = SafeSender(client, store.outbox, config)
    deps = Deps(
        config=config,
        llm=llm,
        outbox=store.outbox,
        prompts=prompts,
        sender=sender,
        client=client,
        queue=queue,
        reports=store.reports,
        # фаза 3: факты автора (oEmbed), кеш обзоров авторов, фоновая очередь
        # обновления и общий лок LLM-вызовов (контент-разбор и обзор автора
        # идут из разных воркеров и не должны пересекаться)
        author_facts=YouTubeAuthorFactsProvider(),
        authors=store.authors,
        background_queue=create_background_queue(),
        llm_lock=asyncio.Lock(),
        # фаза 4-5: реестр экстракторов (YouTube — первый, article — fallback,
        # порядок = приоритет), джобы и дедуп сообщений — репозитории БД
        # (переживают рестарт); провайдер топ-комментариев — ознакомительный
        # обзор реакций (UX-итерация)
        router=Router([YoutubeExtractor(), ArticleExtractor()]),
        comments=YouTubeCommentsProvider(),
        jobs=store.jobs,
        dedup=store.dedup,
    )
    discord_app.attach_handlers(client, deps)
    return deps


def migrate_legacy_outbox(store: Store, legacy_path: Path | None = None) -> None:
    """Однократная миграция легаси-буфера pending_replies.json в outbox-таблицу.

    Почему отдельная функция, а не шаг build_deps: build_deps — чистая композиция
    (тесты фиксируют, что она не трогает файловую систему сверх создания БД), а
    миграция — разовая конвертация рантайм-состояния со своими правилами.

    Почему параметр — Store, а не Deps: миграция работает с хранилищем, ей не
    нужны Discord/LLM/очередь; из main() Store доступен как владелец репозиториев
    (deps.reports.db / deps.outbox.db), тянуть его через Deps не нужно — Deps
    хранит только порты пайплайна.

    Поведение (порядок важен — сначала парсинг и валидация формы файла, потом
    решение, куда его отправить):
    - битый JSON или не та форма (не list / элементы не dict с thread_id и
      content) — файл в .corrupted, вливания нет, старт бота не роняется
      (в том числе когда outbox уже непуст);
    - outbox уже непуст (миграция выполнялась) — корректный файл переименовывается
      в .migrated БЕЗ вливания, дублей нет;
    - иначе записи вливаются в outbox в порядке файла ОДНИМ save (атомарная
      транзакция DELETE+INSERT), и только после успешного save файл
      переименовывается в .migrated — смерть процесса между вливанием и rename
      записи не теряет (при рестарте миграция повторится целиком);
    - файла нет — noop.
    """
    path = Path(legacy_path) if legacy_path is not None else PENDING_FILE
    if not path.exists():
        return

    try:
        items = _validate_legacy_items(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        # битый файл не должен ронять старт бота: сохраняем как есть, под другим именем
        _stash_legacy_file(path, ".corrupted")
        return

    existing = store.outbox.load()
    if existing:
        # в outbox уже писали из БД: легаси вторично не вливаем (без дублей)
        _stash_legacy_file(path, ".migrated")
        return

    # атомарное вливание: один save = DELETE+INSERT в одной транзакции; rename
    # строго после успешного save, иначе падение между шагами теряло бы остаток
    # файла (при рестарте непустой outbox отправил бы файл в .migrated без вливания)
    store.outbox.save(existing + items)
    _stash_legacy_file(path, ".migrated")


def _validate_legacy_items(raw) -> list[dict]:
    """Проверяет форму легаси-буфера и нормализует записи к виду outbox.

    Форма: list, каждый элемент — dict с ключами thread_id (приводимо к int) и
    content (str). Любое отклонение — ValueError: файл целиком уйдёт в
    .corrupted, частичное вливание исключено.
    """
    if not isinstance(raw, list):
        raise ValueError("легаси-буфер: ожидался список записей")
    items = []
    for entry in raw:
        if not isinstance(entry, dict) or not {"thread_id", "content"} <= set(entry):
            raise ValueError("легаси-буфер: запись не dict с ключами thread_id и content")
        if not isinstance(entry["content"], str):
            raise ValueError("легаси-буфер: content не строка")
        try:
            thread_id = int(entry["thread_id"])
        except (TypeError, ValueError) as e:
            raise ValueError("легаси-буфер: thread_id не приводится к int") from e
        items.append({"thread_id": thread_id, "content": entry["content"]})
    return items


def _stash_legacy_file(path: Path, suffix: str) -> None:
    """Переименовывает легаси-файл: суффикс дописывается к имени целиком
    (pending_replies.json -> pending_replies.json.migrated), file не удаляется."""
    path.replace(path.with_name(path.name + suffix))


def main() -> None:
    """Точка входа: валидация обязательных настроек -> миграция -> запуск discord-клиента."""
    config = BotConfig.from_env()
    missing = config.missing_fields()
    if missing:
        # в сообщении — имена env-переменных, как в монолите
        names = ", ".join(ENV_NAMES.get(name, name) for name in missing)
        sys.exit(f"Не заданы переменные: {names}. Создай .env рядом со скриптом.")
    deps = build_deps(config)
    # Store — владелец репозиториев: миграции нужен именно он (см. migrate_legacy_outbox)
    migrate_legacy_outbox(deps.reports.db)
    deps.client.run(config.bot_token)
