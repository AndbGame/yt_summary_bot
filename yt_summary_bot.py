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

PROMPT = (
    "Тебе даны субтитры видео с YouTube (автогенерация, возможны ошибки распознавания). "
    "Сделай краткое тезисное описание видео на русском: 5-10 пунктов по сути, "
    "без воды. Если субтитры пустые или бессмысленные — так и напиши.\n\n"
    "СУБТИТРЫ:\n{transcript}"
)


def extract_video_id(text: str):
    """Возвращает (video_id, start, end) или None. start/end — границы совпадения."""
    m = YT_RE.search(text or "")
    if not m:
        return None
    return m.group(1), m.start(), m.end()


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
            {"role": "system", "content": "Ты кратко и по делу пересказываешь видео по субтитрам."},
            {"role": "user", "content": PROMPT.format(transcript=transcript[:50000])},
        ],
        max_tokens=700,
        temperature=0.3,
    )
    return resp.choices[0].message.content.strip()

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
        log.error("не смог создать тред: %s", e)
        if status:
            await status.edit(content="⚠️ Не удалось создать тред (лимиты Discord?).")
        return

    # субтитры + LLM в отдельном потоке, чтобы не висел event loop
    try:
        transcript = await asyncio.to_thread(fetch_transcript, video_id)
        summary = await asyncio.to_thread(summarize, transcript)
    except Exception as e:
        log.warning("саммари не вышло (%s): %s", video_id, e)
        summary = (
            f"Не смог сделать саммари этого видео: {e}\n"
            f"Вероятно, у видео отключены субтитры."
        )

    link = f"https://youtu.be/{video_id}"
    await thread.send(f"**Тезисы: {link}**\n\n{summary[:1900]}")
    if status:
        try:
            await status.delete()
        except discord.HTTPException:
            pass
    log.info("готово: %s", video_id)


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
