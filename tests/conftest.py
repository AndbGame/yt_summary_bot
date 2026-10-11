"""Общий bootstrap тестов и общие фейки Discord/LLM.

Репозиторий добавляется в sys.path, чтобы `import bot` работал из корня;
фейки (треды, статусные сообщения, LLM, discord-клиент, кеш отчётов) собраны
здесь и разданы фикстурами-фабриками, чтобы юнит- и e2e-тесты не дублировали их.
"""

import asyncio
import dataclasses
import itertools
import sys
import time
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


# Дефолтный JSON-ответ LLM для фейка (фаза 2): валидный отчёт «кратко + тезисы».
DEFAULT_ANALYSIS = {
    "brief": "Короткая суть видео.",
    "complexity": "simple",
    "context": "",
    "theses": ["первый тезис", "второй тезис"],
    "sections": [],
}


class FakeLLM:
    """Фейк deps.llm: analyze_content отдаёт Report из заготовленного JSON-ответа LLM;
    review_author (фаза 3) — настраиваемый текст обзора автора или исключение.

    analysis_json — ответ LLM в виде dict (собирается в Report тем же
    Report.from_analysis, что и в LLMClient); raw — сырой ответ строкой:
    парсится тем же терпеливым парсером, поэтому мусор даёт ReportParseError
    (эмуляция «LLM вернул не JSON, попытки исчерпаны»); error — исключение
    наружу как есть; review — текст обзора (по умолчанию «Обзор автора: тест»),
    review_error — исключение из review_author. Вызовы считаются раздельно:
    calls (analyze_content) и review_calls (review_author). Импорт
    bot.analysis.report ленивый: до реализации фазы 2 фейк не должен ломать
    КОЛЛЕКЦИЮ тестов, которым он не нужен (TDD).
    """

    def __init__(self, analysis_json=None, raw=None, error=None, review=None, review_error=None):
        self.analysis_json = dict(DEFAULT_ANALYSIS) if analysis_json is None else analysis_json
        self.raw = raw
        self.error = error
        self.review = "Обзор автора: тест" if review is None else review
        self.review_error = review_error
        self.calls = []
        self.review_calls = []

    def analyze_content(self, transcript):
        self.calls.append(transcript)
        if self.error is not None:
            raise self.error
        from bot.analysis.report import Report, parse_report_json  # лениво — см. докстринг

        if self.raw is not None:
            return parse_report_json(self.raw)
        return Report.from_analysis(self.analysis_json)

    def review_author(self, facts):
        """Фейк deps.llm.review_author (фаза 3): текст обзора по фактам или исключение."""
        self.review_calls.append(facts)
        if self.review_error is not None:
            raise self.review_error
        return self.review


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
    """Фейк deps.reports (кеш отчётов): тот же интерфейс get/get_latest/put, что у
    ReportStore фазы 2 — с content_hash в ключе.

    Данные только в памяти; персистентность, схема БД и семантика совпадения
    None-хэша с NULL/"" проверяются отдельными тестами настоящего Store
    (tests/unit/test_store.py). Нужен, чтобы все существующие e2e-тесты работали
    с полем deps.reports без SQLite-файла.
    """

    def __init__(self):
        self._reports = {}

    def get(self, canonical_id, prompts_ver, schema_ver, content_hash=None):
        return self._reports.get((canonical_id, prompts_ver, schema_ver, content_hash))

    def get_latest(self, canonical_id, prompts_ver, schema_ver):
        """Последняя запись тройки (любой content_hash) — зеркало ReportStore.get_latest.

        «Последняя» — по порядку put: перезапись двигает запись в конец, как
        created_at у настоящего хранилища.
        """
        found = None
        for key, payload in self._reports.items():
            if key[:3] == (canonical_id, prompts_ver, schema_ver):
                found = payload
        return found

    def put(self, canonical_id, prompts_ver, schema_ver, payload, content_hash=""):
        key = (canonical_id, prompts_ver, schema_ver, content_hash)
        # перезапись двигает запись в конец порядка вставки — аналог created_at
        self._reports.pop(key, None)
        self._reports[key] = payload


class FakeAuthorFactsProvider:
    """Фейк deps.author_facts (фаза 3): настраиваемые результаты get()/get_channel().

    get_result — ответ get(video_id): None («автора нет/сеть упала») или (ref, facts);
    get_channel_result — ответ get_channel(ref): None или facts. ref/facts — утиной
    типизации (в тестах SimpleNamespace): пайплайн читает только атрибуты
    platform/handle и name/url, так что настоящие dataclass'ы фазы 3 совместимы.
    Вызовы считаются: e2e проверяет «провайдер не зовётся на кеш-хите» и «ровно
    один вызов на промахе».
    """

    def __init__(self, get_result=None, get_channel_result=None):
        self.get_result = get_result
        self.get_channel_result = get_channel_result
        self.get_calls = []
        self.channel_calls = []

    def get(self, video_id):
        self.get_calls.append(video_id)
        return self.get_result

    def get_channel(self, ref):
        self.channel_calls.append(ref)
        return self.get_channel_result


class InMemoryAuthorStore:
    """Тестируемый аналог AuthorStore (фаза 3): тот же интерфейс get/put/is_fresh,
    данные в памяти.

    Схему БД, PK-перезапись и персистентность проверяет настоящий AuthorStore
    (tests/unit/test_author_store.py); фейк нужен, чтобы make_deps работал без
    SQLite-файла. Keyword-only analyzed_at в put — тестовая надстройка: сеять
    устаревшие строки без ожиданий (настоящий put штампует только now).
    """

    def __init__(self):
        self._rows = {}

    def get(self, platform, handle):
        return self._rows.get((platform, handle))

    def put(
        self, platform, handle, display_name, facts_json, review_json, prompts_ver, *, analyzed_at=None
    ):
        stamp = int(time.time()) if analyzed_at is None else analyzed_at
        self._rows[(platform, handle)] = {
            "platform": platform,
            "handle": handle,
            "display_name": display_name,
            "facts_json": facts_json,
            "review_json": review_json,
            "prompts_ver": prompts_ver,
            "analyzed_at": stamp,
            "refreshed_at": stamp,
        }

    def is_fresh(self, row, ttl_seconds, now=None):
        """Свежесть по analyzed_at: возраст строго меньше TTL. Форму («метод или
        статикметод») настоящего AuthorStore тесты не фиксируют — зовут через инстанс."""
        if row is None:
            return False
        current = int(time.time()) if now is None else now
        return current - row["analyzed_at"] < ttl_seconds


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
    """Фабрика LLM-фейка: make_llm(analysis_json={...}), make_llm(raw="..."),
    make_llm(error=exc); для автор-пути фазы 3 — make_llm(review="обзор")
    и make_llm(review_error=exc)."""

    def _make(analysis_json=None, raw=None, error=None, review=None, review_error=None):
        return FakeLLM(
            analysis_json=analysis_json, raw=raw, error=error, review=review, review_error=review_error
        )

    return _make


@pytest.fixture
def make_author_facts():
    """Фабрика провайдера фактов автора (фаза 3):
    make_author_facts(get_result=(ref, facts), get_channel_result=facts)."""
    return FakeAuthorFactsProvider


@pytest.fixture
def make_client():
    """Фабрика discord-клиента: make_client({thread_id: thread})."""
    return FakeClient


@pytest.fixture
def make_deps(tmp_path, make_client, make_llm):
    """Собирает Deps на фейках: все части можно подменить по одному.

    По умолчанию: конфиг с тестовыми токенами, фейк-клиент, фейк-LLM,
    PendingOutbox во временном файле, InMemoryReportStore как кеш отчётов
    (deps.reports), реальный SafeSender поверх фейков и заготовки фазы 3:
    провайдер фактов без результатов (get/get_channel -> None), пустой
    InMemoryAuthorStore, реальная asyncio.Queue фоновой очереди; llm_lock=None
    (сериализация LLM включается только в тестах блокировки).
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
        author_facts=None,
        authors=None,
        background_queue=None,
        llm_lock=None,
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
        if author_facts is None:
            # провайдер без результатов: автор-путь существующих сценариев
            # не задействует, точечные тесты автора подставляют свои ответы
            author_facts = FakeAuthorFactsProvider()
        if authors is None:
            authors = InMemoryAuthorStore()
        if background_queue is None:
            background_queue = asyncio.Queue()
        # поля Deps фазы 3: если уже объявлены — передаём конструктору; пока их нет
        # (тесты написаны под контракт раньше реализации, TDD) — донашиваем setattr'ом,
        # чтобы все существующие тесты оставались зелёными и до, и после реализации
        phase3 = {
            "author_facts": author_facts,
            "authors": authors,
            "background_queue": background_queue,
            "llm_lock": llm_lock,
        }
        declared = {f.name for f in dataclasses.fields(Deps)}
        deps = Deps(
            config=config,
            llm=llm,
            outbox=outbox,
            prompts=prompts,
            sender=sender,
            client=client,
            queue=queue,
            reports=reports,
            **{name: value for name, value in phase3.items() if name in declared},
        )
        for name, value in phase3.items():
            if name not in declared:
                setattr(deps, name, value)
        return deps

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
