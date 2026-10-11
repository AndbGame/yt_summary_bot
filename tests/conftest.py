"""Общий bootstrap тестов и общие фейки Discord/LLM.

Репозиторий добавляется в sys.path, чтобы `import bot` работал из корня;
фейки (треды, статусные сообщения, LLM, discord-клиент, кеш отчётов) собраны
здесь и разданы фикстурами-фабриками, чтобы юнит- и e2e-тесты не дублировали их.
"""

import asyncio
import dataclasses
import itertools
import re
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
    """Тред-фейк: копит отправленные сообщения в sent.

    fetchable — сообщения, «существующие» на стороне Discord и доступные
    fetch_message: recovery фазы 4 прикрепляет статусное сообщение к
    восстановленной после рестарта джобе.
    """

    def __init__(self, id=4242):
        self.id = id
        self.sent = []
        self.fetchable = {}

    async def send(self, content):
        self.sent.append(content)

    async def fetch_message(self, message_id):
        if message_id in self.fetchable:
            return self.fetchable[message_id]
        raise discord.NotFound(SimpleNamespace(status=404, reason="x"), "нет сообщения")


class FakeStatus:
    """Статусное сообщение-фейк: запоминает удаление и правки текста.

    id нужен фазе 4: handle() кладёт status_message_id в джобу, recovery по
    нему находит сообщение через fetch_message.
    """

    def __init__(self, id=None):
        self.id = id
        self.deleted = False
        self.edits = []

    async def delete(self):
        self.deleted = True

    async def edit(self, content=None, **kwargs):
        self.edits.append(content)


# Уникальные id сообщений между тестами: дедуп (фаза 4 — в БД) keyed by
# message.id, уникальные id изолируют тесты без сброса общего состояния.
_message_ids = itertools.count(1001)


class FakeMessage:
    """Сообщение-фейк для handle(): reply() -> статус, create_thread() -> тред.

    thread может быть тредом (обычный путь), исключением (создание падает)
    или None (создание треда не ожидалось). status_id — id статусного сообщения
    (фаза 4: попадает в джобу как status_message_id). kwargs create_thread
    записываются в thread_kwargs — так тесты проверяют имя треда.
    """

    def __init__(self, content, *, id=None, thread=None, channel=None, status_id=None):
        self.id = id if id is not None else next(_message_ids)
        self.content = content
        self.author = SimpleNamespace(bot=False)
        self.channel = channel if channel is not None else SimpleNamespace(id=10, threads=[])
        self.status = FakeStatus(id=status_id)
        self._thread = thread
        self.thread_kwargs = None

    async def reply(self, text=""):
        return self.status

    async def create_thread(self, **kwargs):
        self.thread_kwargs = kwargs
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
    """Фейк deps.author_facts: настраиваемые результаты get_author_ref()/get_channel().

    Фаза 4 сменила контракт MISS-пути: провайдер сначала даёт AuthorRef по URL
    (get_author_ref, oEmbed), факты канала — отдельно через get_channel(ref).
    get_author_ref_result — ответ get_author_ref(url): None (автора нет/сеть
    упала) или ref; get_channel_result — ответ get_channel(ref): None или facts.
    ref/facts — утиной типизации (в тестах SimpleNamespace): пайплайн читает
    только атрибуты platform/handle и name/url. Вызовы считаются раздельно:
    ref_calls (get_author_ref, по url) и channel_calls (get_channel); e2e
    проверяет «провайдер не зовётся на кеш-хите» и «ровно один вызов на промахе».
    """

    def __init__(self, get_author_ref_result=None, get_channel_result=None):
        self.get_author_ref_result = get_author_ref_result
        self.get_channel_result = get_channel_result
        self.ref_calls = []
        self.channel_calls = []

    def get_author_ref(self, url):
        self.ref_calls.append(url)
        return self.get_author_ref_result

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


# ------------------------------------------------------ фейки фазы 4 (источники/джобы)


class FakeExtractor:
    """Фейк экстрактора за портом ExtractorPort (фаза 4).

    transcripts — контент по url (чего нет — default_transcript); extract_error —
    исключение из extract (контент недоступен); find_span — настроенные границы
    совпадения, иначе ищется первый http(s)-URL в тексте. Контракт порта:
    find(text) -> (start, end) | None — основной метод (границы для Router),
    matches(url) производен от find; canonical_id детерминирован по url.
    Вызовы extract считаются — e2e кеша проверяет «контент добывается всегда,
    но LLM на хите не зовётся».
    """

    name = "video"

    _URL_RE = re.compile(r"https?://\S+")

    def __init__(
        self, transcripts=None, extract_error=None, find_span=None, default_transcript="субтитры"
    ):
        self.transcripts = dict(transcripts or {})
        self.extract_error = extract_error
        self.find_span = find_span
        self.default_transcript = default_transcript
        self.extract_calls = []

    def find(self, text):
        if self.find_span is not None:
            start, end = self.find_span
            if text and 0 <= start < end <= len(text):
                return (start, end)
            return None
        match = self._URL_RE.search(text or "")
        return (match.start(), match.end()) if match else None

    def matches(self, url):
        return self.find(url) is not None

    def canonical_id(self, url):
        return f"video:{url}"

    def extract(self, url):
        from bot.sources.base import SourceDocument  # лениво: TDD фазы 4

        self.extract_calls.append(url)
        if self.extract_error is not None:
            raise self.extract_error
        return SourceDocument(
            canonical_id=self.canonical_id(url),
            kind=self.name,
            url=url,
            content=self.transcripts.get(url, self.default_transcript),
        )


class FakeArticleFetch:
    """Фейк fetch(url, timeout) экстрактора статей (фаза 5): настраиваемый ответ.

    Контракт фазы 5: fetch возвращает (status, headers, text) | None — тройку
    (у провайдера автора была пара, у статьи нужен content-type). По умолчанию
    любой URL получает один и тот же ответ — для e2e с повторной ссылкой это
    ровно то, что нужно (та же страница с utm-хвостом); исключение из fetch
    эмулируется exc=..., «сеть легла» — returns_none=True. Вызовы считаются:
    calls хранит (url, timeout) каждого запроса.
    """

    def __init__(self, html="", *, status=200, headers=None, returns_none=False, exc=None):
        self.html = html
        self.status = status
        self.headers = (
            headers if headers is not None else {"content-type": "text/html; charset=utf-8"}
        )
        self.returns_none = returns_none
        self.exc = exc
        self.calls = []

    def __call__(self, url, timeout=None):
        self.calls.append((url, timeout))
        if self.exc is not None:
            raise self.exc
        if self.returns_none:
            return None
        return self.status, self.headers, self.html


class InMemoryJobStore:
    """Фейк deps.jobs (фаза 4): тот же интерфейс, что JobStore на БД, данные в памяти.

    Дефолт make_deps до реализации фазы 4 и тестам воркера, которым нужна
    трассировка mark_done/mark_failed. take_new отдаёт payload + job_id — как
    настоящий (state/error наружу не протекают).
    """

    def __init__(self):
        self._rows = {}
        self._ids = itertools.count(1)
        self.done_calls = []
        self.failed_calls = []

    def put(self, payload):
        job_id = next(self._ids)
        self._rows[job_id] = {"state": "new", "error": None, **payload, "job_id": job_id}
        return job_id

    def take_new(self):
        rows = [r for r in self._rows.values() if r["state"] == "new"]
        return [{k: v for k, v in r.items() if k not in ("state", "error")} for r in rows]

    def mark_done(self, job_id):
        self.done_calls.append(job_id)
        row = self._rows.get(job_id)
        if row is not None:
            row["state"] = "done"

    def mark_failed(self, job_id, error):
        self.failed_calls.append((job_id, error))
        row = self._rows.get(job_id)
        if row is not None:
            row["state"] = "failed"
            row["error"] = error

    def count_new(self):
        return sum(1 for r in self._rows.values() if r["state"] == "new")


class InMemoryDedupStore:
    """Фейк deps.dedup (фаза 4): та же семантика, что DedupStore на БД —
    ключ message_id (PK таблицы), mark даёт True только при первой вставке."""

    def __init__(self):
        self._seen = set()
        self.mark_calls = []

    def mark(self, message_id, canonical_id):
        self.mark_calls.append((str(message_id), canonical_id))
        if str(message_id) in self._seen:
            return False
        self._seen.add(str(message_id))
        return True


def _default_router():
    """Роутер по умолчанию (фаза 4): настоящий Router с FakeExtractor."""
    from bot.router import Router

    return Router([FakeExtractor()])


def _default_job_dedup_stores(tmp_path):
    """Джобы и дедуп по умолчанию (фаза 4): настоящие репозитории на временной
    SQLite — e2e-сценарии дедупа и восстановления идут через ту же БД, что и прод."""
    from bot.store.db import Store
    from bot.store.dedup import DedupStore
    from bot.store.jobs import JobStore

    store = Store(str(tmp_path / "deps_jobs.sqlite3"))
    return JobStore(store), DedupStore(store)


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
    """Фабрика провайдера фактов автора:
    make_author_facts(get_author_ref_result=ref, get_channel_result=facts)."""
    return FakeAuthorFactsProvider


@pytest.fixture
def make_article_fetch():
    """Фабрика фейка fetch экстрактора статей (фаза 5): make_article_fetch(html),
    make_article_fetch(status=404), make_article_fetch(returns_none=True),
    make_article_fetch(exc=...); headers по умолчанию — text/html."""
    return FakeArticleFetch


@pytest.fixture
def make_extractor():
    """Фабрика фейк-экстрактора (фаза 4): make_extractor(transcripts={url: текст},
    extract_error=exc, find_span=(start, end))."""
    return FakeExtractor


@pytest.fixture
def make_router():
    """Фабрика настоящего Router (фаза 4): make_router([extractor, ...]).

    Импорт ленивый: до реализации фазы 4 тест падает точечно, а не ломает
    коллекцию файла (TDD)."""

    def _make(extractors):
        from bot.router import Router

        return Router(extractors)

    return _make


@pytest.fixture
def make_job_store():
    """Фабрика in-memory JobStore-фейка (с трассировкой mark_done/mark_failed)."""
    return InMemoryJobStore


@pytest.fixture
def make_dedup_store():
    """Фабрика in-memory DedupStore-фейка (с трассировкой mark)."""
    return InMemoryDedupStore


@pytest.fixture
def make_client():
    """Фабрика discord-клиента: make_client({thread_id: thread})."""
    return FakeClient


@pytest.fixture
def make_deps(tmp_path, make_client, make_llm):
    """Собирает Deps на фейках: все части можно подменить по одному.

    По умолчанию: конфиг с тестовыми токенами, фейк-клиент, фейк-LLM,
    PendingOutbox во временном файле, InMemoryReportStore как кеш отчётов
    (deps.reports), реальный SafeSender поверх фейков, заготовки фазы 3
    (провайдер фактов без результатов, пустой InMemoryAuthorStore, реальная
    asyncio.Queue фоновой очереди, llm_lock=None) и фазы 4: deps.router —
    Router с FakeExtractor, deps.jobs/deps.dedup — настоящие JobStore/DedupStore
    на временной SQLite, чтобы сценарии дедупа и восстановления шли через ту же
    БД, что и прод.
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
        router=None,
        jobs=None,
        dedup=None,
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
        if router is None:
            router = _default_router()
        if jobs is None or dedup is None:
            default_jobs, default_dedup = _default_job_dedup_stores(tmp_path)
            if jobs is None:
                jobs = default_jobs
            if dedup is None:
                dedup = default_dedup
        # поля фаз 3-4: если уже объявлены в Deps — передаём конструктору; пока их
        # нет (тесты написаны под контракт раньше реализации, TDD) — донашиваем
        # setattr'ом, чтобы все существующие тесты оставались зелёными и до, и
        # после реализации
        phase3 = {
            "author_facts": author_facts,
            "authors": authors,
            "background_queue": background_queue,
            "llm_lock": llm_lock,
        }
        phase4 = {"router": router, "jobs": jobs, "dedup": dedup}
        injectable = {**phase3, **phase4}
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
            **{name: value for name, value in injectable.items() if name in declared},
        )
        for name, value in injectable.items():
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
