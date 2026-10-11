#!/usr/bin/env python3
"""Discord bot: создаёт тред под каждым сообщением с YouTube-ссылкой
и постит туда тезисное саммари видео (по субтитрам + LLM).

Надёжность:
  - все задачи идут через одну очередь: не более одного LLM-запроса одновременно;
  - лимиты LLM (429) и сетевые сбои API ретраятся с экспоненциальной задержкой;
  - если Discord недоступен, ответы накапливаются в pending_replies.json
    и досылаются автоматически, когда бот вернётся онлайн.

Настройка через переменные окружения или .env рядом со скриптом:
  DISCORD_BOT_TOKEN  — токен бота (обязательно)
  LLM_API_KEY        — ключ OpenAI-совместимого API (обязательно)
  LLM_BASE_URL       — базовый URL API (по умолчанию https://api.openai.com/v1)
  LLM_MODEL          — модель (по умолчанию gpt-4o-mini)

Запуск:  python yt_summary_bot.py
"""

import asyncio
import json
import logging
import os
import random
import re
import sys
import time
from pathlib import Path

import aiohttp
import discord
import requests
import yaml
import youtube_transcript_api
from openai import OpenAI, RateLimitError, APIConnectionError, APITimeoutError

# ---------------------------------------------------------------- config ---

def load_env():
    """Читает .env рядом со скриптом (простенько, без внешних зависимостей)."""
    env_path = Path(__file__).parent / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip().strip("'\""))

load_env()

BOT_TOKEN   = os.environ.get("DISCORD_BOT_TOKEN")
LLM_API_KEY = os.environ.get("LLM_API_KEY")
LLM_BASE_URL= os.environ.get("LLM_BASE_URL", "https://api.openai.com/v1")
LLM_MODEL   = os.environ.get("LLM_MODEL", "gpt-4o-mini")

# Discord-лимиты (как в адаптере Hermes: 2000 лимит, сплит на 1900, макс 8 чанков)
MAX_MESSAGE_LENGTH = 2000
SPLIT_THRESHOLD    = 1900
MAX_SPLIT_MESSAGES = 8

# Надёжность
LLM_MAX_ATTEMPTS  = int(os.environ.get("LLM_MAX_ATTEMPTS", "5"))   # попыток на один summarize
LLM_MAX_BACKOFF   = int(os.environ.get("LLM_MAX_BACKOFF", "120"))  # макс пауза между попытками, сек
SEND_MAX_ATTEMPTS = 3          # попыток отправить чанк в Discord до буферизации
FLUSH_INTERVAL_S  = 60         # период фоновой досылки буфера
TRANSCRIPT_LIMIT  = 50000      # обрезка субтитров для LLM

# Промпты — в prompts.yaml рядом со скриптом
def load_prompts():
    p = Path(__file__).parent / "prompts.yaml"
    if p.exists():
        return yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return {}

PROMPTS = load_prompts()

# Персистентный буфер недоставленных сообщений
PENDING_FILE = Path(__file__).parent / "pending_replies.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("yt-summary")

# --------------------------------------------------------------- youtube ---

YT_RE = re.compile(
    r"(?:https?://)?(?:www\.|m\.)?"
    r"(?:youtube\.com/(?:watch\?.*?v=|shorts/|live/|embed/)|youtu\.be/)"
    r"([\w-]{11})"
)


def extract_video_id(text: str):
    """Возвращает (video_id, start, end) или None. start/end — границы совпадения."""
    m = YT_RE.search(text or "")
    if not m:
        return None
    return m.group(1), m.start(), m.end()


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


def fetch_transcript(video_id: str) -> str:
    """Скачивает субтитры (ru -> en -> любые), возвращает текст с таймкодами."""
    api = youtube_transcript_api.YouTubeTranscriptApi()
    try:
        tr = api.fetch(video_id, languages=["ru", "en"])
    except Exception:
        tr_list = api.list(video_id)
        codes = [t.language_code for t in tr_list]
        if not codes:
            raise RuntimeError("нет доступных субтитров")
        tr = api.fetch(video_id, languages=codes)
    lines = []
    for s in tr:
        m, sec = divmod(int(s.start), 60)
        h, mm = divmod(m, 60)
        stamp = f"{h}:{mm:02d}:{sec:02d}" if h else f"{mm:02d}:{sec:02d}"
        lines.append(f"[{stamp}] {s.text}")
    return "\n".join(lines)

# ------------------------------------------------------------------- llm ---

def _retry_after_seconds(exc, cap: float = LLM_MAX_BACKOFF):
    """Достаёт Retry-After из ответа API (секунды), если провайдер его прислал."""
    resp = getattr(exc, "response", None)
    headers = getattr(resp, "headers", None) or {}
    for key in ("retry-after", "Retry-After", "x-ratelimit-reset-requests"):
        raw = headers.get(key)
        if raw:
            try:
                return min(cap, max(1.0, float(raw)))
            except (TypeError, ValueError):
                continue
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        try:
            return min(cap, max(1.0, float(body.get("error", {}).get("retry_after"))))
        except (TypeError, ValueError):
            pass
    return None


def summarize(transcript: str) -> str:
    """Саммари через LLM. Ретраи на 429/лимиты и сетевые сбои, пауза экспоненциальная.

    Бросает исключение только после LLM_MAX_ATTEMPTS неудач — вызывающий код
    тогда отправляет в тред текст ошибки из prompts.yaml.
    """
    client = OpenAI(api_key=LLM_API_KEY, base_url=LLM_BASE_URL)
    messages = [
        {"role": "system", "content": PROMPTS.get("system", "Ты кратко и по делу пересказываешь видео по субтитрам.")},
        {"role": "user", "content": PROMPTS.get("user", "").format(transcript=transcript[:TRANSCRIPT_LIMIT])},
    ]
    last_exc = None
    for attempt in range(1, LLM_MAX_ATTEMPTS + 1):
        try:
            resp = client.chat.completions.create(
                model=LLM_MODEL,
                messages=messages,
                max_tokens=2000,
                temperature=0.3,
            )
            content = (resp.choices[0].message.content or "").strip()
            if content:
                return content
            raise RuntimeError("LLM вернул пустой ответ")
        except RateLimitError as e:
            wait = _retry_after_seconds(e) or min(LLM_MAX_BACKOFF, 5 * 2 ** (attempt - 1))
            log.warning("LLM: лимит запросов (попытка %d/%d), пауза %.0fс", attempt, LLM_MAX_ATTEMPTS, wait)
            last_exc = e
        except (APIConnectionError, APITimeoutError) as e:
            wait = min(LLM_MAX_BACKOFF, 3 * 2 ** (attempt - 1))
            log.warning("LLM: сетевой сбой (попытка %d/%d), пауза %.0fс: %s", attempt, LLM_MAX_ATTEMPTS, wait, e)
            last_exc = e
        except RuntimeError as e:
            # пустой ответ — короткая пауза и повтор
            wait = 2 * attempt
            log.warning("LLM: %s (попытка %d/%d)", e, attempt, LLM_MAX_ATTEMPTS)
            last_exc = e
        # джиттер, чтобы при пачке запросов не биться синхронно
        time.sleep(wait + random.uniform(0, 1))
    raise last_exc or RuntimeError("LLM: исчерпаны попытки")

# --------------------------------------------- discord-форматирование (из Hermes) ---
# Портировано из gateway/platforms/helpers.py и base.py агента Hermes:
#   convert_table_to_bullets / _render_table_block — GFM-таблицы не рендерятся
#   в Discord, превращаем их в "**заголовок** + • поле: значение";
#   truncate_message — сплит длинных сообщений с закрытием/переоткрытием
#   code-fence на границах и индикаторами (1/N).

_TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-{3,}.*\|.*$")
_TABLE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$")


def _split_table_row(line: str) -> list:
    line = line.strip().strip("|")
    return [c.strip() for c in line.split("|")]


def _render_table_block(table_block: list) -> str:
    headers = _split_table_row(table_block[0]) if len(table_block) >= 3 else []
    if len(headers) < 2:
        return "\n".join(table_block)
    groups = []
    for index, row in enumerate(table_block[2:], start=1):
        cells = _split_table_row(row)
        heading = next((c for c in cells if c), f"Строка {index}")
        data_cells = (cells + [""] * len(headers))[: len(headers)]
        bullets = [f"• {h}: {v}" for h, v in zip(headers, data_cells) if v != heading]
        groups.append("\n".join([f"**{heading}**", *bullets]))
    return "\n\n".join(groups)


def convert_table_to_bullets(text: str) -> str:
    """GFM pipe-таблицы -> жирный заголовок + буллеты; fenced код не трогаем."""
    if "|" not in text or "-" not in text:
        return text
    lines = text.split("\n")
    out, in_fence, i = [], False, 0
    while i < len(lines):
        line = lines[i]
        is_fence = line.lstrip().startswith("```")
        in_fence ^= is_fence
        if not (in_fence or is_fence) and "|" in line and i + 1 < len(lines) \
                and _TABLE_SEP_RE.match(lines[i + 1]):
            j = i + 2
            while j < len(lines) and _TABLE_ROW_RE.match(lines[j]):
                j += 1
            out.append(_render_table_block(lines[i:j]))
            i = j
        else:
            out.append(line)
            i += 1
    return "\n".join(out)


def format_for_discord(content: str) -> str:
    if not content:
        return content
    return convert_table_to_bullets(content)


def _fence_state_after(text: str, in_code: bool, lang: str):
    """(in_code, lang) после прохода по тексту; маркер языка берём с открывающего фенса."""
    for line in text.split("\n"):
        stripped = line.lstrip()
        if stripped.startswith("```"):
            if not in_code:
                in_code, lang = True, stripped[3:].strip()
            else:
                in_code = False
    return in_code, lang


def truncate_message(content: str, max_length: int = MAX_MESSAGE_LENGTH) -> list:
    """Сплит с сохранением code-fence: разрез внутри блока закрывает фенс в конце
    чанка и переоткрывает его (с тем же языком) в следующем; (1/N) индикаторы."""
    if len(content) <= max_length:
        return [content]
    INDICATOR_RESERVE = 10
    FENCE_CLOSE = "\n```"
    chunks, remaining, carry_lang = [], content, None
    while remaining:
        prefix = f"```{carry_lang}\n" if carry_lang is not None else ""
        headroom = max(1, max_length - INDICATOR_RESERVE - len(prefix) - len(FENCE_CLOSE))
        if len(prefix) + len(remaining) <= max_length - INDICATOR_RESERVE:
            final_chunk = prefix + remaining
            if carry_lang is not None and _fence_state_after(remaining, True, carry_lang)[0]:
                final_chunk += FENCE_CLOSE
            chunks.append(final_chunk)
            break
        region = remaining[:headroom]
        split_at = region.rfind("\n")
        if split_at < headroom // 2:
            split_at = region.rfind(" ")
        if split_at < 1:
            split_at = headroom
        chunk_body = remaining[:split_at]
        remaining = remaining[split_at:].lstrip()
        full_chunk = prefix + chunk_body
        in_code, lang = _fence_state_after(chunk_body, carry_lang is not None, carry_lang or "")
        carry_lang = lang if in_code else None
        chunks.append(full_chunk + FENCE_CLOSE if in_code else full_chunk)
    if len(chunks) > 1:
        chunks = [f"{c} ({i + 1}/{len(chunks)})" for i, c in enumerate(chunks)]
    return chunks

# ----------------------------------------------------------------- discord --

intents = discord.Intents.default()
intents.message_content = True
intents.messages = True
intents.guilds = True

client = discord.Client(intents=intents)
# защита от повторной обработки (редакты/репосты той же ссылки)
processed: set = set()

# Очередь задач: один worker -> не более одного LLM-запроса в момент времени
job_queue: asyncio.Queue = asyncio.Queue()

# ------------------------------------------------------ персистентный буфер --

def _pending_load() -> list:
    try:
        return json.loads(PENDING_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _pending_save(items: list) -> None:
    tmp = PENDING_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
    tmp.replace(PENDING_FILE)


def _pending_append(thread_id: int, content: str) -> None:
    items = _pending_load()
    items.append({"thread_id": thread_id, "content": content})
    _pending_save(items)
    log.warning("ответ буферизован (%d всего): тред %s", len(items), thread_id)


def _is_retryable_send_error(e: Exception) -> bool:
    """Сетевые сбои и 5xx — ретраебельны; 4xx (права/не найдено) — нет."""
    if isinstance(e, (aiohttp.ClientError, asyncio.TimeoutError)):
        return True
    if isinstance(e, discord.HTTPException):
        return getattr(e, "status", 0) >= 500
    return False


async def safe_send(channel, content: str) -> bool:
    """Отправка с ретраями на сетевых сбоях. False — надо буферизовать."""
    last_exc = None
    for attempt in range(1, SEND_MAX_ATTEMPTS + 1):
        try:
            await channel.send(content)
            return True
        except discord.HTTPException as e:
            if not _is_retryable_send_error(e):
                log.error("постоянная ошибка отправки в %s: %s", getattr(channel, "id", "?"), e)
                return False
            last_exc = e
        except Exception as e:  # ConnectionError и прочее
            last_exc = e
        wait = min(30, 3 * 2 ** (attempt - 1))
        log.warning("отправка не прошла (попытка %d/%d), пауза %ss: %s",
                    attempt, SEND_MAX_ATTEMPTS, wait, last_exc)
        await asyncio.sleep(wait)
    return False


async def _resolve_channel(thread_id: int):
    ch = client.get_channel(thread_id)
    if ch is None:
        try:
            ch = await client.fetch_channel(thread_id)
        except (discord.HTTPException, discord.NotFound, discord.InvalidData):
            return None
    return ch


async def flush_pending() -> None:
    """Досылает буферизованные ответы. Если сеть опять лежит — сохраняет остаток
    и выходит (порядок сообщений сохраняется)."""
    items = _pending_load()
    if not items:
        return
    if client.is_closed() or not client.is_ready():
        return
    log.info("досылаю буфер: %d сообщ.", len(items))
    remaining: list = []
    for idx, item in enumerate(items):
        ch = await _resolve_channel(int(item["thread_id"]))
        if ch is None:
            log.error("буфер: тред %s не найден, сообщение отброшено", item["thread_id"])
            continue
        ok = await safe_send(ch, item["content"])
        if not ok:
            # сеть лежит: сохраняем недоставленное и всё, что после него
            remaining = items[idx:]
            break
    _pending_save(remaining)
    if not remaining:
        log.info("буфер пуст — всё доставлено")


async def flush_loop():
    """Фоновый цикл: раз в минут пытается опустошить буфер."""
    while not client.is_closed():
        await asyncio.sleep(FLUSH_INTERVAL_S)
        try:
            await flush_pending()
        except Exception:
            log.exception("ошибка фоновой досылки буфера")

# --------------------------------------------------------------- worker -----

async def process_job(job: dict) -> None:
    """Полный цикл: субтитры -> LLM -> форматирование -> отправка в тред."""
    video_id = job["video_id"]
    thread = await _resolve_channel(job["thread_id"])
    if thread is None:
        log.error("тред %s недоступен, задача %s отброшена", job["thread_id"], video_id)
        return

    status = job.get("status")

    try:
        transcript = await asyncio.to_thread(fetch_transcript, video_id)
        summary = await asyncio.to_thread(summarize, transcript)
    except Exception as e:
        log.warning("саммари не вышло (%s): %s", video_id, e)
        summary = PROMPTS.get("error", "Не смог сделать саммари: {reason}").format(reason=e)

    link = f"https://youtu.be/{video_id}"
    full = format_for_discord(f"**Тезисы: {link}**\n\n{summary}")
    chunks = truncate_message(full, SPLIT_THRESHOLD)[:MAX_SPLIT_MESSAGES]

    # отправка: при сетевом сбое недоставленные чанки уходят в персистентный буфер
    for idx, chunk in enumerate(chunks):
        if await safe_send(thread, chunk):
            continue
        for rest in chunks[idx:]:
            _pending_append(thread.id, rest)
        log.warning("%s: буферизовано %d/%d чанков (Discord недоступен)",
                    video_id, len(chunks) - idx, len(chunks))
        return

    if status:
        try:
            await status.delete()
        except discord.HTTPException:
            pass
    log.info("готово: %s (%d чанк(ов))", video_id, len(chunks))


async def llm_worker():
    """Единственный потребитель очереди: LLM-запросы строго по одному."""
    while True:
        job = await job_queue.get()
        try:
            await process_job(job)
        except Exception:
            log.exception("задача %s упала", job.get("video_id"))
        finally:
            job_queue.task_done()


def build_thread_name(message: discord.Message, video_id: str) -> str:
    """Имя треда: текст сообщения без ссылки, либо 'Видео <id>'."""
    text = YT_RE.sub("", message.content).strip() or f"Видео {video_id}"
    return text[:90]


async def handle(message: discord.Message):
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
            name=build_thread_name(message, video_id),
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

    position = job_queue.qsize()
    if status and position:
        try:
            await status.edit(content=f"🎬 Принято, позиция в очереди: {position + 1}")
        except discord.HTTPException:
            pass

    await job_queue.put({
        "video_id": video_id,
        "thread_id": thread.id,
        "message_id": message.id,
        "status": status,
    })

# ------------------------------------------------------------------ main ---

_bg_started = False

@client.event
async def on_ready():
    global _bg_started
    log.info("запущен как %s (серверов: %d)", client.user, len(client.guilds))
    if not _bg_started:
        _bg_started = True
        asyncio.create_task(llm_worker())
        asyncio.create_task(flush_loop())
    # вернулись онлайн — сразу досылаем то, что накопилось за офлайн
    try:
        await flush_pending()
    except Exception:
        log.exception("досылка буфера при старте упала")


@client.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    try:
        await handle(message)
    except Exception:
        log.exception("ошибка обработки сообщения %s", message.id)


def main():
    missing = [n for n, v in [("DISCORD_BOT_TOKEN", BOT_TOKEN), ("LLM_API_KEY", LLM_API_KEY)] if not v]
    if missing:
        sys.exit(f"Не заданы переменные: {', '.join(missing)}. Создай .env рядом со скриптом.")
    client.run(BOT_TOKEN)


if __name__ == "__main__":
    main()
