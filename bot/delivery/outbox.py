"""Персистентный буфер недоставленных сообщений (pending_replies.json).

Гарантии из монолита: порядок сообщений сохраняется, запись атомарная
(.tmp -> replace) — переживает падение процесса в середине записи.
Обобщение в outbox-таблицу БД запланировано фазой 4 (ARCHITECTURE.md §10).
"""

import json
import logging
from pathlib import Path

log = logging.getLogger("yt-summary")


class PendingOutbox:
    """JSON-файл со списком {thread_id, content} недоставленных ответов."""

    def __init__(self, path):
        self.path = Path(path)

    def load(self) -> list:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return []

    def save(self, items: list) -> None:
        # атомарная запись: сначала во временный файл, затем атомарный replace
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)

    def append(self, thread_id: int, content: str) -> None:
        items = self.load()
        items.append({"thread_id": thread_id, "content": content})
        self.save(items)
        log.warning("ответ буферизован (%d всего): тред %s", len(items), thread_id)
