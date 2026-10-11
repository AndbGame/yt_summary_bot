#!/usr/bin/env python3
"""Discord bot: создаёт тред под каждым сообщением с YouTube-ссылкой
и постит туда тезисное саммари видео (по субтитрам + LLM).

Настройка через переменные окружения или .env рядом со скриптом:
  DISCORD_BOT_TOKEN  — токен бота (обязательно)
  LLM_API_KEY        — ключ OpenAI-совместимого API (обязательно)
  LLM_BASE_URL       — базовый URL API (по умолчанию https://api.openai.com/v1)
  LLM_MODEL          — модель (по умолчанию gpt-4o-mini)

Запуск:  python yt_summary_bot.py
"""

import asyncio
import logging
import os
import re
import sys
from pathlib import Path

import discord
import requests
import yaml
import youtube_transcript_api
from openai import OpenAI

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
EMBED_DESC_LIMIT   = 4096

# Промпты — в prompts.yaml рядом со скриптом
def load_prompts():
    p = Path(__file__).parent / "prompts.yaml"
    if p.exists():
        return yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return {}

PROMPTS = load_prompts()

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


def summarize(transcript: str) -> str:
    client = OpenAI(api_key=LLM_API_KEY, base_url=LLM_BASE_URL)
    resp = client.chat.completions.create(
        model=LLM_MODEL,
        messages=[
            {"role": "system", "content": PROMPTS.get("system", "Ты кратко и по делу пересказываешь видео по субтитрам.")},
            {"role": "user", "content": PROMPTS.get("user", "").format(transcript=transcript[:50000])},
        ],
        max_tokens=700,
        temperature=0.3,
    )
    return resp.choices[0].message.content.strip()


# --------------------------------------------- discord-форматирование (из Hermes) ---
# Портировано из gateway/platforms/helpers.py и base.py агента Hermes:
#   convert_table_to_bullets / _render_table_block — GFM-таблицы не рендерятся
#   в Discord, превращаем их в "**заголовок** + • поле: значение";
#   truncate_message — сплит длинных сообщений с закрытием/переоткрытием
#   code-fence на границах и индикаторами (1/N).

_TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-{3,}.*\|.*$")
_TABLE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$")


def _split_table_row(line: str) -> list[str]:
    line = line.strip().strip("|")
    return [c.strip() for c in line.split("|")]


def _render_table_block(table_block: list[str]) -> str:
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


def truncate_message(content: str, max_length: int = MAX_MESSAGE_LENGTH) -> list[str]:
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
processed: set[str] = set()


def build_thread_name(message: discord.Message, video_id: str) -> str:
    """Имя треда: текст сообщения без ссылки, либо 'Видео <id>'."""
    text = YT_RE.sub("", message.content).strip() or f"Видео {video_id}"
    name = text[:90]
    return name


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

    # субтитры + LLM в отдельном потоке, чтобы не висел event loop
    try:
        transcript = await asyncio.to_thread(fetch_transcript, video_id)
        summary = await asyncio.to_thread(summarize, transcript)
    except Exception as e:
        log.warning("саммари не вышло (%s): %s", video_id, e)
        summary = PROMPTS.get("error", "Не смог сделать саммари: {reason}").format(reason=e)

    # форматирование под Discord (таблицы -> буллеты) и сплит по лимиту
    link = f"https://youtu.be/{video_id}"
    full = format_for_discord(f"**Тезисы: {link}**\n\n{summary}")
    chunks = truncate_message(full, SPLIT_THRESHOLD)[:MAX_SPLIT_MESSAGES]
    for chunk in chunks:
        await thread.send(chunk)
    if status:
        try:
            await status.delete()
        except discord.HTTPException:
            pass
    log.info("готово: %s (%d чанк(ов))", video_id, len(chunks))


@client.event
async def on_ready():
    log.info("запущен как %s (серверов: %d)", client.user, len(client.guilds))


@client.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    try:
        await handle(message)
    except Exception:
        log.exception("ошибка обработки сообщения %s", message.id)

# ------------------------------------------------------------------- main --

def main():
    missing = [n for n, v in [("DISCORD_BOT_TOKEN", BOT_TOKEN), ("LLM_API_KEY", LLM_API_KEY)] if not v]
    if missing:
        sys.exit(f"Не заданы переменные: {', '.join(missing)}. Создай .env рядом со скриптом.")
    client.run(BOT_TOKEN)


if __name__ == "__main__":
    main()
