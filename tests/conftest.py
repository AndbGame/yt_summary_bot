"""Общий bootstrap тестов и общие фейки Discord/LLM.

Репозиторий добавляется в sys.path, чтобы `import bot` работал из корня;
фейки (треды, статусные сообщения, LLM, discord-клиент, кеш отчётов) собраны
здесь и разданы фикстурами-фабриками, чтобы юнит- и e2e-тесты не дублировали их.
"""

import asyncio
import itertools
import sys
from pathlib import Path
from types import SimpleNamespace

import discord
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ------------------------------------------------------- фейки Discord-объектов


class FakeThread:
    """Тред-фейк: копит отправленные сообщения в sent."""

    def __init__(self, id=4242):
        self.id = id
        self.sent = []

    async def send(self, content):
        self.sent.append(content)


class FakeStatus:
    """Статусное сообщение-фейк: запоминает удаление и правки текста."""

    def __init__(self):
        self.deleted = False
        self.edits = []

    async def delete(self):
        self.deleted = True

    async def edit(self, content=None, **kwargs):
        self.edits.append(content)


# Уникальные id сообщений между тестами: дедуп в discord_app — in-memory set,
# и уникальные ключи «message.id:video_id» изолируют тесты без сброса глобала.
_message_ids = itertools.count(1001)


class FakeMessage:
    """Сообщение-фейк для handle(): reply() -> статус, create_thread() -> тред.

    thread может быть тредом (обычный путь), исключением (создание падает)
    или None (создание треда не ожидалось).
    """

    def __init__(self, content, *, id=None, thread=None, channel=None):
        self.id = id if id is not None else next(_message_ids)
        self.content = content
        self.author = SimpleNamespace(bot=False)
        self.channel = channel if channel is not None else SimpleNamespace(id=10, threads=[])
        self.status = FakeStatus()
        self._thread = thread

    async def reply(self, text=""):
        return self.status

    async def create_thread(self, **kwargs):
        if isinstance(self._thread, Exception):
            raise self._thread
        return self._thread


class FakeLLM:
    """Фейк deps.llm: возвращает заготовку или бросает ошибку; копит транскрипты."""

    def __init__(self, summary="итог", error=None):
        self.summary = summary
        self.error = error
        self.calls = []

    def summarize(self, transcript):
        self.calls.append(transcript)
        if self.error is not None:
            raise self.error
        return self.summary


class FakeClient:
    """Фейк discord-клиента: get_channel из словаря, fetch_channel всегда NotFound."""

    def __init__(self, channels=None):
        self.channels = dict(channels or {})
        self.closed = False
        self.ready = True

    def get_channel(self, channel_id):
        return self.channels.get(channel_id)

    async def fetch_channel(self, channel_id):
        raise discord.NotFound(SimpleNamespace(status=404, reason="x"), "нет такого канала")

    def is_closed(self):
        return self.closed

    def is_ready(self):
        return self.ready


class InMemoryReportStore:
    """Фейк deps.reports (кеш отчётов): тот же интерфейс get/put, что у ReportStore.

    Данные только в памяти; персистентность и схема БД проверяются отдельными
    тестами настоящего Store (tests/unit/test_store.py). Нужен, чтобы все
    существующие e2e-тесты работали с полем deps.reports без SQLite-файла.
    """

    def __init__(self):
        self._reports = {}

    def get(self, canonical_id, prompts_ver, schema_ver):
        return self._reports.get((canonical_id, prompts_ver, schema_ver))

    def put(self, canonical_id, prompts_ver, schema_ver, payload):
        self._reports[(canonical_id, prompts_ver, schema_ver)] = payload


# --------------------------------------------------------- фикстуры-фабрики


@pytest.fixture
def make_thread():
    """Фабрика тредов: make_thread(4242)."""
    return FakeThread


@pytest.fixture
def make_status():
    """Фабрика статусных сообщений: make_status()."""
    return FakeStatus


@pytest.fixture
def make_message():
    """Фабрика сообщений: make_message("текст", id=..., thread=..., channel=...)."""
    return FakeMessage


@pytest.fixture
def make_llm():
    """Фабрика LLM-фейка: make_llm(summary="итог") или make_llm(error=exc)."""

    def _make(summary="итог", error=None):
        return FakeLLM(summary=summary, error=error)

    return _make


@pytest.fixture
def make_client():
    """Фабрика discord-клиента: make_client({thread_id: thread})."""
    return FakeClient


@pytest.fixture
def make_deps(tmp_path, make_client, make_llm):
    """Собирает Deps на фейках: все части можно подменить по одному.

    По умолчанию: конфиг с тестовыми токенами, фейк-клиент, фейк-LLM,
    PendingOutbox во временном файле, InMemoryReportStore как кеш отчётов
    (deps.reports) и реальный SafeSender поверх фейков.
    """
    from bot import jobs as jobs_mod
    from bot.analysis.prompts import load_prompts
    from bot.config import BotConfig
    from bot.delivery.outbox import PendingOutbox
    from bot.delivery.sender import SafeSender
    from bot.pipeline import Deps

    # реальный prompts.yaml репозитория — читается один раз на тест
    repo_prompts = load_prompts()

    def _make(
        *,
        config=None,
        client=None,
        llm=None,
        outbox=None,
        prompts=None,
        sender=None,
        queue=None,
        reports=None,
        thread=None,
    ):
        if config is None:
            config = BotConfig(bot_token="test-token", llm_api_key="test-key")
        if client is None:
            channels = {thread.id: thread} if thread is not None else {}
            client = make_client(channels)
        if llm is None:
            llm = make_llm()
        if outbox is None:
            outbox = PendingOutbox(tmp_path / "pending.json")
        if prompts is None:
            prompts = repo_prompts
        if sender is None:
            sender = SafeSender(client, outbox, config)
        if queue is None:
            queue = jobs_mod.create_queue()
        if reports is None:
            # кеш отчётов по умолчанию — in-memory фейк: e2e-тесты кеша
            # подменяют его на настоящий Store (tests/e2e/test_pipeline_cache.py)
            reports = InMemoryReportStore()
        return Deps(
            config=config,
            llm=llm,
            outbox=outbox,
            prompts=prompts,
            sender=sender,
            client=client,
            queue=queue,
            reports=reports,
        )

    return _make


@pytest.fixture(autouse=True)
def _no_real_network_sleep(monkeypatch):
    """Ни один тест не должен реально ждать: паузы отправки/ретраев — мгновенные.

    Подменяется только asyncio.sleep (паузы между ретраями); на результат
    тестов это не влияет, но защищает от случайных «зависаний» на сбойных путях.
    """

    async def _instant(_seconds):
        return None

    monkeypatch.setattr(asyncio, "sleep", _instant)
