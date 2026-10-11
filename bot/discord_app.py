"""Discord-адаптер: события, треды, статусные сообщения, дедуп, recovery.

discord.Client создаётся ТОЛЬКО в фабрике create_client(): импорт пакета
остаётся чистым — без токенов, без .env и без клиентов (см.
tests/unit/test_import_clean.py). Обработчики вешаются отдельно в
attach_handlers(), когда deps уже собран.

Фаза 4: маршрут сообщения определяет Router (реестр экстракторов), дедуп и
джобы живут в БД (in-memory set удалён — дедуп переживает рестарт); недоделанные
джобы после рестарта возвращаются в очередь recover_jobs'ом ДО старта воркеров.
"""

import asyncio
import logging
import re

import discord

from bot.jobs import author_refresh_worker, llm_worker

log = logging.getLogger("yt-summary")


def default_intents() -> discord.Intents:
    """Интенты как в монолите: сообщения, их содержимое и структура сервера."""
    intents = discord.Intents.default()
    intents.message_content = True
    intents.messages = True
    intents.guilds = True
    return intents


def create_client() -> discord.Client:
    """Фабрика клиента: единственное место, где создаётся discord.Client."""
    return discord.Client(intents=default_intents())


def thread_name_from_message(text: str, route) -> str:
    """Имя треда: текст сообщения без ссылки (срез границ роутера).

    Осталось пусто (сообщение было только ссылкой) — «Разбор <url>»; лимит
    Discord — 100 символов, режем до 90 с запасом.
    """
    stripped = re.sub(r"\s+", " ", (text[: route.start] + text[route.end :])).strip()
    return stripped[:90] if stripped else f"Разбор {route.url}"[:90]


async def find_existing_thread(message: discord.Message):
    """Ищет тред, созданный из этого сообщения (его id == message.id).

    Нужно после перезапуска бота: тред мог быть создан прошлым запуском,
    а дедуп в БД его не помнит — Discord не даст создать второй тред (160004).
    """
    ch = message.channel
    for t in list(getattr(ch, "threads", []) or []):
        if t.id == message.id:
            return t
    try:
        async for t in ch.archived_threads(limit=50):
            if t.id == message.id:
                return t
    except discord.HTTPException:
        pass
    return None


async def _fetch_status(client, job: dict):
    """Статусное сообщение восстановленной джобы; любой сбой — None.

    Тред мог быть удалён, статусное сообщение — стёрто вручную: разбор потерять
    нельзя, а статус опционален (пайплайн допускает джобу без статуса).
    """
    try:
        thread = client.get_channel(job.get("thread_id"))
        return await thread.fetch_message(job.get("status_message_id"))
    except Exception:
        return None


async def recover_jobs(deps) -> None:
    """Возвращает в очередь недоделанные джобы из БД (state=new) после рестарта.

    Вызывается однократно из on_ready ДО старта воркеров (общий с ними guard):
    иначе джоба могла бы обработаться дважды — из recovery и из очереди, куда
    её уже поставили. Статус прикрепляется через fetch_message; джоба без
    статуса всё равно ставится — разбор потерять нельзя.
    """
    jobs = getattr(deps, "jobs", None)
    if jobs is None:
        return
    pending = jobs.take_new()
    if not pending:
        return
    log.info("восстанавливаю после рестарта джоб: %d", len(pending))
    for job in pending:
        job["status"] = await _fetch_status(deps.client, job)
        deps.queue.put_nowait(job)


async def handle(message: discord.Message, deps) -> None:
    """Роутинг: ссылка -> экстрактор -> дедуп -> тред + статус -> джоба в БД и очередь."""
    route = deps.router.route(message.content or "")
    if route is None:
        return
    url = route.url
    canonical = route.extractor.canonical_id(url)

    dedup = getattr(deps, "dedup", None)
    if dedup is not None and not dedup.mark(str(message.id), canonical):
        return

    log.info("источник %s в #%s от %s", url, message.channel, message.author)

    status = None
    try:
        status = await message.reply("🎬 Разбираю ссылку, готовлю тезисы...")
    except discord.HTTPException:
        pass

    try:
        thread = await message.create_thread(
            name=thread_name_from_message(message.content, route),
            auto_archive_duration=1440,
        )
    except discord.Forbidden:
        log.error("нет права Create Public Threads в #%s", message.channel)
        if status:
            await status.edit(content="⚠️ Нет прав создать тред в этом канале.")
        return
    except discord.HTTPException as e:
        # 160004: тред под этим сообщением уже есть (создан до рестарта бота
        # или вторым источником события) — переиспользуем, не сдаёмся
        if getattr(e, "code", None) == 160004 or "160004" in str(e):
            thread = await find_existing_thread(message)
        if thread is None:
            log.error("не смог создать тред: %s", e)
            if status:
                await status.edit(content="⚠️ Не удалось создать тред (лимиты Discord?).")
            return
        log.info("тред уже существует, пишу в него: %s", thread.id)

    position = deps.queue.qsize()
    if status and position:
        try:
            await status.edit(content=f"🎬 Принято, позиция в очереди: {position + 1}")
        except discord.HTTPException:
            pass

    # payload в БД — без status-объекта (не сериализуется); статус прикрепляется
    # к джобе в очереди, после рестарта восстанавливается по status_message_id
    payload = {
        "kind": route.extractor.name,
        "url": url,
        "canonical_id": canonical,
        "thread_id": thread.id,
        "message_id": message.id,
        "status_message_id": status.id if status else None,
    }
    jobs = getattr(deps, "jobs", None)
    try:
        job_id = jobs.put(payload) if jobs is not None else None
    except Exception:
        # БД недоступна: без записи джоба пережила бы рестарт только молчаливой
        # потерей — в очередь её не ставим, пользователь получает честный отказ
        # (исключение не пробрасывается: on_message не должен логировать падение)
        log.exception("джоба не поставлена в БД (%s)", url)
        if status:
            try:
                await status.edit(
                    content="⚠️ Не удалось поставить разбор в очередь, попробуйте позже."
                )
            except discord.HTTPException:
                pass
        return
    await deps.queue.put({**payload, "job_id": job_id, "status": status})


def attach_handlers(client: discord.Client, deps) -> discord.Client:
    """Вешает on_ready/on_message на клиента; deps должен быть собран
    (bot.main.build_deps) — обработчики читают deps.sender и deps.queue на событии."""
    bg_started = False

    async def on_ready() -> None:
        nonlocal bg_started
        log.info("запущен как %s (серверов: %d)", client.user, len(client.guilds))
        if not bg_started:
            bg_started = True
            # восстановление джоб из БД — строго ДО старта воркеров (общий guard
            # с ними: переподключение восстановление не дублирует)
            try:
                await recover_jobs(deps)
            except Exception:
                log.exception("восстановление джоб после рестарта упало")
            asyncio.create_task(llm_worker(deps.queue, deps))
            asyncio.create_task(deps.sender.flush_loop())
            # фазовое обновление обзоров авторов — тоже бесконечный воркер,
            # стартует однократно вместе с остальным фоном
            background_queue = getattr(deps, "background_queue", None)
            if background_queue is not None:
                asyncio.create_task(author_refresh_worker(background_queue, deps))
        # вернулись онлайн — сразу досылаем то, что накопилось за офлайн
        try:
            await deps.sender.flush_pending()
        except Exception:
            log.exception("досылка буфера при старте упала")

    async def on_message(message: discord.Message) -> None:
        if message.author.bot:
            return
        try:
            await handle(message, deps)
        except Exception:
            log.exception("ошибка обработки сообщения %s", message.id)

    client.event(on_ready)
    client.event(on_message)
    return client
