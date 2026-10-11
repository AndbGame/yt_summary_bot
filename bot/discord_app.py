"""Discord-адаптер: события, треды, статусные сообщения, дедуп.

discord.Client создаётся ТОЛЬКО в фабрике create_client(): импорт пакета
остаётся чистым — без токенов, без .env и без клиентов (см.
tests/unit/test_import_clean.py). Обработчики вешаются отдельно в
attach_handlers(), когда deps уже собран.
"""

import asyncio
import logging

import discord

from bot.jobs import author_refresh_worker, llm_worker
from bot.sources.youtube import extract_video_id, thread_name_from_message

log = logging.getLogger("yt-summary")

# защита от повторной обработки (редакты/репосты той же ссылки); in-memory
# осознанно — персистентный дедуп появится в БД (фаза 4, ARCHITECTURE.md §10)
processed: set = set()


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


async def find_existing_thread(message: discord.Message):
    """Ищет тред, созданный из этого сообщения (его id == message.id).

    Нужно после перезапуска бота: тред мог быть создан прошлым запуском,
    а in-memory дедуп потерян — Discord не даст создать второй тред (160004).
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


async def handle(message: discord.Message, deps) -> None:
    """Роутинг: YouTube-ссылка -> тред + статус -> задача в очередь (дедуп «id:видео»)."""
    found = extract_video_id(message.content)
    if not found:
        return
    video_id = found[0]
    key = f"{message.id}:{video_id}"
    if key in processed:
        return
    processed.add(key)
    if len(processed) > 2000:
        processed.clear()

    log.info("видео %s в #%s от %s", video_id, message.channel, message.author)

    status = None
    thread = None
    try:
        status = await message.reply("🎬 Смотрю субтитры, готовлю тезисы...")
    except discord.HTTPException:
        pass

    try:
        thread = await message.create_thread(
            name=thread_name_from_message(message.content, video_id),
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

    await deps.queue.put({
        "video_id": video_id,
        "thread_id": thread.id,
        "message_id": message.id,
        "status": status,
    })


def attach_handlers(client: discord.Client, deps) -> discord.Client:
    """Вешает on_ready/on_message на клиента; deps должен быть собран
    (bot.main.build_deps) — обработчики читают deps.sender и deps.queue на событии."""
    bg_started = False

    async def on_ready() -> None:
        nonlocal bg_started
        log.info("запущен как %s (серверов: %d)", client.user, len(client.guilds))
        if not bg_started:
            bg_started = True
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
