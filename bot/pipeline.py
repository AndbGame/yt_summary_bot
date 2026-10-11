"""Пайплайн задачи: extract (порт экстракторов) -> кеш/LLM (JSON-отчёт) ->
прогрессивный рендер -> тред.

Ядро без глобалов и без знаний о платформах: всё внешнее (Discord, LLM, буфер,
кеш отчётов, экстракторы) приходит через Deps — джоба несёт kind/url/canonical_id,
контент добывает экстрактор из deps.router по kind (extract -> SourceDocument).
Единственная сеть в ядре — за портом экстрактора и LLM.

Отчёт доставляется по сообщениям (кратко -> тезисы -> разделы), каждое отдельно
проходит format_for_discord (LLM мог нарушить запрет таблиц) и чанк-сплит; при
сбое отправки в персистентный буфер уходит ПОЛНЫЙ хвост отчёта — остаток чанков
текущего сообщения плюс все последующие сообщения, — порядок доставки сохраняется.

Контент мог исчезнуть у источника: при ошибке extract'а отдаём последний разбор
этого источника из кеша (любой content_hash), без кеша — error-сообщение. Ошибка
extract/LLM теперь не глотается: process_job сначала информирует пользователя
(error-сообщение), закрывает статус (только если доставка прошла — при
буферизации статус остаётся до досылки хвоста), затем ПРОБРАСЫВАЕТ исключение —
llm_worker помечает джобу failed в БД (mark_failed), и она не повторяется после
рестарта.

Обзор автора (фазы 3-4) строится только на промахе кеша: провайдер даёт
AuthorRef по url (get_author_ref), затем кеш авторов читается ДО сети —
свежая строка отдаёт обзор сразу, без страницы канала и LLM; иначе факты
канала добывает провайдер (get_channel) и обзор строит LLM (review_author);
блок добавляется в отчёт до кеширования. На попадании автор-блок берётся из
payload как есть, а устаревшая или отсутствующая строка автора даёт фоновую
задачу обновления — пользователь никогда не ждёт фона. Любой сбой автор-пути
не роняет джобу.

«По ссылке нет статьи» (NotAnArticle, фаза 5) — не сетевой сбой: страница
отдаётся, но статьи в ней нет и после ретрая не появится. Разбор мог уже лежать
в кеше (контент исчез/стал не-статьёй позже) — тогда отдаётся прошлый разбор;
при пустом кеше статус редактируется вежливым отказом. В обоих случаях джоба
завершается штатно (mark_done воркером), error-сообщение в тред не уходит.
"""

import asyncio
import hashlib
import json
import logging
import re
from dataclasses import dataclass
from typing import Any

import discord

from bot.analysis.author import AUTHOR_PROMPTS_VER, AuthorFacts, facts_to_dict
from bot.analysis.prompts import load_prompts
from bot.analysis.report import Report, ReportBlock, render_report_messages, validate_budgets
from bot.config import BotConfig
from bot.delivery.outbox import OutboxPort
from bot.delivery.render import format_for_discord, truncate_message
from bot.delivery.sender import SafeSender, resolve_channel
from bot.sources.base import NotAnArticle

log = logging.getLogger("yt-summary")

# Версия схемы отчёта — часть ключа кеша (ARCHITECTURE.md §6): фаза 2 перевела
# отчёт на блоки (JSON-ответ LLM), старые записи summary-1 инвалидируются сами.
REPORT_SCHEMA = "blocks-1"

_ERROR_TEMPLATE = "Не смог сделать разбор этого видео: {reason}"

# Вежливый отказ при NotAnArticle — дублируется дефолтом на случай вырожденного
# prompts.yaml; боевой текст живёт в prompts.yaml (ключ not_an_article)
_NOT_AN_ARTICLE_DEFAULT = (
    "🤔 По ссылке не нашлось статьи с текстом. Если это видео — дай ссылку на YouTube."
)


@dataclass
class Deps:
    """Явные зависимости пайплайна (вместо глобалов монолита); в тестах — фейки.

    Поля фаз 3-4 могут быть None (None-безопасность): без провайдера фактов или
    хранилища авторов автор-путь просто пропускается; без jobs/dedup исходы
    джоб и дедуп не фиксируются (поведение фазы 3).
    """

    config: BotConfig
    llm: Any  # LLMClient | фейк из conftest
    outbox: OutboxPort  # PendingOutbox | DbOutbox — оба структурно соответствуют порту
    prompts: dict | None  # prompts.yaml в памяти; None — лениво прочитать с диска
    sender: SafeSender
    client: Any  # discord.Client | фейк из conftest
    queue: asyncio.Queue
    reports: Any  # ReportStore | фейк из conftest — кеш отчётов по ключу фазы 2
    # фаза 3: провайдер фактов автора, кеш обзоров, фоновая очередь обновления
    # и общий лок LLM-вызовов (контент-разбор и обзор автора не пересекаются)
    author_facts: Any = None  # YouTubeAuthorFactsProvider | фейк из conftest
    authors: Any = None  # AuthorStore | фейк из conftest
    background_queue: Any = None  # asyncio.Queue задач {"platform", "handle"}
    llm_lock: Any = None  # asyncio.Lock | None — сериализация LLM-вызовов
    # фаза 4: реестр экстракторов (Router), задачи и дедуп сообщений в БД
    router: Any = None  # bot.router.Router | фейк из conftest
    jobs: Any = None  # JobStore | фейк из conftest — исходы джоб в БД
    dedup: Any = None  # DedupStore | фейк из conftest — дедуп по message_id
    # UX-итерация: провайдер топ-комментариев (ознакомительный обзор реакций)
    comments: Any = None  # YouTubeCommentsProvider | фейк из conftest


def _content_hash(content: str) -> str:
    """Хэш контента — четвёртая часть ключа кеша (короткий, для читаемости ключа)."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]


async def llm_call(deps: "Deps", method: str, arg) -> Any:
    """Единственная дверь к LLM: исполнение в потоке + deps.llm_lock (если задан).

    Методы LLM-клиента синхронные, поэтому asyncio.to_thread; лок — барьер между
    llm_worker и author_refresh_worker, чтобы их вызовы не пересекались
    (провайдер лимитирует, см. AGENTS.md «один LLM-воркер»).
    """
    lock = getattr(deps, "llm_lock", None)
    call = getattr(deps.llm, method)
    if lock is None:
        return await asyncio.to_thread(call, arg)
    async with lock:
        return await asyncio.to_thread(call, arg)


def _author_block(ref, facts, review: str) -> ReportBlock:
    """Автор-блок отчёта; data связывает блок с ключом кеша авторов (HIT-путь)."""
    return ReportBlock(
        "author",
        title="Об авторе",
        body_md=review,
        data={
            "platform": getattr(ref, "platform", "") or "",
            "handle": getattr(ref, "handle", "") or "",
            "name": getattr(facts, "name", "") or "",
            "url": getattr(facts, "url", "") or "",
        },
    )


def _author_ttl_seconds(deps: "Deps") -> int:
    return deps.config.author_ttl_days * 86400


def _author_facts_from_row(row) -> AuthorFacts:
    """Факты для автор-блока из строки кеша авторов (свежая строка спасает без сети).

    display_name — имя канала, url достаётся из facts_json через
    AuthorFacts.from_dict. Битый facts_json не роняет джобу: недостающие
    поля остаются пустыми строками.
    """
    try:
        stored = AuthorFacts.from_dict(json.loads(row.get("facts_json") or "{}"))
    except (TypeError, ValueError):
        stored = AuthorFacts()
    return AuthorFacts(name=row.get("display_name") or stored.name, url=stored.url)


def _author_row_fresh(row, deps: "Deps") -> bool:
    """Строка автора свежа: не устарела по TTL И записана текущей версией
    автор-промпта (AUTHOR_PROMPTS_VER) — смена формата обзора перестраивает
    старые строки при первом же запросе, не дожидаясь TTL."""
    if not row or not row.get("review_json"):
        return False
    if str(row.get("prompts_ver") or "") != AUTHOR_PROMPTS_VER:
        return False
    authors = deps.authors
    try:
        return authors.is_fresh(row, _author_ttl_seconds(deps))
    except Exception as e:
        log.warning("проверка свежести автора упала: %s", e)
        return False


async def _attach_author_block(
    deps: "Deps", report: Report, url: str, prompts_ver: str, budgets: dict | None
) -> None:
    """MISS-путь автора: ref по url -> кеш авторов -> (мимо кеша) факты -> обзор.

    Кеш автора читается ДО сети: свежая строка отдаёт обзор как записан — без
    get_channel (страница канала качается только когда кеш автора не спасает)
    и без LLM, факты для блока собираются из строки. Иначе факты канала
    добывает провайдер, обзор — review_author. Блок добавляется в отчёт ДО
    кеширования — в кеш попадает отчёт уже с автором. Любой сбой пути (ref'а
    нет, фактов нет, обзор не вышел, кеш автора упал) оставляет отчёт без
    автора: доставка разбора не должна зависеть от обзора автора.
    """
    provider = getattr(deps, "author_facts", None)
    authors = getattr(deps, "authors", None)
    if provider is None:
        return
    try:
        ref = await asyncio.to_thread(provider.get_author_ref, url)
    except Exception as e:
        log.warning("ref автора не получен (%s): %s", url, e)
        return
    if ref is None:
        return
    platform = getattr(ref, "platform", "") or ""
    handle = getattr(ref, "handle", "") or ""
    if not handle:
        return

    # кеш автора — ДО сети: свежая строка спасает без страницы канала и LLM
    try:
        row = authors.get(platform, handle) if authors is not None else None
        fresh = _author_row_fresh(row, deps)
    except Exception as e:
        # сбой кеша автора — деградация до без-автор-блока, а не падение джобы:
        # строка считается отсутствующей, обзор строится в LLM как на промахе
        log.warning("кеш автора недоступен (%s/%s): %s", platform, handle, e)
        row, fresh = None, False

    if fresh:
        # свежая строка кеша: обзор берётся как записан, факты — из строки
        review = row["review_json"]
        facts = _author_facts_from_row(row)
    else:
        try:
            facts = await asyncio.to_thread(provider.get_channel, ref)
        except Exception as e:
            log.warning("факты автора не получены (%s): %s", url, e)
            return
        if facts is None:
            return
        try:
            review = await llm_call(deps, "review_author", facts)
        except Exception as e:
            log.warning("обзор автора не вышел (%s): %s", url, e)
            return

    report.blocks.append(_author_block(ref, facts, review))
    # бюджет author-блока проверяется отдельно: блок появляется уже после общей
    # валидации отчёта в process_job (превышение не фатально, рендер укоротит)
    for warning in validate_budgets(Report([report.blocks[-1]]), budgets):
        log.warning("%s: %s", url, warning)
    if fresh:
        return

    if authors is not None:
        try:
            authors.put(
                platform,
                handle,
                getattr(facts, "name", "") or "",
                json.dumps(facts_to_dict(facts), ensure_ascii=False),
                review,
                AUTHOR_PROMPTS_VER,
            )
        except Exception:
            log.exception("обзор автора %s/%s не закеширован", platform, handle)


async def _attach_comments_block(
    deps: "Deps", report: Report, url: str, budgets: dict | None
) -> None:
    """MISS-путь комментариев: топ популярных комментариев -> краткий LLM-обзор.

    Блок добавляется в отчёт ДО кеширования — как автор: на кеш-хите блок
    приходит из payload без повторной добычи. Любой сбой (провайдера нет,
    комментарии отключены/недоступны, обзор не вышел) оставляет отчёт без
    блока: доставка разбора не зависит от обзора комментариев.
    """
    provider = getattr(deps, "comments", None)
    if provider is None:
        return
    try:
        comments = await asyncio.to_thread(provider.get, url)
    except Exception as e:
        log.warning("комментарии не получены (%s): %s", url, e)
        return
    if not comments:
        return
    try:
        review = await llm_call(deps, "comments_review", comments)
    except Exception as e:
        log.warning("обзор комментариев не вышел (%s): %s", url, e)
        return
    report.blocks.append(ReportBlock("comments", body_md=review))
    # бюджет comments-блока проверяется отдельно — как у автора: блок появляется
    # после общей валидации отчёта (превышение не фатально, рендер укоротит)
    for warning in validate_budgets(Report([report.blocks[-1]]), budgets):
        log.warning("%s: %s", url, warning)


def _queue_stale_author_refresh(deps: "Deps", report: Report) -> None:
    """HIT-путь автора: пользователь сразу получает старый блок из payload.

    Строка автора отсутствует или устарела — фоновая задача обновления;
    пользователь никогда не ждёт фона. Автор-блока нет — тишина.
    """
    queue = getattr(deps, "background_queue", None)
    if queue is None:
        return
    block = next((b for b in report.blocks if b.kind == "author"), None)
    if block is None:
        return
    data = block.data or {}
    platform, handle = data.get("platform"), data.get("handle")
    if not platform or not handle:
        return
    authors = getattr(deps, "authors", None)
    if authors is None:
        return
    try:
        row = authors.get(platform, handle)
        fresh = row is not None and _author_row_fresh(row, deps)
    except Exception as e:
        # сбой кеша автора — деградация до без-автор-блока, а не падение джобы:
        # не зная свежести строки, фон не ставим (отчёт уже доставлен из payload)
        log.warning("кеш автора недоступен (%s/%s): %s", platform, handle, e)
        return
    if fresh:
        return
    queue.put_nowait({"platform": platform, "handle": handle})


TITLE_CLIP = 20  # длина названия в заголовке разбора; полное название — в имени треда


def _disclaimer_header(job: dict) -> str:
    """Заголовок первого сообщения треда: «Разбор: Название (Автор)».

    Ссылки в заголовке нет нарочно: голый URL в сообщении даёт превью-встройку
    в Discord. Нет describe-метаданных (сбой сети, статья) — маскированная
    ссылка <url>: она кликабельна, но встраивание не создаёт.
    """
    title = job.get("display_title") or ""
    if not title:
        return f"Разбор: <{job['url']}>"
    clipped = title[:TITLE_CLIP] + ("…" if len(title) > TITLE_CLIP else "")
    author = job.get("display_author") or ""
    return f"Разбор: {clipped} ({author})" if author else f"Разбор: {clipped}"


async def _deliver(deps: "Deps", thread, messages: list[str], label: str) -> bool:
    """Отправляет сообщения треду; при сбое буферизует ПОЛНЫЙ хвост отчёта.

    Чанки всех сообщений предвычислены заранее, чтобы при сбое одним проходом
    буферизовать остаток текущего сообщения плюс все последующие: иначе сбой
    в середине навсегда терял бы конец разбора; порядок доставки сохраняется.
    True — всё доставлено; False — Discord недоступен, хвост в outbox.
    """
    chunks_per_message = [
        truncate_message(format_for_discord(message), deps.config.split_threshold)[
            : deps.config.max_split_messages
        ]
        for message in messages
    ]

    delivered = 0
    for i, chunks in enumerate(chunks_per_message):
        for idx, chunk in enumerate(chunks):
            if await deps.sender.send(thread, chunk):
                delivered += 1
                continue
            # хвост с упавшего чанка до конца отчёта — в порядке рендера
            tail = chunks[idx:] + [c for later in chunks_per_message[i + 1 :] for c in later]
            for buffered in tail:
                deps.outbox.append(thread.id, buffered)
            log.warning("%s: буферизовано %d чанков (Discord недоступен)", label, len(tail))
            return False
    log.info("готово: %s (%d сообщений, %d чанков)", label, len(messages), delivered)
    return True


async def _close_status(status) -> None:
    """Закрывает статусное сообщение («готово»/ошибка уже доставлены в тред)."""
    if not status:
        return
    try:
        await status.delete()
    except discord.HTTPException:
        pass


async def process_job(deps: Deps, job: dict) -> None:
    """Полный цикл: extract -> кеш/LLM -> рендер -> отправка в тред.

    Контракт исхода джобы (фаза 4): успех и «тред недоступен» возвращаются
    штатно (воркер поставит mark_done), любой сбой разбора пробрасывается
    ПОСЛЕ доставки error-сообщения и закрытия статуса (воркер поставит
    mark_failed — джоба не повторится после рестарта, ошибка уже показана).
    """
    url = job["url"]
    canonical = job["canonical_id"]
    extractor = deps.router.by_name(job["kind"])
    thread = await resolve_channel(deps.client, job["thread_id"])
    if thread is None:
        log.error("тред %s недоступен, задача %s отброшена", job["thread_id"], url)
        return

    status = job.get("status")
    link = url

    try:
        if extractor is None:
            # kind джобы нет в реестре (экстрактор убрали из build_deps, джоба
            # из старой версии): человеческий текст вместо AttributeError на
            # None.extract — попадает в error-путь ниже (сообщение пользователю,
            # закрытие статуса, проброс — воркер пометит джобу failed)
            raise RuntimeError(f"неизвестный тип источника: {job['kind']}")

        # промпты могут быть не заданы (обратная совместимость) — лениво читаем
        # с диска; версия промптов и бюджеты берутся из одного словаря
        prompts = deps.prompts
        if prompts is None:
            prompts = load_prompts()
        prompts_ver = str(prompts.get("version", "0"))
        budgets = prompts.get("budgets")

        # контент добывается ВСЕГДА: content_hash считается из него даже при
        # попадании в кеш (изменившийся контент должен дать новый отчёт)
        doc = None
        try:
            doc = await asyncio.to_thread(extractor.extract, url)
        except NotAnArticle as no_article:
            # «по ссылке нет статьи» — не сетевой сбой (ветка ДО общего except):
            # страница отдаётся, но статьи в ней нет и после ретрая не появится.
            # Разбор мог уже лежать в кеше (контент исчез или стал не-статьёй
            # позже) — отдаём прошлый разбор, как и в ветке сетевого сбоя:
            # свежесть здесь недостижима, а без фолбэка пользователь не получил
            # бы ничего. Кеш пуст — вежливый отказ, error-сообщение в тред не
            # уходит, исключение не пробрасывается — воркер завершит джобу
            # штатно (mark_done)
            cached = deps.reports.get_latest(canonical, prompts_ver, REPORT_SCHEMA)
            if cached is None:
                log.info("по ссылке нет статьи (%s): %s", canonical, no_article)
                rejection = str(prompts.get("not_an_article") or _NOT_AN_ARTICLE_DEFAULT)
                if status:
                    try:
                        await status.edit(content=rejection)
                    except discord.HTTPException:
                        pass
                return
            log.info(
                "текст по ссылке исчез/стал не-статьей, отдаю прошлый разбор: %s", no_article
            )
            report = Report.from_json(cached)
        except Exception as extract_err:
            # Трейд-офф «свежесть vs доступность» — осознанно отдаём возможное
            # устаревшее: контент мог исчезнуть у источника навсегда, и тогда без
            # фолбэка пользователь не получит ничего, хотя разбор уже лежит в
            # кеше под прежним content_hash (свежий хэш посчитать не из чего).
            cached = deps.reports.get_latest(canonical, prompts_ver, REPORT_SCHEMA)
            if cached is None:
                raise
            log.info("контент недоступен, отдаю прошлый разбор из кеша: %s", extract_err)
            report = Report.from_json(cached)
        else:
            link = doc.url  # ссылка для рендера — нормализованная форма экстрактора
            content_hash = _content_hash(doc.content)
            payload = deps.reports.get(canonical, prompts_ver, REPORT_SCHEMA, content_hash)
            if payload is not None:
                log.info("разбор из кеша: %s", canonical)
                report = Report.from_json(payload)
                # HIT: автор-блок из payload, провайдер и LLM не трогаются;
                # строка автора устарела или отсутствует — обновление в фоне
                _queue_stale_author_refresh(deps, report)
            else:
                report = await llm_call(deps, "analyze_content", doc.content)
                if not report.blocks:
                    # пустой отчёт нельзя ни кешировать, ни отправлять: пользователь
                    # останется без сообщений с висящим статусом
                    raise RuntimeError("LLM вернул пустой отчёт")
                # превышение бюджета не фатально: рендер укоротит блок, но в логе
                # остаётся след для настройки промптов
                for warning in validate_budgets(report, budgets):
                    log.warning("%s: %s", canonical, warning)
                # MISS: автор-блок строится до кеширования — в кеш попадает
                # отчёт уже с автором
                await _attach_author_block(deps, report, url, prompts_ver, budgets)
                await _attach_comments_block(deps, report, url, budgets)
                # в кеш кладём только успешный разбор: ошибки extract/LLM
                # повторно разберутся на следующей попытке
                deps.reports.put(canonical, prompts_ver, REPORT_SCHEMA, report.to_json(), content_hash)

        messages = render_report_messages(
            report, link, budgets=budgets, section_budget=deps.config.split_threshold
        )
        if messages:
            # заголовок разбора — только в первом сообщении; без ссылок, чтобы
            # Discord не рисовал превью: «Название (Автор)» (название обрезано
            # до TITLE_CLIP, полное — в имени треда), без метаданных —
            # маскированная ссылка <url>: кликабельна, встраивания не создаёт
            messages[0] = f"**{_disclaimer_header(job)}**\n\n{messages[0]}"
    except Exception as e:
        log.warning("разбор не вышел (%s): %s", canonical, e)
        prompts = deps.prompts
        if prompts is None:
            # промпты не переданы в Deps: ленивое чтение с диска — один раз,
            # выше, для ключа кеша; при заданном deps.prompts error-ветка
            # диск не читает (шаблон берётся из памяти)
            prompts = load_prompts()
        # LLM/провайдер могут вернуть текст с mention — не позволяем боту тегать
        # людей: @everyone/@here и начало <@-пинга вырезаются из причины ошибки
        reason = re.sub(r"@everyone|@here|<@", "", str(e))
        error_text = prompts.get("error", _ERROR_TEMPLATE).format(reason=reason)
        delivered = await _deliver(
            deps, thread, [f"**{_disclaimer_header(job)}**\n\n{error_text}"], canonical
        )
        # джоба упала: статус закрываем ТОЛЬКО если error-сообщение доставлено —
        # при буферизации оно остаётся (симметрично success-ветке: досылка хвоста
        # из outbox продолжит разбор в треде). Пробрасываем исключение —
        # llm_worker запишет mark_failed в БД
        if delivered:
            await _close_status(status)
        raise

    if await _deliver(deps, thread, messages, canonical):
        # статус закрывается только после полной доставки; при буферизации он
        # остаётся: досылка хвоста из outbox продолжит разбор в треде
        await _close_status(status)
